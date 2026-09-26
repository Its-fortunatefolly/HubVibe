"""commerce.availability -- can this be bought or booked right now, at what
price, in which options, under the caller's conditions?

Suppliers rarely expose a transactional API even when their own page can
complete the sale. This worker reads the page the way the shopper would,
live at call time, and returns one structured answer: availability, price,
the options on offer and which one matches what the caller asked for,
quantity and ship-to eligibility where the page states them, and the
evidence each field rests on.

DETERMINISTIC FIRST, MODEL LAST, NEVER A GUESS. Three layers, cheapest and
most exact first, each tried only when the previous one could not answer:

  1. structured data the page publishes for machines: schema.org JSON-LD
     Product / ProductGroup / Offer / AggregateOffer (incl. hasVariant), and
     Open Graph product:* tags -- one plain GET, no browser, no model.
  2. the storefront's own product JSON when the page is a Shopify store
     (/products/<handle>.js: per-variant `available`, price, sku, options).
  3. a real browser render, the structured layers again on the rendered DOM,
     and only then Gemini over the rendered text, told to return null and
     "unknown" for anything the page does not state.

ALWAYS CURRENT: nothing here is cached. Every call fetches the page at call
time and the result carries `checked_at` (UTC) plus `source`, so the buyer
knows when the answer was true and what it rests on.
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

from .. import runtime
from ..providers import gemini, web
from . import extract as extract_skill
from . import llm as llm_skill

AVAILABILITY = ("in_stock", "out_of_stock", "preorder", "backorder", "limited",
                "discontinued", "unknown")
SOURCES = ("jsonld", "shopify", "opengraph", "llm", "none")
CONFIDENCE = ("high", "medium", "low")
MAX_OPTIONS = 200
MAX_EVIDENCE = 12
MAX_VARIANT_CHARS = 200
MAX_HTML_CHARS = 1_500_000
MAX_LLM_TEXT_CHARS = 30_000

# schema.org ItemAvailability -> ours. Keys are the bare enumeration names,
# lower-cased; the caller may have written the full URL, http or https.
_SCHEMA_AVAILABILITY = {
    "instock": "in_stock", "instoreonly": "in_stock", "onlineonly": "in_stock",
    "madetoorder": "in_stock",
    "outofstock": "out_of_stock", "soldout": "out_of_stock", "reserved": "out_of_stock",
    "preorder": "preorder", "presale": "preorder",
    "backorder": "backorder",
    "limitedavailability": "limited",
    "discontinued": "discontinued",
}
# Open Graph product:availability values seen in the wild.
_OG_AVAILABILITY = {
    "instock": "in_stock", "in stock": "in_stock", "available": "in_stock",
    "available for order": "in_stock",
    "oos": "out_of_stock", "out of stock": "out_of_stock", "outofstock": "out_of_stock",
    "sold out": "out_of_stock", "soldout": "out_of_stock",
    "preorder": "preorder", "pre-order": "preorder", "pending": "preorder",
    "backorder": "backorder", "discontinued": "discontinued",
}
# "Can I get it at all?" precedence when a product lists several options.
_PRECEDENCE = ("in_stock", "limited", "preorder", "backorder", "out_of_stock",
               "discontinued", "unknown")

_LD_SCRIPT = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL)
_META = re.compile(r"<meta\s+[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"([a-zA-Z_:-]+)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
_SHOPIFY_MARKER = re.compile(r"cdn\.shopify\.com|Shopify\.theme|window\.Shopify|shopify-section",
                             re.IGNORECASE)
_SHOPIFY_PATH = re.compile(r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/collections/[^/]+)?/products/([A-Za-z0-9._-]+)/?$")
_SHOPIFY_CURRENCY = re.compile(r"Shopify\.currency\s*=\s*\{\s*\"active\"\s*:\s*\"([A-Z]{3})\"")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")
# Words that name the KIND of option rather than the option: "size 13" must
# not match every option whose name says "Size".
_GENERIC_TOKENS = {
    "size", "sizes", "sized", "color", "colour", "colors", "colours", "option", "options",
    "variant", "variants", "style", "type", "model", "the", "a", "an", "in", "of", "and",
    "with", "for", "to", "or", "us", "uk", "eu", "one", "per",
}


# --- input -------------------------------------------------------------------

def validate(payload: dict) -> dict:
    """The caller's ask, checked. Raises InvalidRequest, which the router
    turns into a free 400 when run as the PRECHECK before the payment gate."""
    url = extract_skill.validate_url(payload.get("url"))
    variant = payload.get("variant")
    if variant is not None:
        if not isinstance(variant, str) or not variant.strip():
            raise runtime.InvalidRequest("`variant`, when given, must be a non-empty string.")
        if len(variant) > MAX_VARIANT_CHARS:
            raise runtime.InvalidRequest(
                f"`variant` is {len(variant)} characters, over the {MAX_VARIANT_CHARS} limit.")
        variant = variant.strip()
    quantity = payload.get("quantity")
    if quantity is not None:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100000:
            raise runtime.InvalidRequest("`quantity`, when given, must be a whole number from 1 to 100000.")
    ship_to = payload.get("ship_to")
    if ship_to is not None:
        if not isinstance(ship_to, str) or not _COUNTRY.match(ship_to.strip()):
            raise runtime.InvalidRequest(
                "`ship_to`, when given, must be a two-letter ISO 3166-1 country code such as US or JP.")
        ship_to = ship_to.strip().upper()
    language = llm_skill.validate_language(payload)
    return {"url": url, "variant": variant, "quantity": quantity,
            "ship_to": ship_to, "language": language}


def precheck(payload: dict) -> None:
    validate(payload)


# --- layer 1: structured data in the HTML -------------------------------------

def _jsonld_blocks(html: str) -> list:
    blocks = []
    for raw in _LD_SCRIPT.findall(html or ""):
        text = raw.strip()
        if not text:
            continue
        try:
            blocks.append(json.loads(text))
        except json.JSONDecodeError:
            # Some sites leave a trailing comment or a stray entity; one
            # broken block must not hide a good one beside it.
            continue
    return blocks


def _nodes(value, depth: int = 0):
    """Every dict reachable inside a JSON-LD block, incl. @graph and lists."""
    if depth > 12:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child, depth + 1)


def _types(node: dict) -> set:
    kind = node.get("@type")
    if isinstance(kind, str):
        return {kind.split("/")[-1].lower()}
    if isinstance(kind, list):
        return {str(k).split("/")[-1].lower() for k in kind}
    return set()


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _number(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,-]", "", value)
        if not cleaned:
            return None
        # "1.234,56" (comma decimal) vs "1,234.56" (dot decimal).
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            head, _, tail = cleaned.rpartition(",")
            cleaned = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else cleaned.replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _schema_availability(value) -> str:
    if not isinstance(value, str):
        return "unknown"
    key = value.strip().rstrip("/").split("/")[-1].lower()
    return _SCHEMA_AVAILABILITY.get(key, "unknown")


def _offer_facts(offer: dict) -> dict:
    """Price, currency, availability and eligibility read off one Offer or
    AggregateOffer node."""
    price = _number(offer.get("price"))
    if price is None:
        price = _number(offer.get("lowPrice"))
    spec = offer.get("priceSpecification")
    if price is None and isinstance(spec, dict):
        price = _number(spec.get("price"))
    currency = offer.get("priceCurrency") or (spec.get("priceCurrency") if isinstance(spec, dict) else None)
    currency = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None
    facts = {
        "availability": _schema_availability(offer.get("availability")),
        "price": price,
        "currency": currency,
        "high_price": _number(offer.get("highPrice")),
        "sku": offer.get("sku") if isinstance(offer.get("sku"), str) else None,
        "url": offer.get("url") if isinstance(offer.get("url"), str) else None,
        "inventory": None,
        "min_quantity": None,
        "max_quantity": None,
        "ships_to": [],
        "not_ships_to": [],
    }
    level = offer.get("inventoryLevel")
    if isinstance(level, dict):
        facts["inventory"] = _number(level.get("value"))
    elif level is not None:
        facts["inventory"] = _number(level)
    eligible_qty = offer.get("eligibleQuantity")
    if isinstance(eligible_qty, dict):
        facts["min_quantity"] = _number(eligible_qty.get("minValue"))
        facts["max_quantity"] = _number(eligible_qty.get("maxValue"))
    for region in _as_list(offer.get("eligibleRegion")) + _as_list(offer.get("areaServed")):
        code = _region_code(region)
        if code:
            facts["ships_to"].append(code)
    for region in _as_list(offer.get("ineligibleRegion")):
        code = _region_code(region)
        if code:
            facts["not_ships_to"].append(code)
    for detail in _as_list(offer.get("shippingDetails")):
        if not isinstance(detail, dict):
            continue
        for dest in _as_list(detail.get("shippingDestination")):
            code = _region_code(dest)
            if code:
                facts["ships_to"].append(code)
    return facts


def _region_code(region) -> Optional[str]:
    if isinstance(region, str):
        return region.strip().upper() if _COUNTRY.match(region.strip()) else None
    if isinstance(region, dict):
        for key in ("addressCountry", "name"):
            value = region.get(key)
            if isinstance(value, dict):
                value = value.get("name")
            if isinstance(value, str) and _COUNTRY.match(value.strip()):
                return value.strip().upper()
    return None


def _option_name(node: dict) -> str:
    name = node.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:200]
    parts = []
    for key in ("color", "size", "material", "pattern"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    for prop in _as_list(node.get("additionalProperty")):
        if isinstance(prop, dict) and isinstance(prop.get("value"), (str, int, float)):
            parts.append(str(prop["value"]))
    return " / ".join(parts)[:200] or (node.get("sku") if isinstance(node.get("sku"), str) else "option")


def _option_from(node: dict, offers: list, fallback_currency: Optional[str]) -> Optional[dict]:
    facts = [_offer_facts(o) for o in offers if isinstance(o, dict)]
    if not facts:
        facts = [_offer_facts({})]
    best = max(facts, key=lambda f: -_PRECEDENCE.index(f["availability"]))
    availability = best["availability"]
    price = next((f["price"] for f in facts if f["price"] is not None), None)
    currency = next((f["currency"] for f in facts if f["currency"]), None) or fallback_currency
    return {
        "name": _option_name(node),
        "available": _available_flag(availability),
        "availability": availability,
        "price": price,
        "currency": currency,
        "sku": (node.get("sku") if isinstance(node.get("sku"), str) else best["sku"]),
        "url": best["url"] or (node.get("url") if isinstance(node.get("url"), str) else None),
        "_facts": best,
    }


def _available_flag(availability: str) -> Optional[bool]:
    if availability in ("in_stock", "limited", "preorder", "backorder"):
        return True
    if availability in ("out_of_stock", "discontinued"):
        return False
    return None


def from_jsonld(html: str) -> Optional[dict]:
    """Product facts from the page's schema.org JSON-LD, or None when the
    page carries no Product / ProductGroup node."""
    products = []
    for block in _jsonld_blocks(html):
        for node in _nodes(block):
            if _types(node) & {"product", "productgroup", "productmodel", "individualproduct"}:
                products.append(node)
    if not products:
        return None
    # The page's main product: the one with variants or offers, else the first.
    products.sort(key=lambda n: (0 if n.get("hasVariant") else 1, 0 if n.get("offers") else 1))
    product = products[0]
    top_offers = [o for o in _as_list(product.get("offers")) if isinstance(o, dict)]
    fallback_currency = next((_offer_facts(o)["currency"] for o in top_offers
                              if _offer_facts(o)["currency"]), None)
    options, evidence = [], []
    variants = [v for v in _as_list(product.get("hasVariant")) if isinstance(v, dict)]
    if variants:
        for variant in variants[:MAX_OPTIONS]:
            option = _option_from(variant, _as_list(variant.get("offers")), fallback_currency)
            if option:
                options.append(option)
        evidence.append(f"JSON-LD {product.get('@type')} with {len(variants)} variants (hasVariant)")
    elif len(top_offers) > 1 and any(isinstance(o.get("sku"), str) for o in top_offers):
        for offer in top_offers[:MAX_OPTIONS]:
            options.append(_option_from({"name": offer.get("name") or offer.get("sku"),
                                         "sku": offer.get("sku")}, [offer], fallback_currency))
        evidence.append(f"JSON-LD Product with {len(top_offers)} Offers")

    top_facts = [_offer_facts(o) for o in top_offers]
    if options:
        product_availability = _best_of([o["availability"] for o in options])
        price = next((o["price"] for o in options if o["price"] is not None), None)
        currency = next((o["currency"] for o in options if o["currency"]), None)
        for option in options[:3]:
            evidence.append(f"variant {option['name']!r}: availability {option['availability']}"
                            + (f", price {option['price']} {option['currency'] or ''}".rstrip()
                               if option["price"] is not None else ""))
    else:
        if not top_facts:
            product_availability, price, currency = "unknown", None, None
            evidence.append(f"JSON-LD {product.get('@type')} with no Offer")
        else:
            best = max(top_facts, key=lambda f: -_PRECEDENCE.index(f["availability"]))
            product_availability = best["availability"]
            price = next((f["price"] for f in top_facts if f["price"] is not None), None)
            currency = next((f["currency"] for f in top_facts if f["currency"]), None)
            for offer, facts in zip(top_offers, top_facts):
                bits = [f"JSON-LD {offer.get('@type') or 'Offer'}"]
                if offer.get("availability"):
                    bits.append(f"availability {offer.get('availability')}")
                if facts["price"] is not None:
                    bits.append(f"price {facts['price']} {facts['currency'] or ''}".rstrip())
                elif facts["high_price"] is not None:
                    bits.append(f"price range up to {facts['high_price']} {facts['currency'] or ''}".rstrip())
                evidence.append(" ".join(bits))
    facts_pool = [o["_facts"] for o in options] or top_facts
    return {
        "name": product.get("name") if isinstance(product.get("name"), str) else None,
        "availability": product_availability,
        "price": price,
        "currency": currency,
        "options": options,
        "ships_to": sorted({c for f in facts_pool for c in f["ships_to"]}),
        "not_ships_to": sorted({c for f in facts_pool for c in f["not_ships_to"]}),
        "evidence": evidence,
        "source": "jsonld",
    }


def _best_of(availabilities: list) -> str:
    known = [a for a in availabilities if a in _PRECEDENCE]
    if not known:
        return "unknown"
    return min(known, key=_PRECEDENCE.index)


def _meta_tags(html: str) -> dict:
    tags = {}
    for tag in _META.findall(html or ""):
        attrs = {k.lower(): v for k, _, v in _ATTR.findall(tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs and key.lower() not in tags:
            tags[key.lower()] = attrs["content"]
    return tags


def from_opengraph(html: str) -> Optional[dict]:
    """Product facts from Open Graph product:* tags, or None when absent."""
    tags = _meta_tags(html)
    raw_availability = tags.get("product:availability") or tags.get("og:availability")
    amount = tags.get("product:price:amount") or tags.get("og:price:amount")
    if raw_availability is None and amount is None:
        return None
    availability = _OG_AVAILABILITY.get((raw_availability or "").strip().lower(), "unknown")
    currency = tags.get("product:price:currency") or tags.get("og:price:currency")
    evidence = []
    if raw_availability is not None:
        evidence.append(f"Open Graph product:availability={raw_availability}")
    if amount is not None:
        evidence.append(f"Open Graph product:price:amount={amount} {currency or ''}".rstrip())
    return {
        "name": tags.get("og:title"),
        "availability": availability,
        "price": _number(amount),
        "currency": currency.strip().upper() if isinstance(currency, str) and currency.strip() else None,
        "options": [],
        "ships_to": [],
        "not_ships_to": [],
        "evidence": evidence,
        "source": "opengraph",
    }


# --- layer 2: Shopify's own product JSON --------------------------------------

def shopify_product_js_url(url: str, html: str) -> Optional[str]:
    """The store's /products/<handle>.js for a Shopify product page, or None."""
    if not _SHOPIFY_MARKER.search(html or ""):
        return None
    parsed = urlparse(url)
    match = _SHOPIFY_PATH.match(parsed.path)
    if not match:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, f"/products/{match.group(1)}.js", "", "", ""))


def from_shopify(product: dict, page_html: str, fallback_currency: Optional[str]) -> Optional[dict]:
    """Product facts from a Shopify product .js body."""
    if not isinstance(product, dict) or not isinstance(product.get("variants"), list):
        return None
    currency = fallback_currency
    match = _SHOPIFY_CURRENCY.search(page_html or "")
    if match:
        currency = match.group(1)
    options = []
    for variant in product["variants"][:MAX_OPTIONS]:
        if not isinstance(variant, dict):
            continue
        available = variant.get("available")
        available = available if isinstance(available, bool) else None
        availability = ("in_stock" if available else "out_of_stock") if available is not None else "unknown"
        price = variant.get("price")
        price = round(price / 100, 2) if isinstance(price, (int, float)) and not isinstance(price, bool) else None
        inventory = variant.get("inventory_quantity")
        inventory = float(inventory) if isinstance(inventory, (int, float)) and not isinstance(inventory, bool) else None
        managed = bool(variant.get("inventory_management"))
        policy = variant.get("inventory_policy")
        option_values = [v for v in (variant.get("option1"), variant.get("option2"), variant.get("option3"))
                         if isinstance(v, str) and v.strip()]
        name = variant.get("public_title") or variant.get("title") or " / ".join(option_values) or "option"
        options.append({
            "name": str(name)[:200],
            "available": available,
            "availability": availability,
            "price": price,
            "currency": currency,
            "sku": variant.get("sku") if isinstance(variant.get("sku"), str) and variant.get("sku") else None,
            "url": None,
            "_facts": {
                "availability": availability, "price": price, "currency": currency,
                # Only a managed variant that refuses oversells has a real ceiling.
                "inventory": inventory if (managed and policy == "deny" and inventory is not None) else None,
                "min_quantity": None, "max_quantity": None, "ships_to": [], "not_ships_to": [],
                "requires_shipping": variant.get("requires_shipping"),
            },
        })
    if not options:
        return None
    available_count = sum(1 for o in options if o["available"])
    known = sum(1 for o in options if o["available"] is not None)
    availability = _best_of([o["availability"] for o in options])
    top_available = product.get("available")
    if isinstance(top_available, bool) and known == 0:
        availability = "in_stock" if top_available else "out_of_stock"
    return {
        "name": product.get("title") if isinstance(product.get("title"), str) else None,
        "availability": availability,
        "price": next((o["price"] for o in options if o["price"] is not None), None),
        "currency": currency,
        "options": options,
        "ships_to": [],
        "not_ships_to": [],
        "evidence": [f"Shopify product JSON: {available_count} of {len(options)} variants available"],
        "source": "shopify",
    }


# --- matching the caller's ask to what the page offers -----------------------

def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall((text or "").lower())
            if (len(t) > 1 or t.isdigit()) and t not in _GENERIC_TOKENS}


def match_option(variant: Optional[str], options: list) -> Optional[dict]:
    """The option whose name shares the most tokens with the caller's
    variant text; None when nothing overlaps. Exact (case-insensitive) name
    match wins outright."""
    if not variant or not options:
        return None
    wanted = variant.strip().lower()
    for option in options:
        if (option.get("name") or "").strip().lower() == wanted:
            return option
    asked = _tokens(variant)
    if not asked:
        return None
    best, best_score = None, 0
    for option in options:
        have = _tokens(option.get("name") or "") | _tokens(option.get("sku") or "")
        score = len(asked & have)
        if score > best_score:
            best, best_score = option, score
    return best


def _quantity_ok(quantity: Optional[int], facts: Optional[dict], availability: str) -> Optional[bool]:
    """Whether `quantity` can be ordered, from what the page states: a stock
    count, an order ceiling or floor, or plain unavailability. None when the
    page gives no basis to say -- never a guess."""
    if quantity is None:
        return None
    if availability in ("out_of_stock", "discontinued"):
        return False
    if not facts:
        return None
    if facts.get("max_quantity") is not None and quantity > facts["max_quantity"]:
        return False
    if facts.get("min_quantity") is not None and quantity < facts["min_quantity"]:
        return False
    if facts.get("inventory") is not None:
        return quantity <= facts["inventory"]
    if facts.get("max_quantity") is not None or facts.get("min_quantity") is not None:
        # The page states an order bound and this quantity is inside it.
        return availability in ("in_stock", "limited", "preorder", "backorder") or None
    if quantity == 1 and availability in ("in_stock", "limited", "preorder", "backorder"):
        # "In stock" is the page's own statement that one can be ordered.
        return True
    return None


def _ship_to_ok(ship_to: Optional[str], ships_to: list, not_ships_to: list) -> Optional[bool]:
    if not ship_to:
        return None
    if ship_to in not_ships_to:
        return False
    if ships_to:
        return ship_to in ships_to
    return None


def _public_option(option: dict) -> dict:
    return {k: v for k, v in option.items() if not k.startswith("_")}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- layer 3: the model, over the rendered page --------------------------------

_SHOPPER = (
    "You are checking, for a software buyer, whether the product, service or "
    "booking on this page can actually be purchased or booked right now. Use "
    "only what the page states. For anything it does not state return null "
    "(or \"unknown\" for availability). Never guess, never infer from tone."
)


def _llm_prompt(text: str, req: dict, title: Optional[str]) -> str:
    asks = []
    if req.get("variant"):
        asks.append(f"the option/variant described as: {req['variant']!r}")
    if req.get("quantity"):
        asks.append(f"a quantity of {req['quantity']}")
    if req.get("ship_to"):
        asks.append(f"delivery to country code {req['ship_to']}")
    conditions = ("Conditions to check: " + "; ".join(asks) + ".") if asks else \
        "No extra conditions: answer for the product as listed."
    return (
        f"Page title: {title or 'untitled'}\n\nPage text:\n\n{text[:MAX_LLM_TEXT_CHARS]}\n\n---\n\n"
        f"{conditions}\n"
        "Return exactly this JSON object:\n"
        '{"availability": "in_stock|out_of_stock|preorder|backorder|limited|discontinued|unknown", '
        '"price": number or null, "currency": "ISO 4217 code" or null, '
        '"options": [{"name": "...", "available": true|false|null, "price": number or null}], '
        '"matched_option": "name of the option matching the requested variant" or null, '
        '"quantity_ok": true|false|null, "ship_to_ok": true|false|null, '
        '"shipping": "what the page says about shipping/delivery" or null, '
        '"eligibility_notes": ["any restriction the page states: region, age, membership, minimums"], '
        '"evidence": ["exact short quotes from the page that each answer rests on"], '
        '"confidence": "high|medium|low"}\n'
        "Prices are numbers without symbols. `options` lists every purchasable variation the "
        "page shows (sizes, colours, dates, room types, fare classes), at most 50."
    )


async def _from_llm(ctx, text: str, req: dict, title: Optional[str]) -> dict:
    prompt = _llm_prompt(text, req, title)
    system = llm_skill.in_language(_SHOPPER, req.get("language"))

    async def call(provider):
        return await provider.generate_json(prompt, system=system, temperature=0.0)

    value = await ctx.run("assess", gemini.PROVIDERS, call, per_attempt_seconds=90)
    parsed = value["json"]
    if not isinstance(parsed, dict):
        raise runtime.InvalidProviderResponse("Model returned JSON that is not an object.")
    availability = parsed.get("availability")
    availability = availability if availability in AVAILABILITY else "unknown"
    options = []
    for raw in _as_list(parsed.get("options"))[:50]:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            continue
        available = raw.get("available") if isinstance(raw.get("available"), bool) else None
        options.append({
            "name": raw["name"][:200], "available": available,
            "availability": ("in_stock" if available else "out_of_stock") if available is not None else "unknown",
            "price": _number(raw.get("price")), "currency": None, "sku": None, "url": None,
            "_facts": {"availability": "unknown", "price": None, "currency": None, "inventory": None,
                       "min_quantity": None, "max_quantity": None, "ships_to": [], "not_ships_to": []},
        })
    currency = parsed.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) and len(currency.strip()) == 3 else None
    for option in options:
        option["currency"] = currency
    confidence = parsed.get("confidence") if parsed.get("confidence") in CONFIDENCE else "low"
    return {
        "name": title,
        "availability": availability,
        "price": _number(parsed.get("price")),
        "currency": currency,
        "options": options,
        "ships_to": [],
        "not_ships_to": [],
        "evidence": [str(e)[:300] for e in _as_list(parsed.get("evidence")) if isinstance(e, (str, int, float))][:MAX_EVIDENCE],
        "source": "llm",
        "llm": {
            "matched_option": parsed.get("matched_option") if isinstance(parsed.get("matched_option"), str) else None,
            "quantity_ok": parsed.get("quantity_ok") if isinstance(parsed.get("quantity_ok"), bool) else None,
            "ship_to_ok": parsed.get("ship_to_ok") if isinstance(parsed.get("ship_to_ok"), bool) else None,
            "shipping": parsed.get("shipping") if isinstance(parsed.get("shipping"), str) else None,
            "eligibility_notes": [str(n)[:300] for n in _as_list(parsed.get("eligibility_notes"))
                                  if isinstance(n, str) and n.strip()][:MAX_EVIDENCE],
            "confidence": confidence,
            "model": value["model"],
        },
    }


# --- the worker ---------------------------------------------------------------

def _structured(html: str) -> Optional[dict]:
    """JSON-LD first, Open Graph second; the first layer that names an
    availability wins, else whichever exists (for its price)."""
    ld = from_jsonld(html)
    og = from_opengraph(html)
    for facts in (ld, og):
        if facts and facts["availability"] != "unknown":
            if facts is og and ld and ld["options"]:
                # Variants from JSON-LD, availability from OG: keep both.
                merged = dict(ld)
                merged.update(availability=facts["availability"], source="opengraph",
                              evidence=ld["evidence"] + og["evidence"])
                return merged
            return facts
    return ld or og


async def check(ctx, payload: dict) -> dict:
    req = validate(payload)
    url = req["url"]

    # Layer 1: one plain GET, the structured data machines are given.
    async def fetch(provider):
        # The whole document: a page's JSON-LD can sit past fetch.raw's cap.
        return await provider.fetch(url, max_chars=MAX_HTML_CHARS)

    page = await ctx.run("fetch", web.FETCH_PROVIDERS, fetch, per_attempt_seconds=30)
    html = (page.get("text") or "")[:MAX_HTML_CHARS] if page.get("status", 0) < 400 else ""
    facts = _structured(html) if html else None
    rendered, title, text = False, None, None

    # Layer 2: a Shopify store's own product JSON, exact per-variant stock.
    if html and (facts is None or facts["availability"] == "unknown" or not facts["options"]):
        js_url = shopify_product_js_url(url, html)
        if js_url:
            try:
                async def fetch_js(provider):
                    return await provider.fetch(js_url, max_chars=MAX_HTML_CHARS)

                body = await ctx.run("shopify_json", web.FETCH_PROVIDERS, fetch_js, per_attempt_seconds=20)
                if body.get("status") == 200 and body.get("text"):
                    shop = from_shopify(json.loads(body["text"]), html,
                                        facts["currency"] if facts else None)
                    if shop:
                        if facts and facts["evidence"]:
                            shop["evidence"] = facts["evidence"] + shop["evidence"]
                        facts = shop
            except (runtime.WorkerError, ValueError):
                pass  # the page's own layers still stand

    # Layer 3: render, re-read the structured layers, then ask the model.
    if facts is None or facts["availability"] == "unknown":
        async def render(provider):
            return await provider.extract(url)

        try:
            view = await ctx.run("render", web.PROVIDERS, render, per_attempt_seconds=45)
        except runtime.WorkerError:
            view = None
        if view:
            rendered = bool(view.get("rendered"))
            title, text = view.get("title"), view.get("text")
            again = _structured((view.get("html") or "")[:MAX_HTML_CHARS]) if view.get("html") else None
            if again and again["availability"] != "unknown":
                facts = again
            elif text:
                facts = await _from_llm(ctx, text, req, title)

    checked_at = _now()
    if facts is None:
        facts = {"name": title, "availability": "unknown", "price": None, "currency": None,
                 "options": [], "ships_to": [], "not_ships_to": [], "source": "none",
                 "evidence": [f"HTTP {page.get('status')} from the page; no product data found"]}

    options = facts["options"]
    matched = match_option(req["variant"], options)
    llm = facts.get("llm") or {}
    if matched is None and llm.get("matched_option"):
        matched = match_option(llm["matched_option"], options)

    notes = list(llm.get("eligibility_notes") or [])
    if req["variant"] and options and matched is None:
        availability = "unknown"
        notes.append(f"No listed option matched {req['variant']!r}; {len(options)} options are listed.")
    elif matched is not None:
        availability = matched["availability"]
    else:
        availability = facts["availability"]
    if req["variant"] and not options and facts["source"] != "llm":
        notes.append("The page lists no separate options; the answer is for the product as listed.")

    subject_facts = matched["_facts"] if matched else (
        options[0]["_facts"] if len(options) == 1 else None)
    if subject_facts is None and facts["source"] in ("jsonld", "opengraph") and not options:
        subject_facts = {"availability": availability, "inventory": None,
                         "min_quantity": None, "max_quantity": None}
    quantity_ok = _quantity_ok(req["quantity"], subject_facts, availability)
    if quantity_ok is None and req["quantity"] is not None:
        quantity_ok = llm.get("quantity_ok")
    ship_to_ok = _ship_to_ok(req["ship_to"], facts.get("ships_to") or [], facts.get("not_ships_to") or [])
    if ship_to_ok is None and req["ship_to"]:
        ship_to_ok = llm.get("ship_to_ok")
    if req["ship_to"] and facts.get("ships_to"):
        notes.append(f"Page states delivery regions: {', '.join(facts['ships_to'][:20])}.")

    if facts["source"] in ("jsonld", "shopify") and availability != "unknown" and (matched or not req["variant"]):
        confidence = "high"
    elif facts["source"] == "opengraph" and availability != "unknown":
        confidence = "medium"
    elif facts["source"] == "llm":
        confidence = llm.get("confidence") or "low"
    else:
        confidence = "low"
    if availability == "unknown":
        # No layer could say whether it can be had: never call that anything
        # but low, whatever the model thought of its own reading.
        confidence = "low"

    price = matched["price"] if matched and matched["price"] is not None else facts["price"]
    currency = (matched["currency"] if matched and matched["currency"] else facts["currency"])
    return {
        "url": url,
        "final_url": page.get("final_url"),
        "title": facts.get("name") or title,
        "available": _available_flag(availability),
        "availability": availability,
        "price": price,
        "currency": currency,
        "options": [_public_option(o) for o in options],
        "options_count": len(options),
        "matched_option": _public_option(matched) if matched else None,
        "variant": req["variant"],
        "quantity": req["quantity"],
        "quantity_ok": quantity_ok,
        "ship_to": req["ship_to"],
        "ship_to_ok": ship_to_ok,
        "shipping": llm.get("shipping"),
        "eligibility_notes": notes,
        "evidence": list(facts["evidence"])[:MAX_EVIDENCE],
        "source": facts["source"],
        "javascript_rendered": rendered,
        "http_status": page.get("status"),
        "confidence": confidence,
        "checked_at": checked_at,
        "language": req["language"],
        "model": llm.get("model"),
    }


SKILLS = {"commerce.availability": check}
PRECHECKS = {"commerce.availability": precheck}
