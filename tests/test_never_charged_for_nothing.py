"""A prepaid key and an MPP payment are never kept for a call that did no
new work.

A prepaid key is debited (and an MPP payment marked as spent) at the gate,
before the job runs, so a key with no balance is refused before any work is
spent on it. Every way a call can then end WITHOUT delivering something new
must hand that back, or "charged only for results" is true for x402 and false
for the buyers who paid up front:

  * the stored result for a repeated Idempotency-Key ("this request was not
    charged") debited the key again;
  * every poll of a still-running job with that key (409) debited it again;
  * a request the tool refused after the gate (400, "billed": false) kept
    the debit;
  * a handed-back job that a restart or a crash interrupted ("nothing was
    charged") kept the debit.

Each test drives the REAL route against the REAL SQLite key store and checks
the balance, which is the only thing the buyer can spend.
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

QUOTE = {"product_id": "BTC-USD"}
QUOTE_CENTS = 2  # market.quote is $0.02


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


def _drop_sibling_cache():
    for name in [n for n in sys.modules
                 if n.startswith("wcag_audit_engine_") and n != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _isolated_siblings():
    _drop_sibling_cache()
    yield
    _drop_sibling_cache()


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "x" * 24)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("PURCHASE_BOOK_PATH", str(tmp_path / "purchases.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("WORKER_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    for var in ("AUDIT_API_KEY", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "STRIPE_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_main_charged_for_nothing", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    W.router._DEFERRED.clear()
    yield module
    W.router._DEFERRED.clear()
    W.ledger.reset_for_tests()


def _valid(name, **extra):
    return {**W.catalog.output_example(W.catalog.BY_NAME[name]), **extra}


def _serve(monkeypatch, skill):
    """market.quote runs `skill`; everything around it is the real node."""
    registry = dict(W.router.REGISTRY)
    registry["market.quote"] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    monkeypatch.setattr(W.router, "PRECHECKS", {})


def _balance(app_module, key):
    return app_module.billing.lookup_key(key)["prepaid_balance_cents"]


def _collect(client, job_id, until_not=202, seconds=5.0):
    deadline = time.monotonic() + seconds
    while True:
        response = client.get(f"/work/jobs/{job_id}")
        if response.status_code != until_not or time.monotonic() > deadline:
            return response
        time.sleep(0.05)


async def _fast(ctx, payload):
    return _valid("market.quote", marker="fast")


def test_a_delivered_job_costs_the_key_its_price_exactly_once(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    _serve(monkeypatch, _fast)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        response = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
    assert response.status_code == 200, response.text
    assert _balance(app_module, key) == 10 - QUOTE_CENTS


def test_a_replayed_request_does_not_debit_the_key_again(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    _serve(monkeypatch, _fast)
    key = app_module.billing.issue_prepaid_key(10)
    headers = {"X-API-Key": key, "Idempotency-Key": "replay-1"}
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers=headers, json=QUOTE)
        assert first.status_code == 200, first.text
        assert _balance(app_module, key) == 10 - QUOTE_CENTS
        for _ in range(3):
            replay = client.post("/work/market/quote", headers=headers, json=QUOTE)
            assert replay.status_code == 200
            assert replay.json()["idempotent_replay"] is True and replay.json()["billed"] is False
    assert _balance(app_module, key) == 10 - QUOTE_CENTS, (
        "the replay said 'this request was not charged' and debited the key again")


def test_polling_a_running_job_with_its_key_costs_nothing(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")

    async def slow(ctx, payload):
        await asyncio.sleep(0.7)
        return _valid("market.quote", marker="once")

    _serve(monkeypatch, slow)
    key = app_module.billing.issue_prepaid_key(10)
    headers = {"X-API-Key": key, "Idempotency-Key": "poll-1"}
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers=headers, json=QUOTE)
        assert first.status_code == 202, first.text
        for _ in range(2):
            busy = client.post("/work/market/quote", headers=headers, json=QUOTE)
            assert busy.status_code == 409 and busy.json()["billed"] is False
        done = _collect(client, first.json()["job_id"])
        assert done.status_code == 200
    assert _balance(app_module, key) == 10 - QUOTE_CENTS, (
        "each 409 said 'billed: false' and debited the key")


def test_a_request_the_tool_refuses_after_the_gate_is_refunded(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    async def picky(ctx, payload):
        raise W.runtime.InvalidRequest("That product is not one this tool quotes.")

    _serve(monkeypatch, picky)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        response = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["billed"] is False and body["reason"] == "invalid_request"
    assert "not one this tool quotes" in body["detail"]
    assert "input_schema" in body, "the caller still needs the schema to fix its body"
    assert _balance(app_module, key) == 10, "a 400 that says 'billed: false' kept the debit"


def test_a_handed_back_job_that_fails_is_refunded_once(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")

    async def slow_then_broken(ctx, payload):
        await asyncio.sleep(0.5)
        raise W.runtime.PermanentProviderError("upstream said no")

    _serve(monkeypatch, slow_then_broken)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        assert _balance(app_module, key) == 10 - QUOTE_CENTS, "held while the job runs"
        done = _collect(client, first.json()["job_id"])
        assert done.status_code == 502 and done.json()["billed"] is False
        # A later sweep must find nothing left to hand back.
        assert W.router.reconcile_interrupted() == 0
    assert _balance(app_module, key) == 10


def test_a_handed_back_job_that_delivers_keeps_its_price_even_after_a_sweep(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")

    async def slow(ctx, payload):
        await asyncio.sleep(0.5)
        return _valid("market.quote", marker="late")

    _serve(monkeypatch, slow)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        done = _collect(client, first.json()["job_id"])
        assert done.status_code == 200
        assert W.router.reconcile_interrupted() == 0
        # Nothing is left that a later sweep, or anyone holding the job id,
        # could turn back into credit.
        assert app_module.billing.refund_prepaid_hold(
            W.ledger.get_deferred(first.json()["job_id"])["call_id"]) is False
    assert _balance(app_module, key) == 10 - QUOTE_CENTS


def test_a_prepaid_job_a_restart_interrupted_gets_its_money_back_once(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")

    async def never(ctx, payload):
        await asyncio.sleep(60)

    _serve(monkeypatch, never)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]
        assert _balance(app_module, key) == 10 - QUOTE_CENTS

        # The process that was running it is gone (a crash, the OOM killer):
        # nothing in memory, and its liveness lock is free.
        W.router._DEFERRED.pop(job_id)
        conn = W.ledger._safe_connect()
        conn.execute("UPDATE deferred_jobs SET owner=? WHERE job_id=?", ("f" * 32, job_id))
        conn.commit()

        assert W.router.reconcile_interrupted() == 1
        assert _balance(app_module, key) == 10, (
            "the job was closed 'nothing was charged' and the debit was kept")
        collected = client.get(f"/work/jobs/{job_id}")
        assert collected.status_code == 502 and collected.json()["billed"] is False
        # A second sweep, or the dead job's own late ending, hands back nothing more.
        assert W.router.reconcile_interrupted() == 0
    assert _balance(app_module, key) == 10


def test_a_handed_back_job_cut_off_mid_run_is_refunded(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    monkeypatch.setenv("WORKER_DRAIN_SECONDS", "0")

    async def never(ctx, payload):
        await asyncio.sleep(60)

    _serve(monkeypatch, never)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]
        task = W.router._DEFERRED[job_id]
        client.portal.call(task.cancel)
        collected = _collect(client, job_id)
        assert collected.status_code == 502 and collected.json()["billed"] is False
    assert _balance(app_module, key) == 10, "the cut-off job said 'nothing was charged'"
    assert W.ledger.get_call(W.ledger.get_deferred(job_id)["call_id"])["status"] == "failed"


def test_an_mpp_payment_is_released_when_the_answer_is_a_replay(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    released = []
    monkeypatch.setattr(app_module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: None)
    monkeypatch.setattr(app_module.mpp_payments, "verify_and_settle_sync", lambda cred, realm=None: True)
    monkeypatch.setattr(app_module.mpp_payments, "release_credential", released.append)
    _serve(monkeypatch, _fast)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", json=QUOTE, headers={
            "Authorization": "Payment first-payment", "Idempotency-Key": "mpp-replay"})
        assert first.status_code == 200, first.text
        assert released == [], "a delivered job keeps its payment"
        replay = client.post("/work/market/quote", json=QUOTE, headers={
            "Authorization": "Payment second-payment", "Idempotency-Key": "mpp-replay"})
        assert replay.status_code == 200 and replay.json()["billed"] is False
    assert released == ["second-payment"], (
        "the replay was 'not charged' but the payment that came with it stayed spent")


def test_a_topup_that_arrives_with_a_replay_still_hands_over_its_key(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    _serve(monkeypatch, _fast)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", json=QUOTE,
                            headers={"X-API-Key": key, "Idempotency-Key": "topup-replay"})
        assert first.status_code == 200, first.text
        monkeypatch.setattr(app_module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: 50)
        replay = client.post("/work/market/quote", json=QUOTE, headers={
            "Authorization": "Payment topup-credential", "Idempotency-Key": "topup-replay"})
    assert replay.status_code == 200 and replay.json()["billed"] is False
    bought = replay.json().get("api_key")
    assert bought, "the key the top-up bought was dropped from the replay"
    assert _balance(app_module, bought) == 50, "the replay was not charged, so the key holds it all"


# --- found by the review of this change (2026-10-03) -------------------------

def _hold(app_module, call_id):
    doc = app_module.billing._firestore().collection("prepaid_holds").document(call_id).get()
    return doc.to_dict() if doc.exists else None


def _wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_job_with_no_prepaid_debit_never_touches_the_key_store(app_module, monkeypatch):
    """The buyer who pays per call (x402) must see exactly what main does: a
    handed-back job of theirs finishing is not a reason to open the key
    store, least of all on the thread every other request is served from."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    monkeypatch.setenv("AUDIT_API_KEY", "owner-key")
    monkeypatch.setattr(app_module, "API_KEY", "owner-key")
    touched = []
    for name in ("hold_prepaid", "close_prepaid_hold", "refund_prepaid_hold"):
        real = getattr(app_module.billing, name)
        monkeypatch.setattr(app_module.billing, name,
                            lambda *a, _n=name, _r=real, **k: (touched.append(_n), _r(*a, **k))[1])
    monkeypatch.setattr(W.router, "_close_hold", app_module.billing.close_prepaid_hold)
    monkeypatch.setattr(W.router, "_refund_hold", app_module.billing.refund_prepaid_hold)

    async def slow(ctx, payload):
        await asyncio.sleep(0.5)
        return _valid("market.quote", marker="no-key-store")

    _serve(monkeypatch, slow)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": "owner-key"}, json=QUOTE)
        assert first.status_code == 202, first.text
        assert _collect(client, first.json()["job_id"]).status_code == 200
        time.sleep(0.2)
    assert touched == [], f"a job with nothing held reached the key store: {touched}"


def test_the_hold_is_written_and_closed_off_the_event_loop(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    on_loop = {}

    def watch(name):
        real = getattr(app_module.billing, name)

        def wrapper(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop[name] = True
            except RuntimeError:
                on_loop[name] = False
            return real(*args, **kwargs)
        monkeypatch.setattr(app_module.billing, name, wrapper)
        return wrapper

    watch("hold_prepaid")
    monkeypatch.setattr(W.router, "_close_hold", watch("close_prepaid_hold"))

    async def slow(ctx, payload):
        await asyncio.sleep(0.5)
        return _valid("market.quote", marker="off-loop")

    _serve(monkeypatch, slow)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        call_id = W.ledger.get_deferred(first.json()["job_id"])["call_id"]
        assert _collect(client, first.json()["job_id"]).status_code == 200
        assert _wait_for(lambda: (_hold(app_module, call_id) or {}).get("state") == "closed")
    assert on_loop == {"hold_prepaid": False, "close_prepaid_hold": False}, (
        f"key-store writes ran on the thread that serves every request: {on_loop}")
    assert _balance(app_module, key) == 10 - QUOTE_CENTS


def _cut_off_during_billing(app_module, monkeypatch, client_headers, auth=None):
    """Hand a job back, let it reach billing, hold the billing thread there,
    cancel the job, then let billing finish. Returns (collected, retry, unwound)."""
    import threading
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    monkeypatch.setenv("WORKER_DRAIN_SECONDS", "0")
    entered, release, unwound = threading.Event(), threading.Event(), []

    def slow_bill(bill_auth, price_usd):
        entered.set()
        release.wait(5)
        return None if auth is not None else app_module._bill(bill_auth, price_usd)

    def failed(failed_auth, detail):
        unwound.append(detail)
        return app_module._failed_audit_response(failed_auth, detail)

    async def slowish(ctx, payload):
        await asyncio.sleep(0.4)
        return _valid("market.quote", marker="settling")

    _serve(monkeypatch, slowish)
    monkeypatch.setattr(W.router, "_bill", slow_bill)
    monkeypatch.setattr(W.router, "_failed", failed)
    if auth is not None:
        monkeypatch.setattr(W.router, "_authorize", lambda *a, **k: (auth, None))
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers=client_headers, json=QUOTE)
        assert first.status_code == 202, first.text
        job_id = first.json()["job_id"]
        assert entered.wait(5), "the job never reached billing"
        client.portal.call(W.router._DEFERRED[job_id].cancel)
        collected = _collect(client, job_id)
        release.set()
        retry = client.post("/work/market/quote", headers=client_headers, json=QUOTE)
        if retry.status_code == 202:
            _collect(client, retry.json()["job_id"])
    return collected, retry, unwound


def test_a_job_cut_off_while_its_payment_is_being_finalised_is_not_unwound(app_module, monkeypatch):
    """A payment that settles after delivery (x402) is settled in a thread
    that cannot be cancelled. A job cut off at that moment may have been
    charged, so it must not be told 'nothing was charged, send it again', and
    its Idempotency-Key must stay taken."""
    from types import SimpleNamespace

    settling = app_module.AuthContext(
        stripe_billable=False, payment_method="x402",
        pending_payment=SimpleNamespace(payload=None, requirements=[], settle_result=None,
                                        settle_state=None))
    collected, retry, unwound = _cut_off_during_billing(
        app_module, monkeypatch, {"X-PAYMENT": "stub", "Idempotency-Key": "mid-settle"}, auth=settling)
    assert collected.status_code == 502
    body = collected.json()
    assert body["reason"] == "interrupted_during_billing"
    assert "Nothing was charged" not in body["detail"] and "may have been charged" in body["detail"]
    assert body.get("receipt_url", "").startswith("/work/receipts/")
    assert retry.status_code == 409, "the key was freed: a resend would be a second charge"
    assert unwound == [], "the payment was handed back while it was being taken"


def test_a_prepaid_job_cut_off_at_that_same_moment_is_refunded(app_module, monkeypatch):
    """A prepaid debit was taken at the gate: billing moves nothing, so a
    cut-off there is an ordinary one. Refunded, and the key can be retried."""
    key = app_module.billing.issue_prepaid_key(10)
    collected, retry, unwound = _cut_off_during_billing(
        app_module, monkeypatch, {"X-API-Key": key, "Idempotency-Key": "prepaid-mid-bill"})
    assert collected.status_code == 502 and collected.json()["reason"] == "interrupted"
    assert unwound, "the debit was kept for a job that delivered nothing"
    assert retry.status_code != 409, "the key must be free to retry"
    assert _wait_for(lambda: _balance(app_module, key) in (10, 10 - QUOTE_CENTS))


def test_a_job_that_ends_while_its_hold_is_being_written_leaves_no_open_hold(app_module, monkeypatch):
    """The job fails in the moment its hold is on its way to the key store.
    It is refunded directly; the hold that lands afterwards must be closed,
    or a later sweep would refund the same debit a second time."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    real_hold = W.router._hold_payment

    def slow_hold(auth, hold_id):
        time.sleep(0.5)
        return real_hold(auth, hold_id)

    monkeypatch.setattr(W.router, "_hold_payment", slow_hold)

    async def fails_during_the_write(ctx, payload):
        await asyncio.sleep(0.4)
        raise W.runtime.PermanentProviderError("upstream said no")

    _serve(monkeypatch, fails_during_the_write)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        call_id = W.ledger.get_deferred(first.json()["job_id"])["call_id"]
        assert _collect(client, first.json()["job_id"]).status_code == 502
        assert _wait_for(lambda: (_hold(app_module, call_id) or {}).get("state") == "closed"), (
            f"the hold was left open: {_hold(app_module, call_id)}")
        assert app_module.billing.refund_prepaid_hold(call_id) is False
    assert _balance(app_module, key) == 10 and call_id not in W.router._HELD


def test_a_busy_key_store_does_not_hold_up_the_202(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    monkeypatch.setattr(W.router, "_HOLD_WRITE_WAIT_SECONDS", 0.3)
    real_hold = W.router._hold_payment
    monkeypatch.setattr(W.router, "_hold_payment",
                        lambda auth, hold_id: (time.sleep(1.5), real_hold(auth, hold_id))[1])

    async def slow(ctx, payload):
        await asyncio.sleep(2.2)
        return _valid("market.quote", marker="late-hold")

    _serve(monkeypatch, slow)
    key = app_module.billing.issue_prepaid_key(10)
    with TestClient(app_module.app) as client:
        started = time.monotonic()
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        waited = time.monotonic() - started
        assert first.status_code == 202, first.text
        assert waited < 1.2, f"the 202 waited {waited:.1f} s for bookkeeping"
        assert _collect(client, first.json()["job_id"]).status_code == 200
    assert _balance(app_module, key) == 10 - QUOTE_CENTS


def test_a_hold_refund_that_fails_keeps_the_record_so_it_can_be_made_good(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")

    async def slow_then_broken(ctx, payload):
        await asyncio.sleep(0.5)
        raise W.runtime.PermanentProviderError("upstream said no")

    _serve(monkeypatch, slow_then_broken)
    key = app_module.billing.issue_prepaid_key(10)
    real = app_module.billing._run_transactional
    state = {"fail": True}

    def flaky(fn):
        if state["fail"] and getattr(fn, "__name__", "") == "_refund":
            state["fail"] = False
            raise RuntimeError("database is locked")
        return real(fn)

    monkeypatch.setattr(app_module.billing, "_run_transactional", flaky)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": key}, json=QUOTE)
        assert first.status_code == 202, first.text
        call_id = W.ledger.get_deferred(first.json()["job_id"])["call_id"]
        assert _collect(client, first.json()["job_id"]).status_code == 502
        time.sleep(0.3)
        held = _hold(app_module, call_id)
        assert held and held.get("state") == "held" and held.get("api_key") == key, (
            f"the only record of what the buyer is owed was destroyed: {held}")
        assert _balance(app_module, key) == 10 - QUOTE_CENTS
        assert app_module.billing.refund_prepaid_hold(call_id) is True, "it can still be made good"
    assert _balance(app_module, key) == 10


def test_a_sweep_asks_the_key_store_only_about_jobs_that_could_hold_a_debit(app_module, monkeypatch):
    asked = []
    monkeypatch.setattr(W.router, "_refund_hold", lambda call_id: asked.append(call_id) or False)
    for name, rail in (("x402", "x402"), ("mpp", "mpp"), ("key", None)):
        W.ledger.open_call(call_id=f"c-{name}", worker="market.quote", path="/work/market/quote",
                           price_usd=0.02)
        W.ledger.open_deferred(f"job-{name}", f"c-{name}", "market.quote", rail=rail)
    conn = W.ledger._safe_connect()
    conn.execute("UPDATE deferred_jobs SET owner=?", ("f" * 32,))
    conn.commit()
    assert W.router.reconcile_interrupted() == 3
    assert asked == ["c-key"], f"jobs paid per call cannot hold a prepaid debit: {asked}"


def test_a_topup_key_is_handed_over_as_soon_as_its_job_is_handed_back(app_module, monkeypatch):
    """The key a top-up bought used to arrive only with the finished result.
    A job that was then interrupted never delivered one, so the buyer had
    paid for a key nobody ever told them."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    monkeypatch.setattr(app_module.mpp_payments, "settle_topup_sync", lambda cred, realm=None: 50)

    async def never(ctx, payload):
        await asyncio.sleep(60)

    _serve(monkeypatch, never)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", json=QUOTE,
                            headers={"Authorization": "Payment topup-credential"})
        assert first.status_code == 202, first.text
        bought = first.json().get("api_key")
        assert bought, "the key the top-up bought is not in the 202"
        job_id = first.json()["job_id"]
        W.router._DEFERRED.pop(job_id)
        conn = W.ledger._safe_connect()
        conn.execute("UPDATE deferred_jobs SET owner=? WHERE job_id=?", ("f" * 32, job_id))
        conn.commit()
        assert W.router.reconcile_interrupted() == 1
    assert _balance(app_module, bought) == 50, "interrupted: the key holds everything it bought"
