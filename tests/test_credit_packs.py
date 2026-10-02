"""Credit packs: the homepage's "Get started" opens a card payment window and
the buyer leaves with a key every paid route accepts.

What costs money if it is wrong:
  * a paid checkout yields exactly one key, however often the page reloads;
  * the key carries the pack's credit (bonus included) and nothing more;
  * an unpaid or foreign checkout yields nothing;
  * every pack sold is booked once in the purchase book, without card data;
  * the homepage sends people to the payment window, not to GitHub.
"""

import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"


def _drop_cache():
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("PURCHASE_BOOK_PATH", str(tmp_path / "purchases.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "x" * 24)
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    for var in ("AUDIT_API_KEY", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "STRIPE_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)
    _drop_cache()
    spec = importlib.util.spec_from_file_location("wcag_main_credit_packs", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    _drop_cache()


class _Stripe:
    """Stands in for stripe.checkout.Session: records creates, serves sessions."""

    def __init__(self):
        self.created = []
        self.sessions = {}

    def create(self, **kwargs):
        self.created.append(kwargs)
        sid = f"cs_test_{len(self.created)}"
        self.sessions[sid] = SimpleNamespace(
            id=sid, status="open", payment_status="unpaid",
            metadata=kwargs.get("metadata") or {},
            amount_total=kwargs["line_items"][0]["price_data"]["unit_amount"])
        return SimpleNamespace(url=f"https://checkout.stripe.com/c/pay/{sid}", id=sid)

    def retrieve(self, sid):
        if sid not in self.sessions:
            raise RuntimeError("No such checkout.session")
        return self.sessions[sid]

    def pay(self, sid):
        self.sessions[sid].status = "complete"
        self.sessions[sid].payment_status = "paid"


@pytest.fixture
def stripe_fake(app, monkeypatch):
    fake = _Stripe()
    monkeypatch.setattr(app.billing.stripe.checkout, "Session",
                        SimpleNamespace(create=fake.create, retrieve=fake.retrieve))
    return fake


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app.app)


def test_the_packs_and_their_bonus_credit(app):
    packs = app.billing.CREDIT_PACKS
    assert {k: (v["price_cents"], v["credit_cents"]) for k, v in packs.items()} == {
        "25": (2500, 2500), "100": (10000, 10500), "500": (50000, 55000)}


def test_get_started_opens_the_payment_window(app, stripe_fake):
    client = _client(app)
    assert client.get("/start").status_code == 200
    response = client.post("/billing/credits", json={"pack": "100"})
    assert response.status_code == 200
    assert response.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    created = stripe_fake.created[0]
    assert created["mode"] == "payment", "a pack is a one-time payment, never a subscription"
    assert created["line_items"][0]["price_data"]["unit_amount"] == 10000
    assert created["metadata"] == {"kind": "credit_pack", "pack": "100", "credit_cents": "10500"}
    assert created["success_url"].endswith("/start/success?session_id={CHECKOUT_SESSION_ID}")


def test_an_unknown_pack_is_refused(app, stripe_fake):
    response = _client(app).post("/billing/credits", json={"pack": "7"})
    assert response.status_code == 400 and stripe_fake.created == []


def test_no_card_payments_without_a_stripe_key(app, monkeypatch):
    monkeypatch.setattr(app.billing.stripe, "api_key", None)
    assert _client(app).post("/billing/credits", json={"pack": "25"}).status_code == 503


def test_a_paid_checkout_yields_one_key_with_its_credit(app, stripe_fake):
    client = _client(app)
    client.post("/billing/credits", json={"pack": "500"})
    sid = "cs_test_1"
    assert client.get(f"/billing/credits/key?session_id={sid}").status_code == 202, "unpaid: pending"
    stripe_fake.pay(sid)
    first = client.get(f"/billing/credits/key?session_id={sid}").json()
    assert first["credit_usd"] == 550.0 and first["paid_usd"] == 500.0 and first["pack"] == "500"
    again = client.get(f"/billing/credits/key?session_id={sid}").json()
    assert again["api_key"] == first["api_key"], "a reload must not mint a second balance"
    assert app.billing.lookup_key(first["api_key"])["prepaid_balance_cents"] == 55000


def test_the_key_spends_like_any_prepaid_key(app, stripe_fake):
    client = _client(app)
    client.post("/billing/credits", json={"pack": "25"})
    stripe_fake.pay("cs_test_1")
    key = client.get("/billing/credits/key?session_id=cs_test_1").json()["api_key"]
    assert app.billing.spend_prepaid(key, 275)
    assert app.billing.lookup_key(key)["prepaid_balance_cents"] == 2225
    assert not app.billing.spend_prepaid(key, 5000), "never spends past the balance"


def test_a_foreign_or_bogus_session_yields_nothing(app, stripe_fake):
    client = _client(app)
    stripe_fake.sessions["cs_test_sub"] = SimpleNamespace(
        status="complete", payment_status="paid", metadata={"plan": "pro"}, amount_total=2900)
    assert client.get("/billing/credits/key?session_id=cs_test_sub").status_code == 404
    assert client.get("/billing/credits/key?session_id=not-a-session").status_code == 404


def test_each_pack_is_booked_once_without_card_data(app, stripe_fake):
    client = _client(app)
    client.post("/billing/credits", json={"pack": "100"})
    stripe_fake.pay("cs_test_1")
    for _ in range(3):
        client.get("/billing/credits/key?session_id=cs_test_1")
    conn = app.purchase_book.connect()
    rows = conn.execute("SELECT kind, product, received_usd, rail, method, payment_ref, note, api_key_hash "
                        "FROM purchases").fetchall()
    assert len(rows) == 1
    kind, product, received, rail, method, ref, note, key_hash = rows[0]
    assert (kind, product, received, rail, method) == ("topup", "credit_pack_100", 100.0, "stripe-checkout", "card")
    assert ref == "stripe-checkout:cs_test_1"
    assert "bonus" in note and key_hash and len(key_hash) < 100


def test_an_old_key_is_not_shown_again(app, stripe_fake, monkeypatch):
    client = _client(app)
    client.post("/billing/credits", json={"pack": "25"})
    stripe_fake.pay("cs_test_1")
    client.get("/billing/credits/key?session_id=cs_test_1")
    real_time = time.time
    monkeypatch.setattr(app.time, "time", lambda: real_time() + app.CREDIT_KEY_REVEAL_SECONDS + 60)
    assert client.get("/billing/credits/key?session_id=cs_test_1").status_code == 410


def test_the_homepage_sends_people_to_the_payment_window():
    index = (STATIC / "index.html").read_text()
    assert "github.com" not in index
    assert index.count('href="/start"') >= 2
    start = (STATIC / "start.html").read_text()
    assert 'fetch("/billing/credits"' in start
    for name in ("start.html", "start-success.html"):
        text = (STATIC / name).read_text()
        for leak in ("gmail", "fortunatefool", "Amanda", "github.com"):
            assert leak not in text, f"{name} names {leak}"
