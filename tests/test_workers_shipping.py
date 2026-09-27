"""commerce.shipping -- the store's own shipping options, eligibility and
cart total for a product, keyless on any Shopify storefront.

Pinned: the storefront shapes captured 2026-09-27 (product .js variants,
cart/add.js item, cart.js totals, shipping_rates.json rows, the 422 error
objects for an unsupported country and a missing state); variant choice by
name, sku or first available; eligibility true/false/null with the store's
reasons and the missing address fields; estimated total before tax; a
refused add; input refused before the gate; paid HTTP + MCP; manifests.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "commerce.shipping", "/work/commerce/shipping", "hubvibe_commerce_shipping"
URL = "https://www.allbirds.com/products/mens-wool-runners"

PRODUCT = {"id": 4344001101904, "title": "Men's Wool Runners", "handle": "mens-wool-runners", "variants": [
    {"id": 31330825076816, "title": "8", "option1": "8", "sku": "WR3MNCW080", "price": 11000, "available": False, "requires_shipping": True},
    {"id": 31330825109584, "title": "9", "option1": "9", "sku": "WR3MNCW090", "price": 11000, "available": True, "requires_shipping": True},
    {"id": 31330825142352, "title": "10", "option1": "10", "sku": "WR3MNCW100", "price": 11000, "available": True, "requires_shipping": True}]}
ITEM = {"id": 31330825109584, "quantity": 1, "variant_id": 31330825109584, "title": "Men's Wool Runner - Natural Grey (Light Grey Sole)",
        "product_title": "Men's Wool Runner - Natural Grey (Light Grey Sole)", "variant_title": "9", "price": 11000, "final_price": 11000,
        "sku": "WR3MNCW090", "requires_shipping": True, "taxable": True, "grams": 500}
CART = {"token": "hWNHKDZk", "item_count": 1, "items_subtotal_price": 11000, "total_price": 11000, "currency": "USD", "requires_shipping": True}
RATES = [{"name": "Ground Shipping", "presentment_name": "Ground Shipping", "code": "Ground Shipping", "price": "0.00", "markup": "0.00",
          "source": "shopify", "delivery_date": None, "delivery_range": None, "delivery_days": [], "currency": "USD", "description": "",
          "carrier_identifier": None},
         {"name": "Express", "price": "25.00", "currency": "USD", "delivery_days": [1, 2], "description": "Next business day", "carrier_identifier": "ups"}]
UNSUPPORTED = {"country": ["Country/region not supported"], "province": ["Select a prefecture"]}
NEEDS_STATE = {"province": ["Select a state"], "zip": ["Enter a ZIP code"]}


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
S = W.skills.shipping
P = W.providers.shopify_cart


@pytest.fixture(autouse=True)
def _target_guard(monkeypatch):
    def guard(url):
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host.startswith(("169.254.", "10.", "127.", "localhost", "192.168.")):
            return "must not point at a private, loopback or link-local address"
        return None
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason", guard)


def _run(coro):
    return asyncio.run(coro)


class _Ctx:
    def __init__(self, product=None, quote=None):
        self.product = PRODUCT if product is None else product
        self.quote = quote if quote is not None else {"added": True, "add_error": None, "item": ITEM, "cart": CART, "rates": RATES, "errors": None, "rates_status": 200}
        self.calls = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        outer = self

        class _Pv:
            id = providers[0].id

            async def product(self, url):
                outer.calls.append(("product", url))
                return W.runtime.ProviderResult(value=outer.product, cost_micros=0, cost_measured=True)

            async def quote(self, product_url, variant_id, quantity, ship_to):
                outer.calls.append(("quote", variant_id, quantity, ship_to))
                return W.runtime.ProviderResult(value=outer.quote, cost_micros=0, cost_measured=True)

        return (await call(_Pv())).value


def test_product_page_urls_are_recognised_and_input_is_validated_before_the_gate():
    assert P.product_js_url(URL) == "https://www.allbirds.com/products/mens-wool-runners.js"
    assert P.product_js_url("https://shop.example/en-us/collections/all/products/thing-1/") == "https://shop.example/products/thing-1.js"
    assert P.product_js_url("https://shop.example/pages/about") is None and P.store_base(URL) == "https://www.allbirds.com"
    ok = S.parse({"url": URL, "ship_to": {"country": "us", "province": "NY", "postal_code": "10001"}})
    assert ok["ship_to"] == {"country": "US", "province": "NY", "postal_code": "10001"} and ok["quantity"] == 1
    assert S.parse({"url": URL, "ship_to": {"country": "United States"}})["ship_to"]["country"] == "United States"
    for bad in ({}, {"url": URL}, {"url": "https://shop.example/pages/about", "ship_to": {"country": "US"}},
                {"url": URL, "ship_to": {}}, {"url": URL, "ship_to": {"country": " "}}, {"url": URL, "ship_to": {"country": "US"}, "quantity": 0},
                {"url": URL, "ship_to": {"country": "US"}, "quantity": 11}, {"url": URL, "ship_to": {"country": "US"}, "variant": ""},
                {"url": "http://10.0.0.1/products/x", "ship_to": {"country": "US"}}, {"url": URL, "ship_to": {"country": "US"}, "language": "bad tag!"}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.parse(bad)


def test_variant_choice_by_name_sku_or_first_available():
    assert S.choose_variant(PRODUCT, "size 10")[0]["id"] == 31330825142352
    assert S.choose_variant(PRODUCT, "WR3MNCW090")[0]["id"] == 31330825109584
    v, note = S.choose_variant(PRODUCT, None)
    assert v["id"] == 31330825109584 and "first available" in note
    v, note = S.choose_variant({"variants": [{"id": 1, "title": "Default Title", "available": False}]}, None)
    assert v["id"] == 1 and "No variant is available" in note
    with pytest.raises(W.runtime.InvalidRequest):
        S.choose_variant(PRODUCT, "size 99 purple")
    with pytest.raises(W.runtime.PermanentProviderError):
        S.choose_variant({"variants": []}, None)


def test_a_supported_destination_returns_the_stores_options_and_an_estimated_total():
    ctx = _Ctx()
    r = _run(S.shipping(ctx, {"url": URL, "variant": "9", "ship_to": {"country": "US", "province": "NY", "postal_code": "10001"}}))
    assert ctx.calls[1] == ("quote", 31330825109584, 1, {"country": "US", "province": "NY", "postal_code": "10001"})
    assert r["eligible"] is True and r["eligibility_reasons"] == [] and r["missing_fields"] == []
    assert [o["name"] for o in r["shipping_options"]] == ["Ground Shipping", "Express"]
    assert r["shipping_options"][1] == {"name": "Express", "price": 25.0, "currency": "USD", "delivery_days_min": 1, "delivery_days_max": 2,
                                        "description": "Next business day", "carrier": "ups"}
    assert r["cheapest_shipping"] == {"name": "Ground Shipping", "price": 0.0, "currency": "USD"}
    assert r["cart"] == {"subtotal": 110.0, "total": 110.0, "currency": "USD", "item_count": 1, "requires_shipping": True}
    assert r["estimated_total"] == {"amount": 110.0, "currency": "USD", "includes_tax": False}
    assert r["variant"] == {"id": 31330825109584, "name": "9", "sku": "WR3MNCW090", "price": 110.0, "available": True, "requires_shipping": True}
    assert r["store"]["domain"] == "www.allbirds.com" and r["notes"] == []
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_an_unsupported_country_is_a_clear_no_with_the_stores_reason():
    quote = {"added": True, "add_error": None, "item": ITEM, "cart": CART, "rates": None, "errors": UNSUPPORTED, "rates_status": 422}
    r = _run(S.shipping(_Ctx(quote=quote), {"url": URL, "ship_to": {"country": "JP", "province": "Tokyo", "postal_code": "150-0002"}}))
    assert r["eligible"] is False and "country: Country/region not supported" in r["eligibility_reasons"]
    assert r["shipping_options"] == [] and r["cheapest_shipping"] is None and r["estimated_total"] is None
    assert r["cart"]["subtotal"] == 110.0 and any("first available" in n for n in r["notes"])
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_missing_state_is_null_eligibility_with_the_fields_to_add():
    quote = {"added": True, "add_error": None, "item": ITEM, "cart": CART, "rates": None, "errors": NEEDS_STATE, "rates_status": 422}
    r = _run(S.shipping(_Ctx(quote=quote), {"url": URL, "variant": "10", "ship_to": {"country": "US"}}))
    assert r["eligible"] is None and r["missing_fields"] == ["province", "postal_code"]
    assert any("add province, postal_code" in n for n in r["notes"])
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_refused_add_is_reported_as_the_stores_no():
    quote = {"added": False, "add_error": "The product 'Men\\'s Wool Runners' is already sold out.", "item": None, "cart": None, "rates": None, "errors": None, "rates_status": None}
    r = _run(S.shipping(_Ctx(quote=quote), {"url": URL, "variant": "8", "ship_to": {"country": "US", "province": "NY", "postal_code": "10001"}}))
    assert r["eligible"] is False and r["eligibility_reasons"][0].startswith("The store refused to add") and r["cart"] is None
    assert r["variant"]["available"] is False
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


# --- the route, the tool, the contract ---------------------------------------

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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_shipping", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
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


def test_the_row_is_priced_on_the_ladder_keyless_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.50 and w.tier == "standard" and w.available()
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"url": "https://shop.example/pages/about", "ship_to": {"country": "US"}})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.shipping.shipping(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"url": URL, "variant": "size 10", "ship_to": {"country": "US", "province": "NY", "postal_code": "10001"}}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 500_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.50 and envelope["result"]["eligible"] is True
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["cheapest_shipping"]["price"] == 0.0


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.50}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
