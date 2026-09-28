"""market.insiders -- insider trades parsed from the Form 4s filed with the SEC.

Pinned on EDGAR as it answered on 2026-09-28: NVIDIA's filing list (first
40 rows) and two real ownership documents -- an NVIDIA officer's 10b5-1
sales and an Apple officer's RSU vesting (derivative line, shares withheld
for tax). Filing list to trades to summary, the code filter, derivatives
off, a document that fails is retried and then named in notes, input
refused before the gate, paid HTTP + MCP, manifests.
"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
FIX = REPO_ROOT / "tests" / "fixtures"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "market.insiders", "/work/market/insiders", "hubvibe_market_insiders"

SUBMISSIONS = json.loads((FIX / "sec-submissions-nvda-2026-09-28.json").read_text())
SALE = (FIX / "form4-nvda-sale-2026.xml").read_bytes()
VESTING = (FIX / "form4-aapl-vesting-2026.xml").read_bytes()


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


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)


class _Sec:
    """Stands in for sec.gov: the filing list, then the sale document for the
    newest filing, the vesting document for the next, and the sale again."""

    def __init__(self, fail=()):
        self.fail, self.urls = dict(fail), []

    async def fetch(self, url, accept="application/json", client=None):
        self.urls.append(url)
        if "/submissions/" in url:
            return httpx.Response(200, json=SUBMISSIONS)
        if self.fail.get(url):
            self.fail[url] -= 1
            raise W.runtime.TransientProviderError(f"{url} unreachable: [Errno -3] Temporary failure in name resolution")
        body = VESTING if url.endswith("wk-form4_1790110579.xml") else SALE
        return httpx.Response(200, content=body)


@pytest.fixture
def sec(monkeypatch):
    fake = _Sec()
    S = W.providers.sec_edgar
    monkeypatch.setattr(S, "_fetch", fake.fetch)
    monkeypatch.setattr(S, "_ticker_map", {"NVDA": {"cik": 1045810, "title": "NVIDIA CORP"}})
    monkeypatch.setattr(S, "_ticker_map_at", 1e18)
    monkeypatch.setattr(W.skills.finance, "datetime", _Frozen)
    return fake


class _Ctx:
    async def run(self, step, providers, call, **kwargs):
        return (await call(providers[0])).value


def _insiders(body):
    return asyncio.run(W.skills.finance.insiders(_Ctx(), body))


def test_a_form4_parses_into_trades_with_owner_role_and_plan_flag():
    doc = W.providers.sec_edgar.parse_form4(SALE)
    assert doc["issuer_symbol"] == "NVDA" and doc["rule_10b5_1"] is True
    assert doc["owners"][0]["name"] == "Teter Timothy S." and doc["owners"][0]["officer"] is True
    first = doc["transactions"][0]
    assert (first["code"], first["shares"], first["price"], first["derivative"]) == ("S", 12483, 222.1932, False)
    vest = W.providers.sec_edgar.parse_form4(VESTING)
    codes = [(t["code"], t["derivative"]) for t in vest["transactions"]]
    assert codes == [("S", False), ("M", False), ("F", False), ("M", True)]
    assert vest["transactions"][3]["underlying_security"] == "Common Stock"


def test_filings_to_trades_to_summary(sec):
    out = _insiders({"symbol": "NVDA", "days": 30, "max_filings": 3})
    assert out["issuer_name"] == "NVIDIA CORP" and out["cik"] == 1045810 and out["filings_read"] == 3
    assert out["window"] == {"days": 30, "from": "2026-08-29", "to": "2026-09-28"} and out["as_of"] == "2026-09-23"
    first = out["transactions"][0]
    assert first["insider"] == "Teter Timothy S." and first["role"] == "EVP, General Counsel and Sec"
    assert first["code_meaning"] == "open-market or private sale" and first["value"] == round(12483 * 222.1932, 2)
    assert first["filing_url"].endswith("/000169684126000014/xslF345X06/wk-form4_1790196985.xml")
    sales = out["summary"]["open_market_sales"]
    assert sales["insiders"] == 2 and out["summary"]["open_market_purchases"]["transactions"] == 0
    assert out["summary"]["net_shares"] == -sales["shares"] and "Stopped at max_filings=3" in out["notes"][0]
    assert not any(u.endswith("primary_doc.xml") for u in sec.urls)  # Form 144 rows are not read
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_codes_and_derivatives_filter(sec):
    out = _insiders({"symbol": "NVDA", "days": 30, "max_filings": 2, "codes": ["M"], "include_derivatives": False})
    assert [(t["code"], t["derivative"]) for t in out["transactions"]] == [("M", False)]
    assert out["codes"] == ["M"]


def test_a_document_that_fails_is_retried_then_named(sec):
    first = "https://www.sec.gov/Archives/edgar/data/1045810/000169684126000014/wk-form4_1790196985.xml"
    sec.fail = {first: 2}
    assert _insiders({"symbol": "NVDA", "days": 30, "max_filings": 1})["filings_read"] == 1
    sec.fail = {first: 5}
    out = _insiders({"symbol": "NVDA", "days": 30, "max_filings": 2})
    assert out["filings_read"] == 1 and "1 of 2 Form 4 documents could not be read" in out["notes"][0]
    sec.fail = {first: 5}
    with pytest.raises(W.runtime.TransientProviderError):
        _insiders({"symbol": "NVDA", "days": 30, "max_filings": 1})


def test_input_is_checked_before_the_gate():
    for bad in ({}, {"symbol": "NVDA", "days": 0}, {"symbol": "NVDA", "days": 400}, {"symbol": "NVDA", "codes": ["Q"]},
                {"symbol": "NVDA", "codes": []}, {"symbol": "NVDA", "max_filings": 41},
                {"symbol": "NVDA", "include_derivatives": "yes"}, {"cik": "abc"}):
        with pytest.raises(W.runtime.InvalidRequest):
            W.skills.finance.precheck_insiders(bad)
    W.skills.finance.precheck_insiders({"cik": "1045810", "codes": ["p", "S"]})


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.10 and w.tier == "utility" and w.requires == ["sec_edgar"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None
    assert W.catalog.contract.REPRESENTATIVE_QUERIES[WORKER]


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_insiders", MAIN_PATH)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"symbol": "NVDA", "codes": ["Q"]})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    fake = _Sec()
    S = W.providers.sec_edgar
    monkeypatch.setattr(S, "_fetch", fake.fetch)
    monkeypatch.setattr(S, "_ticker_map", {"NVDA": {"cik": 1045810, "title": "NVIDIA CORP"}})
    monkeypatch.setattr(S, "_ticker_map_at", 1e18)
    monkeypatch.setattr(W.skills.finance, "datetime", _Frozen)
    body = {"symbol": "NVDA", "days": 30, "max_filings": 2}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 100_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["filings_read"] == 2
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["source"] == "sec-edgar-form4"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.10}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
