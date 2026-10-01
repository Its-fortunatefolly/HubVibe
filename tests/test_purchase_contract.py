"""VERIFY before PAY: the Purchase Verification Contract.

Pinned: a buyer can GET a machine-readable contract for every capability;
it names the exact capability, price, asset, network and pay-to; its
schemas are the same objects openapi.json, the MCP tools and the A2A card
publish; its rails are the same terms the 402 offers; the hash is
deterministic and moves when a term moves; POST .../verify passes a
matching expectation and fails every kind of mismatch; and a paid request
that binds to a contract the route no longer sells is refused before the
payment layer is touched.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from openapi_refs import resolved

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "workers" / ".." / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
BASE = "https://audit.example.test"


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


def _drop_sibling_cache():
    """main.py caches its sibling modules in sys.modules; x402_payments reads
    its facilitator and pay-to at import, so a copy loaded by an earlier test
    under another environment would leave x402 off here."""
    for name in [k for k in sys.modules if k.startswith("wcag_audit_engine_") and k != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    _drop_sibling_cache()
    workers = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    workers.ledger.reset_for_tests()
    workers.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_purchase", MAIN_PATH.resolve())
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
    _drop_sibling_cache()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _v2(response):
    raw = response.headers.get("PAYMENT-REQUIRED")
    assert raw, "no PAYMENT-REQUIRED header"
    return json.loads(base64.b64decode(raw))


# --- DISCOVER: the contract exists for every capability ---------------------------


def test_every_sold_capability_has_a_contract(app_module, client):
    listing = client.get("/contracts").json()
    ids = {row["id"] for row in listing["capabilities"]}
    assert {"audit.wcag", "audit.seo", "audit.security", "audit.performance", "audit.bundle"} <= ids
    assert {w.name for w in app_module.workers.catalog.live()} <= ids
    assert listing["count"] == len(ids) == 5 + len(app_module.workers.catalog.live())
    for row in listing["capabilities"]:
        assert row["contract_hash"].startswith("sha256:")
        assert row["contract_url"] == f"{BASE}/contracts/{row['id']}"


def test_the_contract_names_the_exact_purchase(client):
    contract = client.get("/contracts/market.quote").json()
    cap = contract["capability"]
    assert cap["id"] == "market.quote" and cap["kind"] == "worker"
    assert cap["path"] == "/work/market/quote" and cap["url"] == f"{BASE}/work/market/quote"
    assert cap["mcp_tool"] == "hubvibe_market_quote" and cap["a2a_skill"] == "hubvibe_market_quote"
    assert contract["provider"]["name"] == "HubVibe" and contract["provider"]["base_url"] == BASE
    assert contract["price"] == {"usd": 0.02, "currency": "USD", "model": "per-call"}
    rails = contract["payment"]["rails"]
    x402 = [r for r in rails if r["protocol"] == "x402"]
    assert x402, "no x402 rail"
    for rail in x402:
        assert rail["pay_to"] == PAY_TO
        assert rail["amount_atomic"] == "20000"
        assert rail["asset"] and rail["network"]
    assert {r["network"] for r in x402} >= {"eip155:8453", "base"}  # v2 CAIP-2 and the v1 legacy name
    assert contract["contract_hash"].startswith("sha256:")
    assert contract["input_schema_hash"].startswith("sha256:")
    assert contract["issued_at"].endswith("Z")
    assert contract["proof"]["receipt"] == f"{BASE}/work/receipts/{{receipt_id}}"


def test_the_same_contract_is_reachable_by_every_name(client):
    by_name = client.get("/contracts/market.quote").json()
    by_tool = client.get("/contracts/hubvibe_market_quote").json()
    by_path = client.get("/contracts/work/market/quote").json()
    assert by_name["contract_hash"] == by_tool["contract_hash"] == by_path["contract_hash"]
    audit = client.get("/contracts/audit_wcag").json()
    alias = client.get("/contracts/audit").json()
    assert audit["capability"]["id"] == "audit.wcag" and audit["capability"]["kind"] == "audit"
    assert audit["contract_hash"] == alias["contract_hash"]
    assert client.get("/contracts/no.such.capability").status_code == 404


# --- one source of truth ----------------------------------------------------------


def test_the_contract_carries_the_schemas_every_other_surface_publishes(app_module, client):
    contract = client.get("/contracts/stats.probability").json()
    tool = next(t for t in app_module._mcp_tools() if t["name"] == "hubvibe_predictive_probability_engine")
    worker = app_module.workers.catalog.BY_NAME["stats.probability"]
    assert contract["output_schema"] == worker.output_schema == tool["outputSchema"]["properties"]["result"]
    assert contract["input_schema"] == worker.input_schema
    openapi = client.get("/openapi.json").json()
    op = openapi["paths"]["/work/stats/probability"]["post"]
    assert resolved(openapi, op["requestBody"]["content"]["application/json"]["schema"]) == contract["input_schema"]
    assert op["x-payment-info"]["price"]["amount"] == "0.50" and contract["price"]["usd"] == 0.50
    card = client.get("/.well-known/agent-card.json").json()
    skill = next(s for s in card["skills"] if s["id"] == contract["capability"]["a2a_skill"])
    assert skill["description"] == tool["description"]


def test_the_contract_rails_are_the_402s_own_terms(client):
    contract = client.get("/contracts/market.quote").json()
    unpaid = client.post("/work/market/quote", json={"product_id": "BTC-USD"})
    assert unpaid.status_code == 402
    v2 = _v2(unpaid)
    for accepted in v2["accepts"]:
        rail = next(r for r in contract["payment"]["rails"]
                    if r["protocol"] == "x402" and r["x402_version"] == 2 and r["network"] == accepted["network"])
        assert rail["asset"] == accepted["asset"] and rail["pay_to"] == accepted["payTo"]
        assert rail["amount_atomic"] == str(accepted["amount"])
    v1 = unpaid.json()["accepts"][0]
    rail = next(r for r in contract["payment"]["rails"] if r["protocol"] == "x402" and r["x402_version"] == 1)
    assert rail["network"] == v1["network"] and rail["pay_to"] == v1["payTo"]
    assert rail["amount_atomic"] == str(v1["maxAmountRequired"])
    # The 402 names the contract it was priced from.
    assert unpaid.json()["contract"] == {
        "url": f"{BASE}/contracts/market.quote", "verify": f"{BASE}/contracts/market.quote/verify",
        "hash": contract["contract_hash"], "header": "X-HubVibe-Contract"}


def test_the_hash_is_deterministic_and_moves_with_the_terms(app_module, client):
    first = client.get("/contracts/market.quote").json()
    second = client.get("/contracts/market.quote").json()
    assert first["contract_hash"] == second["contract_hash"]
    assert first["issued_at"] <= second["issued_at"]
    worker = app_module.workers.catalog.BY_NAME["market.quote"]
    original = worker.price_usd
    try:
        worker.price_usd = 0.03
        moved = client.get("/contracts/market.quote").json()
    finally:
        worker.price_usd = original
    assert moved["price"]["usd"] == 0.03 and moved["contract_hash"] != first["contract_hash"]
    assert moved["input_schema_hash"] == first["input_schema_hash"]


# --- VERIFY: matching expectations pass, every mismatch fails ------------------------


def _verify(client, capability, expect=None, body=None):
    payload = {}
    if expect is not None:
        payload["expect"] = expect
    if body is not None:
        payload["input"] = body
    return client.post(f"/contracts/{capability}/verify", json=payload).json()


def test_a_matching_expectation_and_a_valid_input_verify(client):
    contract = client.get("/contracts/market.quote").json()
    result = _verify(client, "market.quote", expect={
        "capability_id": "market.quote", "url": f"{BASE}/work/market/quote", "price_usd": 0.02,
        "network": "eip155:8453", "asset": contract["payment"]["rails"][0]["asset"],
        "pay_to": PAY_TO.lower(), "amount_atomic": "20000", "protocol": "x402",
        "contract_hash": contract["contract_hash"], "provider": "HubVibe",
    }, body={"product_id": "BTC-USD"})
    assert result["verified"] is True and result["mismatches"] == []
    assert result["input"] == {"checked": True, "valid": True, "problem": None}
    assert result["contract"]["contract_hash"] == contract["contract_hash"]


@pytest.mark.parametrize("expect, field", [
    ({"price_usd": 0.03}, "price_usd"),
    ({"pay_to": "0x0000000000000000000000000000000000000001"}, "rail"),
    ({"network": "eip155:1"}, "rail"),
    ({"asset": "0x0000000000000000000000000000000000000002"}, "rail"),
    ({"amount_atomic": "10000"}, "rail"),
    ({"url": f"{BASE}/work/market/ticker"}, "url"),
    ({"capability_id": "market.ticker"}, "capability_id"),
    ({"contract_hash": "sha256:" + "0" * 64}, "contract_hash"),
    ({"provider": "SomeoneElse"}, "provider"),
    ({"not_a_field": 1}, "not_a_field"),
])
def test_every_kind_of_mismatch_fails_verification(client, expect, field):
    result = _verify(client, "market.quote", expect=expect, body={"product_id": "BTC-USD"})
    assert result["verified"] is False
    assert field in {m["field"] for m in result["mismatches"]}


def test_an_input_off_the_input_schema_fails_verification(client):
    result = _verify(client, "market.quote", body={})  # product_id is required
    assert result["verified"] is False and result["input"]["valid"] is False
    assert "product_id" in result["input"]["problem"]


def test_a_malformed_verify_body_is_a_400_not_a_pass(client):
    bad = client.post("/contracts/market.quote/verify", json=["not", "an", "object"])
    assert bad.status_code == 400 and bad.json()["reason"] == "invalid_request"
    unknown = client.post("/contracts/no.such/verify", json={})
    assert unknown.status_code == 404


# --- PAY only what was verified: the binding header -----------------------------


def test_a_payment_bound_to_a_contract_the_route_no_longer_sells_is_refused_untouched(app_module, client, monkeypatch):
    touched = []
    monkeypatch.setattr(app_module.x402_payments, "verify_only_sync",
                        lambda *a, **k: touched.append(a) or (_ for _ in ()).throw(AssertionError("payment layer reached")))
    response = client.post("/work/market/quote", json={"product_id": "BTC-USD"},
                           headers={"X-Payment": "signed-by-the-buyer", "X-HubVibe-Contract": "sha256:" + "0" * 64})
    assert response.status_code == 402
    body = response.json()
    assert body["error"] == "contract_mismatch" and body["billed"] is False
    assert body["contract"]["url"] == f"{BASE}/contracts/market.quote"
    assert touched == [], "the payment layer must not be reached"


def test_a_payment_bound_to_the_current_contract_proceeds(app_module, client, monkeypatch):
    contract = client.get("/contracts/market.quote").json()
    reached = []

    def verify(header, price=None, **kw):
        reached.append(price)
        return app_module.x402_payments.PendingPayment(None, None, price)

    monkeypatch.setattr(app_module.x402_payments, "verify_only_sync", verify)
    monkeypatch.setattr(app_module.x402_payments, "settle_sync", lambda pending: True)
    response = client.post("/work/market/quote", json={"product_id": "BTC-USD"},
                           headers={"X-Payment": "signed-by-the-buyer", "X-HubVibe-Contract": contract["contract_hash"]})
    assert reached == ["$0.02"], "the payment layer was reached exactly once, at the contract's price"
    assert response.status_code in (200, 502)  # the job's own outcome; the binding let it through


def test_the_binding_applies_to_audits_too(app_module, client, monkeypatch):
    touched = []
    monkeypatch.setattr(app_module.x402_payments, "verify_only_sync",
                        lambda *a, **k: touched.append(a) or (_ for _ in ()).throw(AssertionError("payment layer reached")))
    response = client.post("/audit/wcag", json={"url": "https://example.com"},
                           headers={"X-Payment": "signed", "X-HubVibe-Contract": "sha256:" + "1" * 64})
    assert response.status_code == 402 and response.json()["error"] == "contract_mismatch"
    assert touched == []


def test_the_manifest_points_buyers_at_the_contracts(client):
    manifest = client.get("/.well-known/agent.json").json()
    assert manifest["discovery"]["contracts"] == f"{BASE}/contracts"
    assert "X-HubVibe-Contract" in manifest["payment"]["purchase_verification"]
    assert any("contract_hash" in g for g in manifest["guarantees"])
