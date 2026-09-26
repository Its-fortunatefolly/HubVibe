"""commerce.availability -- can this be bought or booked right now?

Pinned here: each answering layer on a fixture page (schema.org JSON-LD
variants, a Shopify product JSON, Open Graph tags, the model over a
rendered page) and the order they are tried in -- structured data first,
the model last, and never when a cheaper layer already answered; the
caller's conditions (variant, quantity, ship_to) resolved against what the
page states, with "unknown" rather than a guess when it does not; the free
refusal of bad input before the payment gate; the HTTP route and the MCP
tool `hubvibe_commerce_availability` selling the same row; and the
always-current contract: `checked_at` is required and stamped per call.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
TOOL = "hubvibe_commerce_availability"
WORKER = "commerce.availability"
PATH = "/work/commerce/availability"
URL = "https://shop.example.com/products/wool-runner"
# The HTTP tests go through main's real SSRF guard, which resolves the host.
HTTP_URL = "https://example.com/products/wool-runner"

JSONLD_PAGE = """<html><head><title>Wool Runner</title>
<meta property="og:type" content="product">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "ProductGroup", "name": "Wool Runner",
 "offers": {"@type": "AggregateOffer", "lowPrice": 110, "highPrice": 120, "priceCurrency": "USD"},
 "hasVariant": [
   {"@type": "Product", "name": "Wool Runner - Natural Black - Size 9", "sku": "WR9", "size": "9",
    "offers": {"@type": "Offer", "availability": "https://schema.org/InStock", "price": 110,
               "priceCurrency": "USD", "url": "https://shop.example.com/products/wool-runner?size=9",
               "eligibleQuantity": {"@type": "QuantitativeValue", "maxValue": 5},
               "shippingDetails": {"@type": "OfferShippingDetails", "shippingDestination": [
                   {"@type": "DefinedRegion", "addressCountry": "US"},
                   {"@type": "DefinedRegion", "addressCountry": "CA"}]}}},
   {"@type": "Product", "name": "Wool Runner - Natural Black - Size 10", "sku": "WR10", "size": "10",
    "offers": {"@type": "Offer", "availability": "http://schema.org/OutOfStock", "price": 110,
               "priceCurrency": "USD"}}
 ]}
</script></head><body><h1>Wool Runner</h1></body></html>"""

SHOPIFY_PAGE = """<html><head><title>Wool Runner</title>
<script src="https://cdn.shopify.com/s/files/1/theme.js"></script>
<script>Shopify.currency = {"active":"USD","rate":"1.0"};</script>
</head><body>Wool Runner</body></html>"""
SHOPIFY_JS = {
    "title": "Wool Runner", "available": True, "variants": [
        {"title": "9", "public_title": "9", "available": True, "price": 11000, "sku": "A9",
         "inventory_management": "shopify", "inventory_policy": "deny", "inventory_quantity": 3,
         "option1": "9", "requires_shipping": True},
        {"title": "10", "public_title": "10", "available": False, "price": 11000, "sku": "A10",
         "inventory_management": "shopify", "inventory_policy": "deny", "inventory_quantity": 0,
         "option1": "10", "requires_shipping": True},
    ]}

OG_PAGE = """<html><head><title>Sofa</title>
<meta property="og:title" content="Velvet Sofa">
<meta property="product:availability" content="instock">
<meta property="product:price:amount" content="1.234,56">
<meta property="product:price:currency" content="EUR">
</head><body>Velvet Sofa</body></html>"""

PLAIN_PAGE = """<html><head><title>Deluxe room</title></head>
<body><p>Deluxe room. Sold out for October. From 120 EUR per night. Minimum 2 nights.</p></body></html>"""


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()

@pytest.fixture(autouse=True)
def _target_guard(monkeypatch):
    """main.py injects the audit engine's SSRF guard into the web provider at
    startup; these unit tests run without main, so give the provider a guard
    with the same shape (refuse link-local/private, allow the public web)."""
    def guard(url):
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host.startswith(("169.254.", "10.", "127.", "localhost", "192.168.")):
            return "must not point at a private, loopback or link-local address"
        return None
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason", guard)


def _commerce():
    return W.skills.commerce


def _fetch_value(url, status=200, text="", content_type="text/html"):
    return {"url": url, "final_url": url, "status": status, "content_type": content_type,
            "bytes": len(text), "text": text, "truncated": False, "headers": {}}


class _Ctx:
    """Enough of JobContext for the skill: serves each step from fixtures and
    records which steps ran and what the model was told."""

    def __init__(self, fetches=None, render=None, render_error=None, llm=None):
        self.fetches = fetches or {}
        self.render_value = render
        self.render_error = render_error
        self.llm = llm
        self.steps, self.systems, self.prompts = [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _Provider:
            id = "fake"

            def available(self):
                return True

            async def fetch(self, url, **kwargs):
                if url not in outer.fetches:
                    raise AssertionError(f"unexpected fetch of {url}")
                return W.runtime.ProviderResult(value=outer.fetches[url], cost_micros=0, cost_measured=True)

            async def extract(self, url):
                if outer.render_error is not None:
                    raise outer.render_error
                return W.runtime.ProviderResult(value=outer.render_value, cost_micros=0, cost_measured=True)

            async def generate_json(self, prompt, system=None, temperature=0.2):
                outer.prompts.append(prompt)
                outer.systems.append(system)
                return W.runtime.ProviderResult(
                    value={"json": outer.llm, "model": "fake-model", "prompt_tokens": 1,
                           "output_tokens": 1, "text": json.dumps(outer.llm)},
                    cost_micros=0, cost_measured=True)

        result = await call(_Provider())
        return result.value


def _run(ctx, **payload):
    payload.setdefault("url", URL)
    return asyncio.run(_commerce().check(ctx, payload))


# --- layer 1: schema.org JSON-LD ---------------------------------------------

def test_jsonld_variants_answer_for_the_named_size_without_a_browser_or_a_model():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    result = _run(ctx, variant="size 9", quantity=2, ship_to="ca")
    assert ctx.steps == ["fetch"]
    assert result["source"] == "jsonld" and result["confidence"] == "high"
    assert result["availability"] == "in_stock" and result["available"] is True
    assert result["price"] == 110 and result["currency"] == "USD"
    assert result["matched_option"]["name"].endswith("Size 9")
    assert result["matched_option"]["sku"] == "WR9"
    assert result["options_count"] == 2 and len(result["options"]) == 2
    assert result["quantity_ok"] is True and result["ship_to_ok"] is True
    assert result["ship_to"] == "CA" and result["quantity"] == 2
    assert result["javascript_rendered"] is False and result["model"] is None
    assert result["http_status"] == 200
    assert result["checked_at"].endswith("Z") and "T" in result["checked_at"]
    assert any("in_stock" in e and "110" in e for e in result["evidence"])
    assert "_facts" not in json.dumps(result)


def test_a_size_the_page_lists_as_out_of_stock_is_false_and_no_quantity_is_ok():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    result = _run(ctx, variant="size 10", quantity=1)
    assert result["availability"] == "out_of_stock" and result["available"] is False
    assert result["quantity_ok"] is False
    assert result["matched_option"]["sku"] == "WR10"


def test_a_quantity_above_the_stated_ceiling_is_not_ok():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    assert _run(ctx, variant="size 9", quantity=6)["quantity_ok"] is False
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    assert _run(ctx, variant="size 9", quantity=5)["quantity_ok"] is True


def test_ship_to_outside_the_stated_regions_is_false_and_the_regions_are_disclosed():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    result = _run(ctx, variant="size 9", ship_to="JP")
    assert result["ship_to_ok"] is False
    assert any("CA" in n and "US" in n for n in result["eligibility_notes"])


def test_a_variant_the_page_does_not_list_is_unknown_not_a_guess():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    result = _run(ctx, variant="size 13")
    assert result["availability"] == "unknown" and result["available"] is None
    assert result["matched_option"] is None
    assert result["confidence"] == "low"
    assert any("No listed option matched" in n for n in result["eligibility_notes"])
    assert result["options_count"] == 2  # the buyer can see what IS listed


def test_without_a_variant_the_answer_is_whether_the_product_can_be_had_at_all():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=JSONLD_PAGE)})
    result = _run(ctx)
    assert result["availability"] == "in_stock"  # one size in stock, one out
    assert result["matched_option"] is None and result["variant"] is None
    assert result["quantity_ok"] is None and result["ship_to_ok"] is None


def test_one_of_an_in_stock_option_is_orderable_but_more_needs_a_count():
    """In stock is the page's own statement that one can be ordered; a
    larger quantity needs a stated count or ceiling, else it is unknown."""
    page = JSONLD_PAGE.replace('"eligibleQuantity": {"@type": "QuantitativeValue", "maxValue": 5},', "")
    assert _run(_Ctx(fetches={URL: _fetch_value(URL, text=page)}), variant="size 9", quantity=1)["quantity_ok"] is True
    assert _run(_Ctx(fetches={URL: _fetch_value(URL, text=page)}), variant="size 9", quantity=2)["quantity_ok"] is None


# --- layer 2: Shopify's own product JSON --------------------------------------

def test_shopify_product_json_supplies_exact_per_variant_stock():
    js_url = "https://shop.example.com/products/wool-runner.js"
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=SHOPIFY_PAGE),
                        js_url: _fetch_value(js_url, text=json.dumps(SHOPIFY_JS),
                                             content_type="application/json")})
    result = _run(ctx, variant="10")
    assert ctx.steps == ["fetch", "shopify_json"]
    assert result["source"] == "shopify" and result["confidence"] == "high"
    assert result["availability"] == "out_of_stock" and result["available"] is False
    assert result["price"] == 110.0 and result["currency"] == "USD"
    assert result["matched_option"]["sku"] == "A10"
    assert any("1 of 2 variants available" in e for e in result["evidence"])


def test_shopify_managed_inventory_bounds_the_quantity():
    js_url = "https://shop.example.com/products/wool-runner.js"
    fetches = {URL: _fetch_value(URL, text=SHOPIFY_PAGE),
               js_url: _fetch_value(js_url, text=json.dumps(SHOPIFY_JS), content_type="application/json")}
    assert _run(_Ctx(fetches=fetches), variant="9", quantity=3)["quantity_ok"] is True
    assert _run(_Ctx(fetches=fetches), variant="9", quantity=4)["quantity_ok"] is False


def test_the_shopify_url_is_derived_only_for_shopify_product_pages():
    commerce = _commerce()
    assert commerce.shopify_product_js_url(URL, SHOPIFY_PAGE) == \
        "https://shop.example.com/products/wool-runner.js"
    assert commerce.shopify_product_js_url(
        "https://shop.example.com/collections/shoes/products/wool-runner?variant=1", SHOPIFY_PAGE) == \
        "https://shop.example.com/products/wool-runner.js"
    assert commerce.shopify_product_js_url(URL, JSONLD_PAGE) is None  # no Shopify marker
    assert commerce.shopify_product_js_url("https://shop.example.com/pages/about", SHOPIFY_PAGE) is None


# --- layer 1b: Open Graph -------------------------------------------------------

def test_open_graph_tags_answer_with_medium_confidence_and_a_locale_price():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=OG_PAGE)})
    result = _run(ctx)
    assert ctx.steps == ["fetch"]
    assert result["source"] == "opengraph" and result["confidence"] == "medium"
    assert result["availability"] == "in_stock"
    assert result["price"] == 1234.56 and result["currency"] == "EUR"
    assert result["title"] == "Velvet Sofa"


# --- layer 3: render, then the model, in the caller's language ----------------

def test_a_page_with_no_structured_data_is_rendered_then_read_by_the_model_in_the_asked_language():
    ctx = _Ctx(
        fetches={URL: _fetch_value(URL, text=PLAIN_PAGE)},
        render={"url": URL, "final_url": URL, "title": "Deluxe room", "rendered": True,
                "text": "Deluxe room. Sold out for October. From 120 EUR per night. Minimum 2 nights.",
                "text_chars": 80, "truncated": False, "links": [], "html": PLAIN_PAGE},
        llm={"availability": "out_of_stock", "price": 120, "currency": "eur",
             "options": [{"name": "Deluxe room", "available": False, "price": 120}],
             "matched_option": "Deluxe room", "quantity_ok": None, "ship_to_ok": None,
             "shipping": None, "eligibility_notes": ["Minimum 2 nights"],
             "evidence": ["Sold out for October"], "confidence": "medium"})
    result = _run(ctx, variant="deluxe room in October", language="ja")
    assert ctx.steps == ["fetch", "render", "assess"]
    assert result["source"] == "llm" and result["model"] == "fake-model"
    assert result["availability"] == "out_of_stock" and result["available"] is False
    assert result["price"] == 120 and result["currency"] == "EUR"
    assert result["matched_option"]["name"] == "Deluxe room"
    assert result["javascript_rendered"] is True
    assert result["language"] == "ja" and "'ja'" in ctx.systems[0]
    assert "deluxe room in October" in ctx.prompts[0]
    assert result["eligibility_notes"] == ["Minimum 2 nights"]
    assert result["evidence"] == ["Sold out for October"]
    assert result["confidence"] == "medium"


def test_structured_data_on_the_rendered_dom_beats_the_model():
    ctx = _Ctx(
        fetches={URL: _fetch_value(URL, text=PLAIN_PAGE)},
        render={"url": URL, "final_url": URL, "title": "Wool Runner", "rendered": True,
                "text": "Wool Runner", "text_chars": 11, "truncated": False, "links": [],
                "html": JSONLD_PAGE},
        llm={"availability": "in_stock"})
    result = _run(ctx, variant="size 10")
    assert ctx.steps == ["fetch", "render"]  # no model call
    assert result["source"] == "jsonld" and result["availability"] == "out_of_stock"
    assert result["javascript_rendered"] is True


def test_a_page_that_cannot_be_read_is_unknown_and_never_reaches_the_model():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, status=404, text="")},
               render_error=W.runtime.PermanentProviderError("404; nothing to extract"),
               llm={"availability": "in_stock"})
    result = _run(ctx)
    assert ctx.steps == ["fetch", "render"]
    assert result["source"] == "none" and result["availability"] == "unknown"
    assert result["available"] is None and result["http_status"] == 404
    assert result["confidence"] == "low"
    assert any("404" in e for e in result["evidence"])


def test_the_model_cannot_invent_an_availability_value():
    ctx = _Ctx(fetches={URL: _fetch_value(URL, text=PLAIN_PAGE)},
               render={"url": URL, "final_url": URL, "title": "x", "rendered": False,
                       "text": "some text", "text_chars": 9, "truncated": False, "links": [],
                       "html": PLAIN_PAGE},
               llm={"availability": "probably", "price": "lots", "options": "none",
                    "evidence": "n/a", "confidence": "certain"})
    result = _run(ctx)
    assert result["availability"] == "unknown" and result["price"] is None
    assert result["options"] == [] and result["confidence"] == "low"


# --- the pieces --------------------------------------------------------------

def test_option_matching_prefers_an_exact_name_then_token_overlap():
    match = _commerce().match_option
    options = [{"name": "Natural Black / 10", "sku": "NB10"}, {"name": "Natural Black / 10.5", "sku": "NB105"},
               {"name": "Tree Green / 9", "sku": "TG9"}]
    assert match("natural black / 10", options)["sku"] == "NB10"
    assert match("size 10", options)["sku"] == "NB10"
    assert match("tree green 9", options)["sku"] == "TG9"
    assert match("10.5 natural", options)["sku"] == "NB105"
    assert match("size 13", options) is None
    assert match(None, options) is None and match("size 10", []) is None


def test_price_parsing_handles_locales_and_symbols():
    number = _commerce()._number
    assert number("1.234,56") == 1234.56
    assert number("1,234.56") == 1234.56
    assert number("¥12,800") == 12800
    assert number("110") == 110 and number(110) == 110.0 and number("$1,299") == 1299
    assert number("abc") is None and number(None) is None and number(True) is None


def test_the_schema_org_availability_vocabulary_is_mapped():
    to_ours = _commerce()._schema_availability
    assert to_ours("https://schema.org/InStock") == "in_stock"
    assert to_ours("http://schema.org/SoldOut") == "out_of_stock"
    assert to_ours("PreOrder") == "preorder"
    assert to_ours("https://schema.org/LimitedAvailability") == "limited"
    assert to_ours("https://schema.org/Discontinued") == "discontinued"
    assert to_ours("") == "unknown" and to_ours(None) == "unknown"


def test_bad_input_is_an_invalid_request_before_any_fetch():
    validate = _commerce().validate
    for bad in ({"url": "http://169.254.169.254/latest/meta-data/"},
                {"url": URL, "quantity": 0}, {"url": URL, "quantity": "2"},
                {"url": URL, "ship_to": "USA"}, {"url": URL, "variant": ""},
                {"url": URL, "language": "english"}):
        with pytest.raises(W.runtime.InvalidRequest):
            validate(bad)
    assert validate({"url": URL, "ship_to": "jp", "variant": " size 9 ", "language": "pt-BR"}) == {
        "url": URL, "variant": "size 9", "quantity": None, "ship_to": "JP", "language": "pt-BR"}


# --- the route, the tool, the contract ----------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_commerce", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    # This environment has no Google credential; the bee needs Gemini for its
    # last layer, so make it deliverable here (the layers under test are faked).
    monkeypatch.setattr(W.providers.google_auth, "configured", lambda: True)
    # Re-binding the router re-runs web.configure(); keep the SSRF guard main
    # injected, or every URL is refused as unguarded.
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _gate_must_not_run(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)


def _serve_from_fixture(monkeypatch):
    """The real skill over the JSON-LD fixture, in place of a live fetch."""
    async def skill(ctx, payload):
        return await _commerce().check(_Ctx(fetches={payload["url"]: _fetch_value(payload["url"], text=JSONLD_PAGE)}), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


def test_the_catalog_row_is_priced_and_shaped_as_designed():
    worker = W.catalog.BY_NAME[WORKER]
    assert worker.path == PATH and worker.price_usd == 0.50 and worker.tier == "standard"
    assert worker.input_schema["required"] == ["url"]
    assert set(worker.input_schema["properties"]) == {"url", "variant", "quantity", "ship_to", "language"}
    assert worker.input_schema["additionalProperties"] is False
    assert set(worker.requires) == {"web", "gemini"}
    assert worker.tool_name == "work_commerce_availability"
    assert W.catalog.example_for(worker)["url"].startswith("https://")


def test_always_current_bees_publish_a_required_checked_at():
    """The always-current contract: a worker that reports on the live world
    stamps when it looked, and the schema makes that non-optional."""
    for name in ("commerce.availability",):
        schema = W.catalog.contract.OUTPUT_SCHEMAS[name]
        assert "checked_at" in schema["required"], name
        assert schema["properties"]["checked_at"]["format"] == "date-time", name


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    _gate_must_not_run(monkeypatch)
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"url": HTTP_URL, "quantity": 0})
    assert response.status_code == 400
    body = response.json()
    assert body["reason"] == "invalid_request" and body["billed"] is False
    assert "quantity" in body["detail"]
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"url": HTTP_URL, "language": "english"})
    assert response.status_code == 400 and "language" in response.json()["detail"]


def test_an_unpaid_call_is_a_402_at_the_catalog_price_with_a_runnable_example(client):
    response = client.post(PATH, json={"url": URL})
    assert response.status_code == 402
    body = response.json()
    assert int(body["accepts"][0].get("maxAmountRequired") or body["accepts"][0].get("amount")) == 500_000
    example = body["extensions"]["bazaar"]["info"]["input"]["body"]
    assert example["url"].startswith("https://") and "variant" in example


def test_a_paid_http_call_delivers_the_envelope_and_passes_the_delivery_contract(client, monkeypatch):
    _serve_from_fixture(monkeypatch)
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"url": HTTP_URL, "variant": "size 9", "quantity": 2, "ship_to": "US"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok" and body["worker"] == WORKER and body["price_usd"] == 0.50
    result = body["result"]
    assert result["availability"] == "in_stock" and result["source"] == "jsonld"
    assert result["quantity_ok"] is True and result["ship_to_ok"] is True
    assert W.catalog.contract.check(W.catalog.BY_NAME[WORKER].output_schema, result) is None
    assert body["receipt_url"].startswith("/work/receipts/")
    receipt = client.get(body["receipt_url"])
    assert receipt.status_code == 200
    assert receipt.json()["request"]["worker"] == WORKER
    assert receipt.json()["execution"]["status"] == "ok"


def test_the_tool_is_sold_on_mcp_from_the_same_row(app_module, client):
    tools = {t["name"]: t for t in app_module._mcp_tools()}
    assert TOOL in tools
    tool = tools[TOOL]
    worker = W.catalog.BY_NAME[WORKER]
    assert tool["title"] == worker.title
    assert "$0.50 per call" in tool["description"]
    assert tool["inputSchema"]["properties"] == worker.input_schema["properties"]
    assert tool["outputSchema"] == W.catalog.response_schema(worker)
    assert tool["annotations"]["readOnlyHint"] is True
    manifest = client.get("/mcp.json").json()
    served = next(t for t in manifest["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.50}
    card = next(e for e in client.get("/.well-known/ard.json").json()["entries"]
                if e["identifier"].endswith(":mcp:hubvibe"))
    assert TOOL in card["capabilities"]


def test_the_static_manifests_on_disk_describe_the_same_tool(app_module):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    worker = W.catalog.BY_NAME[WORKER]
    for path in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
        on_disk = next(t for t in json.loads(path.read_text())["tools"] if t["name"] == TOOL)
        for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
            assert on_disk[field] == live[field], f"{path.name} {field} is stale"
        assert on_disk["httpEndpoint"] == {"method": "POST", "path": PATH,
                                           "price_usd": worker.price_usd}


def test_a_paid_mcp_call_returns_the_routes_envelope(client, monkeypatch):
    _serve_from_fixture(monkeypatch)
    response = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"url": HTTP_URL, "variant": "size 10"}}})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False
    envelope = result["structuredContent"]
    assert envelope["worker"] == WORKER
    assert envelope["result"]["availability"] == "out_of_stock"
    assert envelope["receipt_url"].startswith("/work/receipts/")
