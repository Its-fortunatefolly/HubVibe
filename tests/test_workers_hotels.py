"""travel.hotels -- live hotel rates, any language in, sandbox never sold.

Pinned: the LiteAPI shapes captured 2026-09-27 (hotel content record; a
rates row with roomTypes[].rates[] carrying retailRate.total, taxesAndFees,
cancellationPolicies) normalised into hotels with cheapest rate and room
options; dates/guests/place validation before the gate; free-text place to
the provider's aiSearch; sort by price or rating; hotels without rates
disclosed; sandbox disclosed and fail-closed; paid HTTP + MCP; manifests.
"""

import asyncio
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "travel.hotels", "/work/travel/hotels", "hubvibe_travel_hotels"
TODAY = date(2026, 9, 27)

HOTELS = [
    {"id": "lp1db0a1", "name": "Shibuya Excel Hotel Tokyu", "stars": 4, "rating": 8.7, "reviewCount": 3120,
     "address": "1-12-2 Dogenzaka, Shibuya", "city": "Tokyo", "country": "JP", "latitude": 35.6586, "longitude": 139.7007},
    {"id": "lp6555ad8e", "name": "Villa Fontaine Grand Haneda Airport", "stars": 4, "rating": 9.1, "reviewCount": 900,
     "address": "Haneda Airport Terminal 3", "city": "Tokyo", "country": "JP", "latitude": 35.5494, "longitude": 139.7798},
    {"id": "lpnorates", "name": "No Rates Inn", "stars": 2, "rating": None, "reviewCount": None,
     "address": None, "city": "Tokyo", "country": "JP", "latitude": None, "longitude": None},
]
RATES = [
    {"hotelId": "lp1db0a1", "roomTypes": [
        {"offerId": "3gAYonJzkd4A", "rates": [
            {"rateId": "r1", "name": "Run of House, Non Smoking 2 Twin Beds", "boardName": "Room Only", "maxOccupancy": 2,
             "retailRate": {"total": [{"amount": 360.89, "currency": "USD"}],
                            "taxesAndFees": [{"included": False, "description": "VAT", "amount": 36.09, "currency": "USD"}]},
             "cancellationPolicies": {"refundableTag": "RFN", "cancelPolicyInfos": [
                 {"cancelTime": "2026-11-07 00:00:00", "amount": 360.89, "currency": "USD", "type": "amount"},
                 {"cancelTime": "2026-11-05 00:00:00", "amount": 180.45, "currency": "USD", "type": "amount"}]}},
            {"rateId": "r2", "name": "Suite", "boardName": "Breakfast", "maxOccupancy": 3,
             "retailRate": {"total": [{"amount": 816.05, "currency": "USD"}], "taxesAndFees": []},
             "cancellationPolicies": {"refundableTag": "NRFN", "cancelPolicyInfos": []}}]}]},
    {"hotelId": "lp6555ad8e", "roomTypes": [
        {"offerId": "off2", "rates": [
            {"rateId": "r3", "name": "Standard", "boardName": "Room Only", "maxOccupancy": 2,
             "retailRate": {"total": [{"amount": 210.00, "currency": "USD"}], "taxesAndFees": []},
             "cancellationPolicies": {"refundableTag": "XYZ", "cancelPolicyInfos": []}}]}]},
]


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
H = W.skills.hotels


def _run(coro):
    return asyncio.run(coro)


class _Ctx:
    def __init__(self, hotels=None, rates=None, sandbox=True, total=169):
        self.hotels = HOTELS if hotels is None else hotels
        self.rates = RATES if rates is None else rates
        self.sandbox, self.total, self.calls = sandbox, total, []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        outer = self

        class _P:
            id = providers[0].id

            async def hotels(self, **params):
                outer.calls.append(("hotels", params))
                return W.runtime.ProviderResult(value={"hotels": outer.hotels, "total": outer.total}, cost_micros=0, cost_measured=True)

            async def rates(self, hotel_ids, checkin, checkout, occupancies, currency="USD", guest_nationality="US"):
                outer.calls.append(("rates", hotel_ids, checkin, checkout, occupancies, currency, guest_nationality))
                return W.runtime.ProviderResult(value={"rates": outer.rates, "sandbox": outer.sandbox}, cost_micros=0, cost_measured=True)

        return (await call(_P())).value


def test_place_dates_and_guests_are_validated_before_the_gate():
    ok = H.parse({"place": "hotel near Shibuya station Tokyo"}, today=TODAY)
    assert ok["check_in"] == "2026-10-27" and ok["check_out"] == "2026-10-29" and ok["nights"] == 2 and ok["adults"] == 2
    ok = H.parse({"city": "Seoul", "country_code": "kr", "check_in": "2026-11-10", "check_out": "2026-11-13", "currency": "krw"}, today=TODAY)
    assert ok["nights"] == 3 and ok["country_code"] == "KR" and ok["currency"] == "KRW"
    assert H.parse({"latitude": 35.6, "longitude": 139.7, "days_ahead": 1}, today=TODAY)["radius_m"] == 5000
    for bad in ({}, {"city": "Tokyo"}, {"place": "x", "check_in": "2026-01-01"}, {"place": "x", "check_in": "2026-11-10", "days_ahead": 3},
                {"place": "x", "check_out": "2026-11-10", "nights": 2}, {"place": "x", "check_in": "2026-11-10", "check_out": "2026-11-10"},
                {"place": "x", "nights": 31}, {"place": "x", "adults": 0}, {"place": "x", "children_ages": [18]},
                {"place": "x", "currency": "US"}, {"place": "x", "guest_nationality": "USA"}, {"place": "x", "sort": "cheap"},
                {"latitude": 35.6}, {"latitude": 95, "longitude": 0}, {"place": "x", "country_code": "JPN"}):
        with pytest.raises(W.runtime.InvalidRequest):
            H.parse(bad, today=TODAY)


def test_real_rates_become_hotels_with_cheapest_rate_room_options_and_cancellation_terms():
    ctx = _Ctx()
    result = _run(H.hotels(ctx, {"place": "hotel near Shibuya station Tokyo", "check_in": "2026-11-10", "nights": 2, "adults": 2,
                                 "children_ages": [8], "max_hotels": 3}))
    assert ctx.calls[0][0] == "hotels" and ctx.calls[0][1]["ai_search"] == "hotel near Shibuya station Tokyo" and ctx.calls[0][1]["limit"] == 3
    assert ctx.calls[1][1] == ["lp1db0a1", "lp6555ad8e", "lpnorates"] and ctx.calls[1][2:5] == ("2026-11-10", "2026-11-12", [{"adults": 2, "children": [8]}])
    assert result["hotel_count"] == 2 and result["hotels_without_rates"] == 1 and result["hotels_matched"] == 169
    first = result["hotels"][0]
    assert first["name"] == "Villa Fontaine Grand Haneda Airport" and first["cheapest"]["total"] == 210.0 and first["cheapest"]["refundable"] is None
    second = result["hotels"][1]
    assert second["cheapest"] == {"total": 360.89, "currency": "USD", "board": "Room Only", "refundable": True,
                                  "cancel_free_until": "2026-11-05 00:00:00", "offer_id": "3gAYonJzkd4A"}
    room = second["rooms"][0]
    assert room["taxes_and_fees"] == [{"description": "VAT", "amount": 36.09, "currency": "USD", "included": False}]
    assert [c["from"] for c in room["cancellation"]] == ["2026-11-05 00:00:00", "2026-11-07 00:00:00"]
    assert second["rooms"][1]["refundable"] is False and second["room_options_available"] == 2
    assert result["cheapest_total"] == {"amount": 210.0, "currency": "USD", "hotel": "Villa Fontaine Grand Haneda Airport"}
    assert result["live_mode"] is False and any(n.startswith("Sandbox rates") for n in result["notes"])
    assert any("No Rates Inn" in n for n in result["notes"]) and result["checked_at"].endswith("Z")
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_sort_by_rating_and_a_production_key_is_live():
    result = _run(H.hotels(_Ctx(sandbox=False), {"city": "Tokyo", "country_code": "JP", "days_ahead": 5, "sort": "rating"}))
    assert [h["name"] for h in result["hotels"]] == ["Villa Fontaine Grand Haneda Airport", "Shibuya Excel Hotel Tokyu"]
    assert result["live_mode"] is True and not any(n.startswith("Sandbox") for n in result["notes"])


def test_no_match_is_a_delivered_empty_answer_with_a_note():
    result = _run(H.hotels(_Ctx(hotels=[], total=0), {"place": "Atlantis", "days_ahead": 3}))
    assert result["hotels"] == [] and result["hotel_count"] == 0 and result["cheapest_total"] is None
    assert result["notes"] == ["No hotel matched the place given."]
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_the_provider_is_unavailable_on_a_sandbox_key_unless_allowed_on_purpose(monkeypatch):
    provider = W.providers.liteapi.PROVIDERS[0]
    monkeypatch.delenv("LITEAPI_KEY", raising=False)
    monkeypatch.delenv("WORKER_LITEAPI_ALLOW_SANDBOX", raising=False)
    assert provider.available() is False and "not set" in provider.unavailable_reason()
    monkeypatch.setenv("LITEAPI_KEY", "sand_abc")
    assert provider.available() is False and "sandbox" in provider.unavailable_reason()
    monkeypatch.setenv("WORKER_LITEAPI_ALLOW_SANDBOX", "1")
    assert provider.available() is True
    monkeypatch.delenv("WORKER_LITEAPI_ALLOW_SANDBOX")
    monkeypatch.setenv("LITEAPI_KEY", "prod_abc")
    assert provider.available() is True and W.catalog.BY_NAME[WORKER].available()


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
    monkeypatch.setenv("LITEAPI_KEY", "prod_test_only")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_hotels", MAIN_PATH)
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


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.50 and w.tier == "standard" and w.requires == ["liteapi"]
    schema = W.catalog.contract.OUTPUT_SCHEMAS[WORKER]
    assert "checked_at" in schema["required"] and "live_mode" in schema["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"city": "Tokyo"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.hotels.hotels(_Ctx(sandbox=False), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"place": "hotel near Shibuya station Tokyo", "days_ahead": 30, "nights": 2, "max_hotels": 3}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 500_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.50 and envelope["result"]["hotel_count"] == 2
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    receipt = client.get(envelope["receipt_url"]).json()
    assert receipt["request"]["worker"] == WORKER and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": {"place": "명동 서울", "days_ahead": 30}}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["query"]["place"] == "명동 서울"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.50}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
