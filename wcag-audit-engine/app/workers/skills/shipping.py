"""commerce.shipping -- "can this ship to me, for how much, and what would
the cart total be?" for a product on any Shopify storefront, keyless.

Link two of the transaction chain. commerce.availability answers whether a
product can be bought; this bee puts the chosen variant in a real cart
session on the store, reads the store's own totals, and asks the store for
its shipping options to the destination. The store's answer is the answer:
the options and prices it would show at checkout, or the exact reason it
will not ship there (country not supported, state or postcode required,
variant sold out). No order is placed; the cart cookie is discarded.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import shopify_cart
from . import commerce
from .extract import validate_url
from .llm import validate_language

MAX_QUANTITY = 10
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(raw, field, max_len):
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise runtime.InvalidRequest(f"`{field}`, when given, must be text.")
    value = " ".join(value.split())
    if len(value) > max_len:
        raise runtime.InvalidRequest(f"`{field}` is over {max_len} characters.")
    return value


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    url = validate_url(payload.get("url"))
    if shopify_cart.product_js_url(url) is None:
        raise runtime.InvalidRequest("`url` must be a store product page (…/products/<handle>).")
    variant = _text(payload, "variant", 200)
    quantity = payload.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        raise runtime.InvalidRequest(f"`quantity` must be a whole number from 1 to {MAX_QUANTITY}.")
    ship_to = payload.get("ship_to")
    if not isinstance(ship_to, dict):
        raise runtime.InvalidRequest("`ship_to` is required: {country, province?, postal_code?}.")
    country = ship_to.get("country")
    if not isinstance(country, str) or not country.strip():
        raise runtime.InvalidRequest("`ship_to.country` is required: an ISO 3166-1 alpha-2 code (US, JP) or the country name.")
    country = " ".join(country.split())
    if _COUNTRY.match(country):
        country = country.upper()
    province = _text(ship_to, "province", 80)
    postal = _text(ship_to, "postal_code", 20)
    return {"url": url, "variant": variant, "quantity": quantity,
            "ship_to": {"country": country, "province": province, "postal_code": postal},
            "language": validate_language(payload)}


def precheck(payload: dict) -> None:
    parse(payload)


def _money(cents) -> Optional[float]:
    if isinstance(cents, bool) or not isinstance(cents, (int, float)):
        return None
    return round(cents / 100, 2)


def _price_text(value) -> Optional[float]:
    try:
        return round(float(value), 2) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _variant_name(v: dict) -> str:
    title = v.get("title") if isinstance(v.get("title"), str) else ""
    options = [v.get(k) for k in ("option1", "option2", "option3") if isinstance(v.get(k), str)]
    return title if title and title != "Default Title" else " / ".join(options) or title


def choose_variant(product: dict, wanted: Optional[str]) -> tuple:
    """(variant, note). Named variant by token overlap; else the first
    available; else the first one, so the store can say why it fails."""
    variants = [v for v in product.get("variants") or [] if isinstance(v, dict) and v.get("id") is not None]
    if not variants:
        raise runtime.PermanentProviderError("The product has no purchasable variants.")
    if wanted:
        options = [{"name": _variant_name(v), "sku": v.get("sku"), "_v": v} for v in variants]
        hit = commerce.match_option(wanted, options)
        if hit is None:
            names = ", ".join(o["name"] for o in options[:12])
            raise runtime.InvalidRequest(f"`variant` {wanted!r} matched none of the store's options: {names}.")
        return hit["_v"], None
    for v in variants:
        if v.get("available") is True:
            return v, None if len(variants) == 1 else "No variant named; the first available option was quoted."
    return variants[0], "No variant is available; the first option was quoted so the store's answer is on record."


def _option(rate: dict) -> dict:
    days = rate.get("delivery_days") if isinstance(rate.get("delivery_days"), list) else []
    days = [d for d in days if isinstance(d, int) and not isinstance(d, bool)]
    return {
        "name": rate.get("name") or rate.get("presentment_name"),
        "price": _price_text(rate.get("price")),
        "currency": rate.get("currency"),
        "delivery_days_min": min(days) if days else None,
        "delivery_days_max": max(days) if days else None,
        "description": (rate.get("description") or None),
        "carrier": rate.get("carrier_identifier"),
    }


async def shipping(ctx, payload: dict) -> dict:
    req = parse(payload)
    provider = shopify_cart.PROVIDERS[0]

    async def get_product(p):
        return await p.product(req["url"])
    product = await ctx.run("product", [provider], get_product, per_attempt_seconds=25, max_attempts=2)
    variant, note = choose_variant(product, req["variant"])
    notes = [note] if note else []

    async def get_quote(p):
        return await p.quote(req["url"], variant["id"], req["quantity"], req["ship_to"])
    quote = await ctx.run("cart", [provider], get_quote, per_attempt_seconds=30, max_attempts=1)

    item, cart = quote.get("item") or {}, quote.get("cart") or {}
    currency = cart.get("currency") or None
    if not quote["added"]:
        eligible, reasons, missing, options = False, [f"The store refused to add this variant to a cart: {quote['add_error']}"], [], []
    elif quote["rates"] is not None:
        options = sorted((_option(r) for r in quote["rates"]), key=lambda o: (o["price"] is None, o["price"] or 0))
        eligible = bool(options)
        reasons = [] if options else ["The store returned no shipping option for this destination."]
        missing = []
    else:
        errors = quote.get("errors") or {}
        missing = [f for f in ("province", "zip", "country", "address1", "city") if f in errors and
                   any(word in " ".join(errors[f]).lower() for word in ("select", "enter", "required", "can't be blank"))]
        unsupported = any("not supported" in " ".join(v).lower() or "not available" in " ".join(v).lower()
                          for v in errors.values())
        reasons = [f"{field}: {'; '.join(msgs)}" for field, msgs in errors.items()]
        eligible = False if unsupported else None
        options = []
        if missing and not unsupported:
            notes.append("The store needs more of the address before it will quote; add " + ", ".join(
                {"zip": "postal_code"}.get(f, f) for f in missing) + " to ship_to.")
    cheapest = options[0] if options and options[0]["price"] is not None else None
    subtotal = _money(cart.get("items_subtotal_price"))
    estimated = None
    if cheapest is not None and subtotal is not None and (cheapest["currency"] or currency) == currency:
        estimated = {"amount": round(subtotal + cheapest["price"], 2), "currency": currency, "includes_tax": False}
    return {
        "url": req["url"],
        "store": {"platform": "shopify", "domain": shopify_cart.urlparse(req["url"]).netloc, "currency": currency},
        "product": {"id": product.get("id"), "title": product.get("title"), "handle": product.get("handle")},
        "variant": {"id": variant.get("id"), "name": _variant_name(variant), "sku": variant.get("sku") or None,
                    "price": _money(variant.get("price")), "available": variant.get("available") if isinstance(variant.get("available"), bool) else None,
                    "requires_shipping": item.get("requires_shipping") if isinstance(item.get("requires_shipping"), bool) else
                    (variant.get("requires_shipping") if isinstance(variant.get("requires_shipping"), bool) else None)},
        "quantity": req["quantity"],
        "ship_to": req["ship_to"],
        "cart": ({"subtotal": subtotal, "total": _money(cart.get("total_price")), "currency": currency,
                  "item_count": cart.get("item_count") if isinstance(cart.get("item_count"), int) else None,
                  "requires_shipping": cart.get("requires_shipping") if isinstance(cart.get("requires_shipping"), bool) else None}
                 if cart else None),
        "eligible": eligible,
        "eligibility_reasons": reasons,
        "missing_fields": [{"zip": "postal_code"}.get(f, f) for f in missing],
        "shipping_options": options,
        "cheapest_shipping": ({"name": cheapest["name"], "price": cheapest["price"], "currency": cheapest["currency"]} if cheapest else None),
        "estimated_total": estimated,
        "taxes_note": "Sales tax or VAT is computed by the store at checkout and is not included here.",
        "source": "shopify-cart-api",
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"commerce.shipping": shipping}
PRECHECKS = {"commerce.shipping": precheck}
