"""property.context -- the public record for a US address, one call.

Pinned on the answers the federal services gave on 2026-09-28 for 4600
Silver Hill Rd (the Census Bureau's own address, tract 24033802405): the
geography, the FEMA National Risk Index row with its mandatory notice, the
bundled ACS and FHFA tract rows, HUD metro and ZIP rents, NCES schools
sorted by distance, EPA walkability; a failing source is named in
sections_failed while the rest ships; an unmatched address is refused
unbilled; input refused before the gate; paid HTTP + MCP; manifests.
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
WORKER, PATH, TOOL = "property.context", "/work/property/context", "hubvibe_property_context"
ADDRESS = "4600 Silver Hill Rd, Washington, DC 20233"

GEOS = {
    "Census Tracts": [{"GEOID": "24033802405", "NAME": "Census Tract 8024.05"}],
    "Counties": [{"GEOID": "24033", "NAME": "Prince George's County"}],
    "States": [{"GEOID": "24", "NAME": "Maryland"}],
    "Census Block Groups": [{"GEOID": "240338024052"}],
    "Census Designated Places": [{"GEOID": "2475725", "NAME": "Suitland CDP"}],
    "2020 Census ZIP Code Tabulation Areas": [{"GEOID": "20746", "BASENAME": "20746"}],
    "Metropolitan Statistical Areas": [{"GEOID": "47900", "NAME": "Washington-Arlington-Alexandria, DC-VA-MD-WV Metro Area"}],
    "120th Congressional Districts": [{"GEOID": "2404", "NAME": "Congressional District 4"}],
    "Unified School Districts": [{"GEOID": "2400510", "NAME": "Prince George's County Public Schools"}],
}
PLACE = {"matched_address": "4600 SILVER HILL RD, WASHINGTON, DC, 20233", "lng": -76.92836638093,
         "lat": 38.84505589808, "geographies": GEOS, "candidates": 1}
NRI_ROW = {"RISK_RATNG": "Very Low", "RISK_SCORE": 7.406086118939744, "EAL_VALT": 401271.4842413712,
           "SOVI_RATNG": "Relatively High", "RESL_RATNG": "Relatively Low", "NRI_VER": "December 2025",
           "IFLD_RISKR": "Very Low", "CFLD_RISKR": None, "HWAV_RISKR": "Relatively Moderate", "HRCN_RISKR": "Relatively Low"}
FMR_ROW = {"FMR_AREANAME": "Washington-Arlington-Alexandria, DC-VA-MD HUD Metro FMR Area", "FMR_0BDR": 1953,
           "FMR_1BDR": 2015, "FMR_2BDR": 2246, "FMR_3BDR": 2835, "FMR_4BDR": 3332}
SAFMR_ROW = {"SAFMR_0BR": 1800, "SAFMR_1BR": 1860, "SAFMR_2BR": 2070, "SAFMR_3BR": 2610, "SAFMR_4BR": 3070}
SCHOOLS = [
    {"SCH_NAME": "Far School", "SCHOOL_LEVEL": "High", "GSLO": "09", "GSHI": "12", "TOTAL": 1400, "STUTERATIO": 18.2,
     "CHARTER_TEXT": "No", "LEA_NAME": "Prince George's County Public Schools", "LCITY": "Suitland", "_lat": 38.86, "_lng": -76.90},
    {"SCH_NAME": "Suitland Elementary", "SCHOOL_LEVEL": "Elementary", "GSLO": "PK", "GSHI": "05", "TOTAL": 514,
     "STUTERATIO": 14.94, "CHARTER_TEXT": "No", "LEA_NAME": "Prince George's County Public Schools", "LCITY": "Suitland",
     "_lat": 38.85313699981943, "_lng": -76.91920899952252},
]
WALK_ROW = {"NatWalkInd": 11.333333333333332, "D4A": 926.71, "GEOID20": "240338024051"}


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
PR = W.skills.property
PD = W.providers.property_data


class _Ctx:
    def __init__(self, place=PLACE, fail=()):
        self.place, self.fail, self.steps = place, set(fail), []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        if step in self.fail:
            raise W.runtime.TransientProviderError(f"{step} source timed out")
        outer = self

        class _P:
            id = providers[0].id

            async def locate(self, address, lat, lng):
                return W.runtime.ProviderResult(value=outer.place, cost_micros=0, cost_measured=True)

            async def tract(self, fips):
                assert fips == "24033802405"
                return W.runtime.ProviderResult(value=NRI_ROW)

            async def metro(self, lng, lat):
                return W.runtime.ProviderResult(value=FMR_ROW)

            async def zip_area(self, zcta):
                assert zcta == "20746"
                return W.runtime.ProviderResult(value=SAFMR_ROW)

            async def near(self, lng, lat, radius):
                return W.runtime.ProviderResult(value=SCHOOLS if step == "schools" else [])

            async def index(self, lng, lat):
                return W.runtime.ProviderResult(value=WALK_ROW)
        return (await call(_P())).value


def _context(body=None, **kw):
    ctx = _Ctx(**kw)
    return asyncio.run(PR.context(ctx, body or {"address": ADDRESS})), ctx


def test_the_census_bureaus_address_gets_every_section_with_femas_notice():
    out, ctx = _context()
    assert out["geography"]["tract"] == "24033802405" and out["geography"]["zip"] == "20746"
    assert out["geography"]["congressional_district"] == "Congressional District 4"
    risk = out["hazard_risk"]
    assert risk["overall_rating"] == "Very Low" and risk["expected_annual_loss_usd"] == 401271
    assert risk["hazards"]["riverine_flooding"] == "Very Low" and risk["hazards"]["coastal_flooding"] is None
    assert len(risk["hazards"]) == 18 and risk["notice"].startswith("This product uses the Federal Emergency")
    assert out["housing"]["median_home_value"] == 332200 and out["housing"]["median_year_built"] == 1969
    assert out["price_trend"] == {"latest_year": 2024, "annual_change_pct": -4.64, "five_year_change_pct": 39.51,
                                  "source": "FHFA House Price Index, annual, census tract"}
    assert out["fair_market_rent"]["two_bedroom"] == 2246 and out["fair_market_rent"]["zip"]["two_bedroom"] == 2070
    assert [s["name"] for s in out["schools"]] == ["Suitland Elementary", "Far School"]
    assert out["schools"][0]["distance_km"] == pytest.approx(1.0, abs=0.2)
    assert out["walkability"] == {"index": 11.33, "meters_to_transit": 926.71, "block_group": "240338024051"}
    assert out["sections_failed"] == [] and out["superfund_sites"] == []
    assert ctx.steps[0] == "geocode"
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_source_that_fails_is_named_and_the_rest_still_ships():
    out, _ = _context(fail={"risk", "walkability"})
    assert sorted(out["sections_failed"]) == ["risk", "walkability"]
    assert out["hazard_risk"] is None and out["walkability"] is None
    assert out["housing"]["median_home_value"] == 332200 and out["schools"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_an_unmatched_address_is_refused_and_input_is_checked_before_the_gate():
    with pytest.raises(W.runtime.InvalidRequest):
        _context(place=None)
    for bad in ({}, {"address": "x"}, {"lat": 38.8}, {"lat": 51.5, "lng": -0.12}, {"address": ADDRESS, "school_radius_km": 50}):
        with pytest.raises(W.runtime.InvalidRequest):
            PR.precheck(bad)
    PR.precheck({"lat": 29.762, "lng": -95.379})


def test_the_bundled_tables_and_the_distance_math():
    assert PD.acs_tract("24033802405")["median_gross_rent"] == 1486
    assert PD.fhfa_tract("24033802405")["latest_year"] == 2024
    assert PD.acs_tract("00000000000") is None
    assert PD.distance_km(38.845, -76.928, 38.845, -76.928) == 0
    assert PD.distance_km(40.7128, -74.0060, 34.0522, -118.2437) == pytest.approx(3936, abs=5)


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.25 and w.tier == "standard" and w.requires == ["property_data"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    jsonschema.validate({"lat": 29.762, "lng": -95.379}, w.input_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"lat": 29.762}, w.input_schema)
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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_property", MAIN_PATH)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"lat": 51.5, "lng": -0.12})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.property.context(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"address": ADDRESS}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 250_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["housing"]["median_home_value"] == 332200
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["geography"]["zip"] == "20746"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.25}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
