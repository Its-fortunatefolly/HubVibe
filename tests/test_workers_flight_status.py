"""travel.flight_status -- airport and flight status from FAA, Avinor, NOAA and adsb.lol.

Pinned on the answers of 2026-09-28: SFO's FAA ground delay program (low
ceilings, avg 44 / max 120 min) with its runway configuration; Oslo's
Avinor board with the required "Flight data from Avinor" credit; a METAR;
adsb.lol aircraft sorted by distance; a quiet US airport reads "normal";
a non-covered airport says so; input refused before the gate; paid HTTP +
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
WORKER, PATH, TOOL = "travel.flight_status", "/work/travel/flight_status", "hubvibe_travel_flight_status"

SFO_EVENT = {"airportId": "SFO", "groundStop": None, "airportClosure": None, "freeForm": None, "arrivalDelay": None,
             "departureDelay": None, "deicing": None,
             "groundDelay": {"impactingCondition": "low ceilings", "avgDelay": 44.0, "maxDelay": 120,
                             "startTime": "2026-09-28T15:00:00Z", "endTime": "2026-09-28T23:29:00Z"},
             "airportConfig": {"arrivalRunwayConfig": "28L/28R NOISE", "departureRunwayConfig": "28L/28R", "arrivalRate": 30}}
OSL_BOARD = {"last_update": "2026-09-28T12:29:26.891757Z", "flights": [
    {"flight": "SK344", "airline": "SK", "direction": "D", "other_airport": "TRD", "scheduled": "2026-09-28T11:15:00Z",
     "status": "departed", "status_time": "2026-09-28T11:22:38Z", "gate": "A20", "check_in": "4-6", "belt": None,
     "delayed": False, "sector": "domestic"},
    {"flight": "TP763", "airline": "TP", "direction": "D", "other_airport": "LIS", "scheduled": "2026-09-28T10:50:00Z",
     "status": "departed", "status_time": "2026-09-28T11:22:24Z", "gate": "D3", "check_in": "1-2", "belt": None,
     "delayed": True, "sector": "schengen"},
    {"flight": "WF414", "airline": "WF", "direction": "A", "other_airport": "BGO", "scheduled": "2026-09-28T11:05:00Z",
     "status": "arrived", "status_time": "2026-09-28T10:58:38Z", "gate": None, "check_in": None, "belt": "3",
     "delayed": False, "sector": "domestic"}]}
METAR = {"rawOb": "METAR ENGM 281220Z 17008KT 9999 -RA BKN012 14/12 Q1017", "fltCat": "MVFR", "temp": 14, "dewp": 12,
         "wdir": 170, "wspd": 8, "visib": "6+", "wxString": "-RA", "reportTime": "2026-09-28T12:20:00.000Z"}
TAF = {"rawTAF": "TAF ENGM 281100Z 2812/2912 18010KT 9999 BKN015"}
AIRCRAFT = [{"hex": "far", "flight": "FAR1    ", "lat": 60.6, "lon": 11.1, "alt_baro": 30000, "gs": 400},
            {"hex": "4cac79", "flight": "SAS51D  ", "r": "EI-SIJ", "t": "A20N", "lat": 60.25, "lon": 11.1, "alt_baro": 8000,
             "gs": 250, "track": 76.8, "baro_rate": -800, "squawk": "6220"},
            {"hex": "gnd", "flight": "NAX1", "lat": 60.19, "lon": 11.10, "alt_baro": "ground", "gs": 5}]


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
FS = W.skills.flight_status
AV = W.providers.aviation


class _Ctx:
    def __init__(self, event=SFO_EVENT, fail=()):
        self.event, self.fail, self.steps = event, set(fail), []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        if step in self.fail:
            raise W.runtime.TransientProviderError(f"{step} source returned 429")
        outer = self

        class _P:
            id = providers[0].id

            async def events(self, faa_id):
                return W.runtime.ProviderResult(value={"entry": outer.event if outer.event and outer.event["airportId"] == faa_id else None,
                                                       "airports_with_events": 6})

            async def board(self, iata, direction, back, ahead):
                return W.runtime.ProviderResult(value={"flights": [dict(f) for f in OSL_BOARD["flights"]],
                                                       "last_update": OSL_BOARD["last_update"]})

            async def weather(self, icao):
                return W.runtime.ProviderResult(value={"metar": METAR, "taf": TAF})

            async def around(self, lat, lon, nm):
                return W.runtime.ProviderResult(value=AIRCRAFT)

            async def callsign(self, cs):
                return W.runtime.ProviderResult(value=[AIRCRAFT[1]] if cs == "SAS51D" else [])
        return (await call(_P())).value


def _status(body, **kw):
    ctx = _Ctx(**kw)
    return asyncio.run(FS.status(ctx, body)), ctx


def test_sfo_carries_the_faa_ground_delay_and_runways_with_weather_and_traffic():
    out, ctx = _status({"airport": "SFO"})
    d = out["delays"]
    assert d["status"] == "ground_delay" and d["ground_delay"]["avg_minutes"] == 44 and d["ground_delay"]["max_minutes"] == 120
    assert d["runways"] == {"arrival": "28L/28R NOISE", "departure": "28L/28R", "arrival_rate_per_hour": 30}
    assert out["board"] is None and out["coverage"] == {"delays": True, "board": False, "weather": True, "aircraft": True}
    assert out["weather"]["flight_category"] == "MVFR" and out["aircraft"][0]["distance_km"] <= out["aircraft"][-1]["distance_km"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_oslo_board_is_credited_to_avinor_and_filters_by_flight_and_direction():
    out, _ = _status({"airport": "OSL", "direction": "departures"})
    assert [f["flight"] for f in out["board"]["departures"]] == ["TP763", "SK344"] and out["board"]["arrivals"] == []
    assert out["board"]["attribution"] == "Flight data from Avinor" and out["board"]["attribution_url"] == "https://www.avinor.no"
    assert {"text": "Flight data from Avinor", "url": "https://www.avinor.no"} in out["attribution"]
    assert out["board"]["departures"][1]["other_airport_name"].startswith("Trondheim")
    assert out["delays"] is None
    out, _ = _status({"airport": "ENGM", "flight": "SK344"})
    assert [f["flight"] for f in out["board"]["departures"]] == ["SK344"]
    grounded = next(a for a in out["aircraft"] if a["hex"] == "gnd")
    assert grounded["on_ground"] is True and grounded["altitude_ft"] is None
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_quiet_us_airport_is_normal_and_other_countries_are_told_their_coverage():
    out, _ = _status({"airport": "JFK"})
    assert out["delays"]["status"] == "normal" and out["delays"]["ground_delay"] is None
    out, _ = _status({"airport": "NRT"})
    assert out["delays"] is None and out["board"] is None and out["weather"] and out["notes"]


def test_a_callsign_gets_its_aircraft_and_a_silent_one_is_explained():
    out, ctx = _status({"callsign": "SAS51D"})
    assert out["aircraft"][0]["registration"] == "EI-SIJ" and out["airport"] is None and ctx.steps == ["callsign"]
    out, _ = _status({"callsign": "XYZ999"})
    assert out["aircraft"] == [] and "not transmitting" in out["notes"][0]


def test_a_failing_source_is_named_and_the_rest_ships():
    out, _ = _status({"airport": "OSL"}, fail={"aircraft"})
    assert out["sections_failed"] == ["aircraft"] and out["board"]["departures"] and out["weather"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_input_is_checked_and_the_airport_table_resolves_both_codes():
    for bad in ({}, {"airport": "ZZ"}, {"airport": "QQQ"}, {"callsign": "a b"}, {"airport": "OSL", "hours_ahead": 20},
                {"airport": "OSL", "direction": "up"}):
        with pytest.raises(W.runtime.InvalidRequest):
            FS.precheck(bad)
    assert AV.airport("OSL")["icao"] == "ENGM" and AV.airport("kjfk")["iata"] == "JFK"


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.25 and w.tier == "standard" and w.requires == ["aviation"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"flight": "SK344"}, w.input_schema)
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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_flight", MAIN_PATH)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"airport": "QQQ"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.flight_status.status(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"airport": "SFO"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 250_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["delays"]["status"] == "ground_delay"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["airport"]["icao"] == "KSFO"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.25}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
