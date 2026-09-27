"""opendata.search -- Asia-Pacific open-data portals in one call.

Pinned: the CKAN response shape (captured from data.gov.au / data.e-gov.go.jp
/ Tokyo / data.gov.hk on 2026-09-26) parsed into one dataset record with
its resources; the fan-out over a region with a failing portal disclosed
in `portals_failed` rather than hidden; no portal answering is an unbilled
failure; input refused before the gate; the route, the MCP tool and the
static manifests sell the same row; checked_at stamped per call.
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
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "opendata.search", "/work/opendata/search", "hubvibe_opendata_search"

CKAN_RESPONSE = {"help": "https://data.e-gov.go.jp/data/api/3/action/help_show?name=package_search", "success": True,
                 "result": {"count": 4699, "results": [
                     {"id": "5b2f1c7e-8a3d-4c1e-9f2a-1b2c3d4e5f60", "name": "kokusei-2020", "title": "令和2年国勢調査 人口等基本集計",
                      "notes": "国勢調査の人口等基本集計です。" * 3, "metadata_modified": "2026-03-01T09:12:44.512345",
                      "organization": {"name": "soumu", "title": "総務省"}, "license_title": "CC-BY-4.0",
                      "url": "https://www.e-stat.go.jp/stat-search/files?toukei=00200521",
                      "tags": [{"name": "人口"}, {"name": "国勢調査"}],
                      "resources": [{"id": "r1", "url": "https://www.e-stat.go.jp/file-download?id=1", "format": "CSV",
                                     "name": "全国結果", "last_modified": "2026-02-20T00:00:00", "size": 1048576},
                                    {"id": "r2", "url": "", "format": "HTML"},
                                    {"id": "r3", "url": "https://www.e-stat.go.jp/file-download?id=3", "format": None}]},
                     {"id": "x2", "name": "older", "title": "人口動態", "metadata_modified": "2025-01-01T00:00:00",
                      "organization": None, "resources": []}]}}


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


class _Ctx:
    """Serves each portal's step from a fixture: a value, or an error to raise."""

    def __init__(self, answers):
        self.answers = answers  # portal id -> dict value | Exception
        self.steps, self.providers_used, self.attempts = [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        provider = providers[0]
        answer = self.answers.get(provider.portal_id)
        if isinstance(answer, Exception):
            raise answer

        class _P:
            id = provider.id

            async def search(self, query, rows=10, start=0):
                return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)

        result = await call(_P())
        self.providers_used.append(provider.id)
        return result.value


def _parsed(portal_id="data.e-gov.go.jp"):
    return {"portal": portal_id, "count": CKAN_RESPONSE["result"]["count"],
            "results": [W.providers.opendata._package(p, portal_id) for p in CKAN_RESPONSE["result"]["results"]]}


def test_a_ckan_package_becomes_one_dataset_record_with_usable_resources():
    record = W.providers.opendata._package(CKAN_RESPONSE["result"]["results"][0], "data.e-gov.go.jp")
    assert record["portal"] == "data.e-gov.go.jp" and record["region"] == "jp" and record["country"] == "JP"
    assert record["title"] == "令和2年国勢調査 人口等基本集計" and record["organization"] == "総務省"
    assert record["license"] == "CC-BY-4.0" and record["tags"] == ["人口", "国勢調査"]
    assert record["landing_url"].startswith("https://www.e-stat.go.jp/")
    # the resource without a URL is dropped; a null format is kept as null
    assert [r["url"] for r in record["resources"]] == ["https://www.e-stat.go.jp/file-download?id=1",
                                                       "https://www.e-stat.go.jp/file-download?id=3"]
    assert record["resources"][0] == {"url": "https://www.e-stat.go.jp/file-download?id=1", "format": "CSV",
                                      "name": "全国結果", "last_modified": "2026-02-20T00:00:00", "size_bytes": 1048576}
    assert record["resources"][1]["format"] is None and record["resource_count"] == 3
    empty = W.providers.opendata._package(CKAN_RESPONSE["result"]["results"][1], "data.gov.hk")
    assert empty["organization"] is None and empty["resources"] == [] and empty["description"] is None


def test_the_portal_table_is_the_verified_keyless_set():
    portals = W.providers.opendata.PORTALS
    assert len(portals) == 16 and {"data.gov.au", "data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp", "data.gov.hk",
                                    "data.bodik.jp", "data.go.kr", "ckan.publishing.service.gov.uk", "open.canada.ca",
                                    "data.europa.eu", "govdata.de", "dati.gov.it", "data.gov.ie", "opendata.swiss",
                                    "data.overheid.nl", "data.gov.il", "datos.gob.cl"} == set(portals)
    assert W.providers.opendata.REGIONS == ["au", "ca", "ch", "cl", "de", "eu", "gb", "hk", "ie", "il", "it", "jp", "kr", "nl"]
    assert W.providers.opendata.portals_for("jp") == ["data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp", "data.bodik.jp"]
    assert W.providers.opendata.portals_for("kr") == ["data.go.kr"]
    assert W.providers.opendata.portals_for("all") == list(portals)
    assert all(p.available() for p in W.providers.opendata.PROVIDERS)


def test_a_region_fans_out_and_a_failing_portal_is_disclosed_not_hidden():
    S = W.skills.opendata
    ctx = _Ctx({"data.e-gov.go.jp": _parsed("data.e-gov.go.jp"),
                "catalog.data.metro.tokyo.lg.jp": W.runtime.TransientProviderError("catalog.data.metro.tokyo.lg.jp timed out"),
                "data.bodik.jp": W.runtime.TransientProviderError("data.bodik.jp timed out")})
    r = asyncio.run(S.search(ctx, {"query": " 人口 ", "region": "jp", "limit": 5}))
    assert sorted(ctx.steps) == ["search:catalog.data.metro.tokyo.lg.jp", "search:data.bodik.jp", "search:data.e-gov.go.jp"]
    assert r["query"] == "人口" and r["region"] == "jp"
    assert r["portals_searched"] == ["data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp", "data.bodik.jp"]
    assert r["portals_ok"] == ["data.e-gov.go.jp"]
    assert r["portals_failed"] == [{"portal": "catalog.data.metro.tokyo.lg.jp", "reason": "catalog.data.metro.tokyo.lg.jp timed out"},
                                   {"portal": "data.bodik.jp", "reason": "data.bodik.jp timed out"}]
    assert r["total_matches"] == {"data.e-gov.go.jp": 4699}
    assert r["result_count"] == 2 and [x["id"] for x in r["results"]] == ["5b2f1c7e-8a3d-4c1e-9f2a-1b2c3d4e5f60", "x2"]  # newest first
    assert r["limit_per_portal"] == 5 and r["checked_at"].endswith("Z")
    assert W.catalog.contract.check(W.catalog.BY_NAME[WORKER].output_schema, r) is None


def test_no_portal_answering_is_an_unbilled_failure_not_an_empty_success():
    S = W.skills.opendata
    ctx = _Ctx({"data.gov.hk": W.runtime.TransientProviderError("data.gov.hk returned 503")})
    with pytest.raises(W.runtime.TransientProviderError) as exc:
        asyncio.run(S.search(ctx, {"query": "weather", "region": "hk"}))
    assert exc.value.reason == "no_sources" and "data.gov.hk returned 503" in exc.value.detail


def test_specific_portals_can_be_named_and_bad_input_is_refused_before_the_gate():
    S = W.skills.opendata
    assert S.parse({"query": "x", "portals": ["data.gov.au", "data.gov.au"]})["portals"] == ["data.gov.au"]
    assert S.parse({"query": "x"})["portals"] == list(W.providers.opendata.PORTALS)
    for bad in ({}, {"query": " "}, {"query": "x" * 301}, {"query": "x", "region": "sg"},
                {"query": "x", "limit": 0}, {"query": "x", "limit": 51}, {"query": "x", "portals": []},
                {"query": "x", "portals": ["data.gov.sg"]}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.precheck(bad)


# --- the route, the tool, the contract --------------------------------------------

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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_opendata", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
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


def _serve_from_fixture(monkeypatch):
    async def skill(ctx, payload):
        req = W.skills.opendata.parse(payload)
        return await W.skills.opendata.search(_Ctx({pid: _parsed(pid) for pid in req["portals"]}), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


def test_the_row_is_priced_keyless_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.10 and w.tier == "utility" and w.available()
    schema = W.catalog.contract.OUTPUT_SCHEMAS[WORKER]
    assert "checked_at" in schema["required"] and "portals_failed" in schema["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert W.skills.PRECHECKS.get(w.skill) is not None


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"query": "x", "region": "sg"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    _serve_from_fixture(monkeypatch)
    response = client.post(PATH, json={"query": "人口", "region": "jp"})
    assert response.status_code == 402
    assert int(response.json()["accepts"][0].get("maxAmountRequired") or response.json()["accepts"][0].get("amount")) == 100_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"query": "人口", "region": "jp", "limit": 5})
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.10
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    assert envelope["result"]["portals_ok"] == ["data.e-gov.go.jp", "catalog.data.metro.tokyo.lg.jp", "data.bodik.jp"]
    receipt = client.get(envelope["receipt_url"]).json()
    assert receipt["request"]["worker"] == WORKER and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"query": "rainfall", "region": "au"}}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["portals_ok"] == ["data.gov.au"]


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    worker = W.catalog.BY_NAME[WORKER]
    assert live["outputSchema"] == W.catalog.response_schema(worker) and "$0.10 per call" in live["description"]
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.10}
    for static in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
        on_disk = next(t for t in json.loads(static.read_text())["tools"] if t["name"] == TOOL)
        for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
            assert on_disk[field] == live[field], f"{static.name} {field} is stale"
