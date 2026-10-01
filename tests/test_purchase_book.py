"""The purchase book: one durable row per purchase, reconciled to the chain.

Why: on 2026-09-28 the owner asked who bought, what, and from where, and the
node could not answer -- /work rows kept a request HASH and no client, and
audit sales existed only as a log line every redeploy deleted (0x9b7dcf...
paid $0.05 for an audit on 09-14; 0x72c573... $0.15 on 09-27; both only on
chain). These tests pin: a paid audit and a paid worker call each write one
row with the request and the client; the client address is taken from the
proxy hop only; bookkeeping can never change a sale; failed jobs are booked
unbilled; owner-key calls are not sales; the owner pages are invisible to
everyone else; the CSV is safe to open; the summary separates outside sales,
the owner's own seeding and plain transfers; and reconcile finds, classifies
and backfills chain payments without ever inventing a request or a client.
"""

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"
MAIN_PATH = APP / "main.py"
PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BUYER = "0x728de2a85cb1372d713e3e7b71b5d068fecdc01d"
SEED = "0x104fea79f30b4fb4da86b6d65951217f914bdd35"
TX = "0x" + "ab" * 32

_SIBLINGS = ("billing", "x402_payments", "mpp_payments", "audits", "purchase_book")


def _drop_sibling_cache():
    for name in _SIBLINGS:
        sys.modules.pop(f"wcag_audit_engine_{name}", None)


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    _drop_sibling_cache()
    monkeypatch.setenv("AUDIT_API_KEY", "owner-key")
    monkeypatch.setenv("PURCHASE_OWNER_KEY", "books-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hubvibe-io.com")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    spec = importlib.util.spec_from_file_location("wcag_audit_main_purchase_book", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    W = module.workers
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    module.purchase_book.reset_for_tests()
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit, bill=module._bill,
        deliver=module._deliver, failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION, mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    monkeypatch.setattr(module, "_run_axe", lambda html, url: {"violations": [], "passes": [], "incomplete": []})
    yield module
    W.ledger.reset_for_tests()
    module.purchase_book.reset_for_tests()
    _drop_sibling_cache()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _pay_x402(app_module, monkeypatch, *, payer=BUYER, settle=True):
    X = app_module.x402_payments

    def verify(header, price=None, resource_url=None):
        payload = types.SimpleNamespace(x402_version=2, payload={
            "authorization": {"from": payer, "nonce": "0x" + "cd" * 32, "value": "50000"}})
        return X.PendingPayment(payload, [{"network": "eip155:8453", "asset": USDC, "amount": "50000",
                                           "payTo": PAY_TO}], price or "0.05")

    def settle(pending):
        if not settle:
            pending.settle_state = "refused"
            return False
        pending.settle_result = types.SimpleNamespace(success=True, transaction=TX, network="eip155:8453",
                                                      payer=payer, amount="50000")
        pending.settle_state = "settled"
        return True
    monkeypatch.setattr(X, "verify_only_sync", verify)
    monkeypatch.setattr(X, "settle_sync", settle)


def _rows(app_module):
    return app_module.purchase_book.query(limit=100)


def test_a_paid_audit_is_booked_with_its_request_and_client(app_module, client, monkeypatch):
    _pay_x402(app_module, monkeypatch)
    response = client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed", "User-Agent": "agent-sdk/1.0"},
                           json={"html": "<html lang='en'><body>hi</body></html>"})
    assert response.status_code == 200, response.text
    rows = _rows(app_module)
    assert len(rows) == 1
    row = rows[0]
    assert (row["product"], row["route"], row["transport"], row["rail"]) == ("audit.wcag", "/audit/wcag", "http", "x402")
    assert row["payer"] == BUYER and row["tx_hash"] == TX and row["received_usd"] == 0.05
    assert row["outcome"] == "delivered" and row["internal"] == 0 and row["kind"] == "sale"
    assert "<body>hi</body>" in json.dumps(row["request_json"]) and row["user_agent"] == "agent-sdk/1.0"


def test_a_paid_worker_call_is_booked_once_with_its_receipt(app_module, client, monkeypatch):
    W = app_module.workers
    _pay_x402(app_module, monkeypatch)
    worker = W.catalog.BY_NAME["stats.probability"]
    registry = dict(W.router.REGISTRY)

    async def skill(ctx, payload):
        return W.catalog.output_example(worker)
    registry["stats.probability"] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = W.catalog.example_for(worker)
    response = client.post(worker.path, headers={"PAYMENT-SIGNATURE": "signed"}, json=body)
    assert response.status_code == 200, response.text
    rows = _rows(app_module)
    assert len(rows) == 1 and rows[0]["product"] == "stats.probability" and rows[0]["route"] == worker.path
    assert rows[0]["receipt_id"] == response.json()["receipt_id"]
    ledger = sqlite3.connect(str(Path(W.ledger.status()["path"])))
    request_hash = ledger.execute("SELECT request_hash FROM worker_calls").fetchone()[0]
    assert rows[0]["request_hash"] == request_hash


def test_owner_key_calls_are_not_sales(app_module, client):
    assert client.post("/audit/wcag", headers={"X-API-Key": "owner-key"},
                       json={"html": "<html lang='en'></html>"}).status_code == 200
    assert _rows(app_module) == []


def test_a_failed_job_after_payment_is_booked_unbilled(app_module, client, monkeypatch):
    _pay_x402(app_module, monkeypatch)

    def boom(html, url):
        raise RuntimeError("the page did not load")
    monkeypatch.setattr(app_module, "_run_axe", boom)
    response = client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"})
    assert response.status_code == 502
    row = _rows(app_module)[0]
    assert row["outcome"] == "failed_unbilled" and not row["received_usd"] and "page did not load" in (row["note"] or "")


def test_bookkeeping_failures_never_change_a_sale(app_module, client, monkeypatch):
    _pay_x402(app_module, monkeypatch)
    control = client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"})

    def explode(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(app_module.purchase_book, "insert_row", explode)
    monkeypatch.setattr(app_module.purchase_book, "open_sale", explode)
    broken = client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"})
    assert broken.status_code == control.status_code == 200
    assert broken.json()["status"] == control.json()["status"]


def test_the_client_address_comes_from_the_proxy_hop_only(app_module):
    from starlette.requests import Request

    def request(peer, forwarded):
        headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return Request({"type": "http", "method": "POST", "path": "/", "headers": headers,
                        "client": (peer, 1234), "query_string": b""})
    ip = app_module.purchase_book.client_ip
    assert ip(request("172.18.0.3", "1.2.3.4, 198.51.100.7")) == ("198.51.100.7", "172.18.0.3")
    assert ip(request("203.0.113.9", "1.2.3.4")) == ("203.0.113.9", "203.0.113.9")  # spoof ignored


def test_owner_pages_are_invisible_without_the_owner_key(app_module, client, monkeypatch):
    _pay_x402(app_module, monkeypatch)
    client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"})
    unknown = client.get("/owner/nothing-here")
    for path in ("/owner/purchases", "/owner/purchases.csv", "/owner/purchases/summary"):
        for headers in ({}, {"X-API-Key": "wrong"}):
            refused = client.get(path + "?limit=zzz", headers=headers)
            assert refused.status_code == 404 and refused.content == unknown.content, path
        # The unmetered call key is not the owner key.
        assert client.get(path, headers={"X-API-Key": "owner-key"}).status_code == 404
        assert client.post(path, headers={"X-API-Key": "books-key"}).status_code == 404
        assert client.get(path, headers={"X-API-Key": "books-key"}).status_code == 200
        assert path not in json.dumps(client.get("/openapi.json").json())
    listed = client.get("/owner/purchases", headers={"Authorization": "Bearer books-key"}).json()
    assert listed["count"] == 1 and listed["rows"][0]["payer"] == BUYER


def test_the_csv_is_safe_to_open_in_a_spreadsheet(app_module):
    PB = app_module.purchase_book
    PB.insert_row({"kind": "sale", "outcome": "delivered", "payer": BUYER, "user_agent": "=HYPERLINK(\"x\")",
                   "product": "market.quote", "price_usd": 0.02, "received_usd": 0.02, "rail": "x402"})
    text = PB.to_csv(PB.query())
    assert "'=HYPERLINK" in text and "\n=HYPERLINK" not in text


def test_the_summary_separates_outside_sales_own_seeding_and_transfers(app_module):
    PB = app_module.purchase_book
    PB.insert_row({"kind": "sale", "outcome": "delivered", "payer": BUYER, "received_usd": 0.05, "rail": "x402"})
    PB.insert_row({"kind": "sale", "outcome": "delivered", "payer": SEED, "received_usd": 0.10, "rail": "x402",
                   "internal": 1})
    PB.insert_row({"kind": "transfer", "outcome": "backfilled_from_chain", "payer": "0x0780", "received_usd": 80.0,
                   "rail": "chain"})
    totals = PB.summary()["totals"]
    assert totals["outside"]["earned_usd"] == 0.05 and totals["outside"]["payers"] == 1
    assert totals["internal"]["earned_usd"] == 0.10
    assert totals["transfers"] == {"count": 1, "usd": 80.0}


def _reconcile_module(app_module):
    sys.modules["wcag_audit_engine_purchase_book"] = app_module.purchase_book
    spec = importlib.util.spec_from_file_location("wcag_audit_engine_purchase_reconcile", APP / "purchase_reconcile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reconcile_finds_classifies_and_backfills_without_inventing(app_module, tmp_path):
    R = _reconcile_module(app_module)
    PB = app_module.purchase_book
    PB.insert_row({"kind": "sale", "outcome": "delivered", "payer": BUYER, "tx_hash": TX, "rail": "x402",
                   "network": "eip155:8453", "product": "audit.wcag"})
    audit_sale = "0x" + "9b" * 32
    funding = "0x" + "07" * 32
    batched = "0x" + "82" * 32
    listing = {"status": "1", "message": "OK", "result": [
        {"hash": TX, "from": BUYER, "to": PAY_TO.lower(), "value": "50000", "timeStamp": "1790000000",
         "input": "0xe3ee160e00"},
        {"hash": audit_sale, "from": "0x9b7dcf5287f13118b9d29acd4f719172e2302f48", "to": PAY_TO.lower(),
         "value": "50000", "timeStamp": "1789426349", "input": "0xe3ee160e00"},
        {"hash": funding, "from": "0x0780cf08b9a5504a828e666ff38f90e49653560f", "to": PAY_TO.lower(),
         "value": "80000000", "timeStamp": "1789900000", "input": "0xa9059cbb00"},
        {"hash": batched, "from": SEED, "to": PAY_TO.lower(), "value": "30000", "timeStamp": "1789500000",
         "input": "0x82ad56cb00"},
        {"hash": "0x" + "ff" * 32, "from": BUYER, "to": "0xsomeoneelse", "value": "1", "timeStamp": "1790000001",
         "input": "0xe3ee160e"}]}

    def get_json(url, params=None):
        assert params["module"] == "account" and params["action"] == "tokentx"
        return listing

    found = list(R.base_transfers(PAY_TO, get_json=get_json))
    assert len(found) == 4  # the transfer to someone else is not ours

    def logs_of(tx):
        if tx == batched:
            return [{"address": R.BASE_USDC, "topics": [R.AUTH_USED, "0x" + "0" * 24 + SEED[2:], "0x" + "ee" * 32]}]
        return []
    for p in found:
        R.classify_base(p, logs_of=logs_of, bridges=frozenset())
    kinds = {p.tx_hash: p.classification for p in found}
    assert kinds == {TX: "x402", audit_sale: "x402", funding: "plain_transfer", batched: "x402_batched"}
    conn = PB.connect()
    known = R.load_known(conn, worker_db=str(tmp_path / "none.db"), mpp_db=str(tmp_path / "none.db"),
                         solana_db=str(tmp_path / "none.db"))
    report = R.reconcile(conn, found, known, backfill=True, dry_run=False)
    assert report["counts"]["eip155:8453"] == {"recorded": 1, "ledger_only": 0, "missing": 3}
    rows = {r["tx_hash"]: r for r in PB.query(limit=100)}
    assert rows[audit_sale]["kind"] == "sale" and rows[audit_sale]["outcome"] == "backfilled_from_chain"
    assert rows[audit_sale]["request_json"] is None and rows[audit_sale]["client_ip"] is None
    assert rows[funding]["kind"] == "transfer"
    assert rows[batched]["internal"] == 1
    assert rows[TX]["chain_status"] == "seen"


def test_ledger_backfill_keeps_product_and_receipt_but_never_invents_the_request(app_module, tmp_path):
    R = _reconcile_module(app_module)
    worker_db = tmp_path / "old-ledger.db"
    conn = sqlite3.connect(worker_db)
    conn.execute("CREATE TABLE worker_calls (call_id TEXT, worker TEXT, path TEXT, started_at REAL, finished_at REAL, "
                 "price_micros INTEGER, rail TEXT, network TEXT, asset TEXT, pay_to TEXT, amount_atomic INTEGER, "
                 "payer TEXT, tx_hash TEXT, settled INTEGER, request_hash TEXT, idempotency_key TEXT, node_version TEXT)")
    conn.execute("INSERT INTO worker_calls VALUES ('c1','market.prediction','/work/market/prediction',1790000000,"
                 "1790000001,50000,'x402','eip155:8453',?,?,50000,?,?,1,'sha256:aa',NULL,'1.5.0')",
                 (USDC, PAY_TO, BUYER, TX))
    conn.commit()
    conn.close()
    added = R.backfill_from_ledgers(worker_db=str(worker_db), solana_db=str(tmp_path / "none.db"))
    assert added["worker_calls"] == 1
    row = app_module.purchase_book.query()[0]
    assert (row["product"], row["receipt_id"], row["outcome"]) == ("market.prediction", "rcpt_c1", "backfilled_from_ledger")
    assert row["request_json"] is None and row["client_ip"] is None and "not recorded" in row["note"]


def test_an_outside_sale_alerts_and_a_failed_alert_changes_nothing(app_module, client, monkeypatch):
    PB = app_module.purchase_book
    sent = []
    monkeypatch.setenv("PURCHASE_ALERT_WEBHOOK", "https://hooks.example/x")
    monkeypatch.setattr(PB, "_post_alert", lambda url, payload: sent.append(payload))
    _pay_x402(app_module, monkeypatch)
    assert client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"}).status_code == 200
    PB.wait_for_alerts()
    assert len(sent) == 1 and sent[0]["product"] == "audit.wcag" and "<p>x</p>" not in json.dumps(sent[0])

    def fail(url, payload):
        raise RuntimeError("webhook down")
    monkeypatch.setattr(PB, "_post_alert", fail)
    assert client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"}).status_code == 200
    PB.wait_for_alerts()
    _pay_x402(app_module, monkeypatch, payer=SEED)
    sent.clear()
    monkeypatch.setattr(PB, "_post_alert", lambda url, payload: sent.append(payload))
    client.post("/audit/wcag", headers={"PAYMENT-SIGNATURE": "signed"}, json={"html": "<p>x</p>"})
    PB.wait_for_alerts()
    assert sent == []  # the owner's own seeding is not a sale to alert on
