"""The delivery contract: a result is billed only if it matches the output
schema the route publishes. A result that does not is an unbilled failure
(502, reason contract_mismatch) on every transport -- the worker routes,
the audit routes, and the MCP audit tools -- and the ledger says so."""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
POINTS = {"points": [[1, 2.1], [2, 3.9], [3, 6.2], [4, 7.8], [5, 10.1]], "predict_x": [6]}


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


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    workers = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    workers.ledger.reset_for_tests()
    workers.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_contract", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workers = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    workers.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for)
    yield module
    workers.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_the_check_itself_reads_the_published_schema(app_module):
    contract = app_module.workers.catalog.contract
    schema = {"type": "object", "properties": {"pass": {"type": "boolean"}}, "required": ["pass"]}
    assert contract.check(schema, {"pass": True, "extra": 1}) is None
    assert contract.check(schema, {"pass": "yes"}) == "pass: 'yes' is not of type 'boolean'"
    assert contract.check(schema, {}).startswith("(root): 'pass' is a required property")
    assert contract.check(None, {"anything": 1}) is None


def test_a_worker_result_off_its_schema_is_a_502_that_is_not_billed(app_module, client, monkeypatch):
    workers = app_module.workers

    async def wrong_shape(ctx, payload):
        return {"nothing": "like the contract"}

    monkeypatch.setitem(workers.router.REGISTRY, "stats.probability", wrong_shape)
    response = client.post("/work/stats/probability", json=POINTS, headers={"X-API-Key": "test-key"})
    assert response.status_code == 502
    body = response.json()
    assert body["reason"] == "contract_mismatch" and body["billed"] is False
    assert "output schema" in body["detail"]
    receipt = client.get(f"/work/receipts/{body['receipt_id']}").json()
    assert receipt["outcome"] == "unpaid_failed"
    assert receipt["execution"]["failure_reason"] == "contract_mismatch"
    assert receipt["execution"]["failure_stage"] == "deliver"
    assert receipt["delivery"]["delivered"] is False


def test_a_worker_result_on_its_schema_is_still_delivered(app_module, client):
    response = client.post("/work/stats/probability", json=POINTS, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["result"]["linear_regression"]


def test_an_audit_result_off_its_schema_is_a_502_that_is_not_billed(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module.audits, "run_security_audit",
                        lambda url, response=None: {"status": "ok"})  # no `pass`
    billed = []
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: billed.append(price_usd))
    response = client.post("/audit/security", json={"url": "https://example.com"},
                           headers={"X-API-Key": "test-key"})
    assert response.status_code == 502
    body = response.json()
    assert body["billed"] is False and "output schema" in body["detail"]
    assert billed == []


def test_an_mcp_audit_tool_result_off_its_schema_is_an_unbilled_error_result(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "_mcp_run_tool", lambda name, args: {"status": "ok", "pass": "maybe"})
    billed = []
    monkeypatch.setattr(app_module, "_bill", lambda auth, price_usd: billed.append(price_usd))
    response = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "audit_seo", "arguments": {"url": "https://example.com"}}})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "contract_mismatch" in result["content"][0]["text"]
    assert billed == []


def test_the_guarantee_is_published_where_agents_read(app_module, client):
    manifest = client.get("/.well-known/agent.json").json()
    assert "contract_mismatch" in manifest["guarantees"][0]
    assert "contract_mismatch" in client.get("/llms.txt").text
