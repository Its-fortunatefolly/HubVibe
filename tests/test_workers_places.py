"""maps.places -- places near a location from Overture Maps' open data.

Pinned on the Overture rows BigQuery returned on 2026-09-28 near the San
Francisco Ferry Building: the query split into what and where, the center
resolved, category matches ranked before name-only matches, source datasets
kept per place and the Overture credit on every answer; nothing close widens
the radius once; the SQL escapes what it is given; input refused before the
gate; paid HTTP + MCP; manifests.
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
WORKER, PATH, TOOL = "maps.places", "/work/maps/places", "hubvibe_maps_places"

ROWS = [
    {"id": "8333e7a5-b1ca-4c61-969d-5543d8de977a", "name": "Red Bay Coffee Ferry Building", "basic_category": "coffee_shop",
     "category": "coffee_shop", "operating_status": "open", "confidence": "0.9416600465774536", "meters": "59.0",
     "lat": "37.79534503", "lon": "-122.3930613", "street": "Ferry Building, 1", "city": "San Francisco", "region": "CA",
     "postcode": "94111", "country": "US", "website": "http://redbaycoffee.com", "phone": None, "brand": "Red Bay Coffee",
     "datasets": "meta,Overture,Overture-signals"},
    {"id": "0b02c811-2723-4f3e-b25a-4d408c7cfbac", "name": "Frog Hollow Farm", "basic_category": "coffee_shop",
     "category": "coffee_shop", "operating_status": "open", "confidence": "0.9199122190475464", "meters": "62.0",
     "lat": "37.795122", "lon": "-122.393187", "street": "1 Ferry Building, Shop # 46", "city": "San Francisco",
     "region": None, "postcode": None, "country": "US", "website": "http://www.froghollow.com", "phone": "(415) 983-8000",
     "brand": None, "datasets": "Foursquare,Overture"},
]
FERRY = {"name": "San Francisco Ferry Building", "description": "building in San Francisco", "lat": 37.7955,
         "lon": -122.3937, "wikidata": "Q1060289"}


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
MP, PL = W.skills.maps, W.providers.places


@pytest.fixture(autouse=True)
def _bigquery_available(monkeypatch):
    monkeypatch.setattr(W.providers.places._Overture, "available", lambda self: True)


class _Ctx:
    def __init__(self, rows=ROWS, empty_first=False):
        self.rows, self.empty_first, self.steps, self.calls = rows, empty_first, [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def find(self, name):
                outer.calls.append(("find", name))
                return W.runtime.ProviderResult(value=FERRY)

            async def nearby(self, lat, lon, radius, words, limit):
                outer.calls.append(("nearby", radius, tuple(words), limit))
                if outer.empty_first and len([c for c in outer.calls if c[0] == "nearby"]) == 1:
                    return W.runtime.ProviderResult(value=[])
                return W.runtime.ProviderResult(value=[PL.place(r) for r in outer.rows])
        return (await call(_P())).value


def _places(body, **kw):
    ctx = _Ctx(**kw)
    return asyncio.run(MP.places(ctx, body)), ctx


def test_what_and_where_are_split_and_the_answer_carries_sources_and_the_overture_credit():
    out, ctx = _places({"query": "coffee near the Ferry Building, San Francisco"})
    assert out["what"] == "coffee" and ("find", "Ferry Building, San Francisco") in ctx.calls
    assert ("nearby", 1000, ("coffee",), 10) in ctx.calls
    first = out["places"][0]
    assert first["name"] == "Red Bay Coffee Ferry Building" and first["distance_m"] == 59 and first["sources"] == ["meta"]
    assert first["address"] == "Ferry Building, 1, San Francisco, CA, 94111" and out["places"][1]["sources"] == ["Foursquare"]
    assert out["attribution"][0]["url"] == "https://docs.overturemaps.org/attribution/" and out["place_count"] == 2
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_nothing_close_widens_the_radius_once_and_says_so():
    out, ctx = _places({"query": "pharmacy", "lat": 35.6595, "lng": 139.7005, "radius_m": 500}, empty_first=True)
    assert [c[1] for c in ctx.calls if c[0] == "nearby"] == [500, 2000] and out["radius_m"] == 2000
    assert "widened" in out["notes"][0] and out["near"]["source"] == "coordinates"


def test_keywords_drop_filler_and_the_sql_escapes_input():
    assert PL.keywords("the best italian restaurants") == ["italian", "restaurant"]
    sql = PL.build_sql(37.7955, -122.3937, 1000, ["o'brien%"], 5)
    assert "o\\'brien\\%" in sql and "ST_DWITHIN(geometry, ST_GEOGPOINT(-122.3937, 37.7955), 1000)" in sql
    assert "ORDER BY IF(" in sql and "LIMIT 5" in sql and "permanently_closed" in sql


def test_input_is_checked_before_the_gate():
    for bad in ({}, {"query": ""}, {"query": "coffee"}, {"query": "coffee", "lat": 10}, {"query": "x near y", "radius_m": 20},
                {"query": "x near y", "limit": 0}):
        with pytest.raises(W.runtime.InvalidRequest):
            MP.precheck_places(bad)
    assert MP.parse_places({"query": "ramen", "near": "Shibuya Station"})["where"] == "Shibuya Station"


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.10 and w.tier == "utility" and w.requires == ["places"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_places", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(W.providers.places._Overture, "available", lambda self: True)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"query": "coffee"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.maps.places(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"query": "coffee near the Ferry Building, San Francisco"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 100_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["place_count"] == 2
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["places"][0]["category"] == "coffee_shop"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.10}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
