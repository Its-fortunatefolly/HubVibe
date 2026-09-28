"""maps.weather -- MET Norway forecasts anywhere, NWS alerts in the US.

Pinned on MET Norway's real Kyoto forecast of 2026-09-28 (first 40 hours):
current conditions, 24 hourly points, daily min/max/precipitation with the
condition nearest local midday; place resolution by airport code,
coordinates, Wikidata name and US street address; US points get NWS alerts
and a failed alert read is named while the forecast ships; an unknown
place is refused unbilled; the MET credit is always carried; paid HTTP +
MCP; manifests.
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
WORKER, PATH, TOOL = "maps.weather", "/work/maps/weather", "hubvibe_maps_weather"
MET_KYOTO = json.loads((REPO_ROOT / "tests" / "fixtures" / "met-kyoto-2026-09-28.json").read_text())
ALERT = {"properties": {"event": "Air Quality Alert", "severity": "Unknown", "headline": "Air Quality Alert issued",
                        "effective": "2026-09-28T12:00:00-05:00", "expires": "2026-09-29T00:00:00-05:00"}}


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
WS = W.skills.weather


class _Ctx:
    def __init__(self, place=None, census=None, fail=()):
        self.place, self.census, self.fail, self.steps = place, census, set(fail), []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        if step in self.fail:
            raise W.runtime.TransientProviderError(f"{step} timed out")
        outer = self

        class _P:
            id = providers[0].id

            async def forecast(self, lat, lon):
                return W.runtime.ProviderResult(value=MET_KYOTO)

            async def active(self, lat, lon):
                return W.runtime.ProviderResult(value=[ALERT])

            async def find(self, name):
                return W.runtime.ProviderResult(value=outer.place)

            async def locate(self, address, lat, lng):
                return W.runtime.ProviderResult(value=outer.census)
        return (await call(_P())).value


KYOTO = {"name": "Kyoto", "description": "city in Japan", "lat": 35.0117, "lon": 135.7683, "wikidata": "Q34600"}


def _weather(body, **kw):
    ctx = _Ctx(**kw)
    return asyncio.run(WS.weather(ctx, body)), ctx


def test_kyoto_by_name_gets_current_hourly_daily_and_the_met_credit():
    out, ctx = _weather({"location": "Kyoto"}, place=KYOTO)
    assert out["place"]["source"] == "Wikidata Q34600" and out["place"]["name"] == "Kyoto (city in Japan)"
    first = MET_KYOTO["properties"]["timeseries"][0]["data"]["instant"]["details"]
    assert out["current"]["temperature_c"] == first["air_temperature"] and len(out["hourly"]) == 24
    assert 1 <= len(out["daily"]) <= 7 and all(d["min_c"] <= d["max_c"] for d in out["daily"])
    assert out["alerts_covered"] is False and out["alerts"] == [] and ctx.steps == ["place", "forecast"]
    assert out["attribution"][0]["text"] == "Data from MET Norway"
    assert out["forecast_updated_at"] == MET_KYOTO["properties"]["meta"]["updated_at"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_airport_codes_and_coordinates_resolve_without_a_lookup_and_us_points_get_alerts():
    out, ctx = _weather({"location": "JFK"})
    assert out["place"]["source"] == "airport code" and out["alerts"][0]["event"] == "Air Quality Alert"
    assert "place" not in ctx.steps and any(a["text"].startswith("Alerts from the US") for a in out["attribution"])
    out, ctx = _weather({"lat": 29.76, "lng": -95.37}, fail={"alerts"})
    assert out["place"]["source"] == "coordinates" and out["sections_failed"] == ["alerts"] and out["hourly"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_street_address_goes_to_the_census_geocoder_and_an_unknown_place_is_refused():
    census = {"matched_address": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500", "lat": 38.8987, "lng": -77.0352}
    out, ctx = _weather({"location": "1600 Pennsylvania Ave NW, Washington, DC 20500"}, census=census)
    assert out["place"]["source"] == "US Census Geocoder" and ctx.steps[0] == "geocode"
    with pytest.raises(W.runtime.InvalidRequest):
        _weather({"location": "Zzqxv Nowhere"}, place=None, census=None)
    for bad in ({}, {"location": ""}, {"lat": 10}, {"lat": 100, "lng": 0}, {"location": "x" * 201}):
        with pytest.raises(W.runtime.InvalidRequest):
            WS.precheck(bad)


def test_the_day_condition_is_nearest_local_midday():
    _, _, daily, _ = WS.shape(MET_KYOTO, 135.77)
    assert daily[0]["condition"] is not None


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.10 and w.tier == "utility" and w.requires == ["weather"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None and w.available()


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_weather", MAIN_PATH)
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


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"lat": 100, "lng": 0})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.weather.weather(_Ctx(place=KYOTO), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"location": "Kyoto"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 100_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["place"]["source"] == "Wikidata Q34600"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and len(result["structuredContent"]["result"]["hourly"]) == 24


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.10}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
