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
import stripe

# The real class, captured before any fixture swaps Session out for the fake.
_REAL_SESSION = stripe.checkout.Session

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"


def _drop_cache():
    # Configured siblings (billing, payments) are re-read per test; the shared
    # workers package stays, because other test files hold references to it.
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_") and name != "wcag_audit_engine_workers":
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
            customer_details={"email": "buyer@example.com"},
            amount_total=kwargs["line_items"][0]["price_data"]["unit_amount"])
        return SimpleNamespace(url=f"https://checkout.stripe.com/c/pay/{sid}", id=sid)

    def retrieve(self, sid):
        if sid not in self.sessions:
            raise RuntimeError("No such checkout.session")
        # The real library's object, not the namespace: stripe-python 15's
        # StripeObject is no longer a dict, and a fake that hands back plain
        # dicts is how the first real card payment came back without its key.
        return _REAL_SESSION.construct_from(dict(vars(self.sessions[sid])), "sk_test_x")

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


def test_the_buyers_own_link_shows_their_key_any_time(app, stripe_fake, monkeypatch):
    """No lockout: a buyer who comes back weeks later still gets their key."""
    client = _client(app)
    client.post("/billing/credits", json={"pack": "25"})
    stripe_fake.pay("cs_test_1")
    first = client.get("/billing/credits/key?session_id=cs_test_1").json()["api_key"]
    real_time = time.time
    monkeypatch.setattr(app.time, "time", lambda: real_time() + 60 * 24 * 3600)
    later = client.get("/billing/credits/key?session_id=cs_test_1")
    assert later.status_code == 200 and later.json()["api_key"] == first


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


def test_the_source_of_a_sale_is_carried_from_the_link_to_the_book(app, stripe_fake):
    """A buyer who arrives on /start?ref=ads is counted as an ads sale: the
    label rides through Stripe's metadata into the purchase book's note."""
    client = _client(app)
    client.post("/billing/credits", json={"pack": "25", "source": "ads"})
    assert stripe_fake.created[0]["metadata"]["source"] == "ads"
    stripe_fake.pay("cs_test_1")
    assert client.get("/billing/credits/key?session_id=cs_test_1").status_code == 200
    note = app.purchase_book.connect().execute("SELECT note FROM purchases").fetchone()[0]
    assert note.startswith("source ads; credit $25.00")


def test_a_bad_or_missing_source_is_simply_left_out(app, stripe_fake):
    client = _client(app)
    for source in (None, "", "x" * 41, "ads<script>", "a b", 7):
        body = {"pack": "25"} if source is None else {"pack": "25", "source": source}
        response = client.post("/billing/credits", json=body)
        assert response.status_code in (200, 422), response.text
    for created in stripe_fake.created:
        assert "source" not in created["metadata"], created["metadata"]
        assert created["metadata"]["kind"] == "credit_pack"


def test_the_start_page_passes_the_ref_of_the_link():
    start = (STATIC / "start.html").read_text()
    assert "sourceTag()" in start and 'get("ref")' in start
    assert "source label of the link" in (STATIC / "privacy.html").read_text()


# --- released on purchase: Stripe's notice issues and mails the key ---------

WEBHOOK_SECRET = "whsec_test_" + "s" * 24


@pytest.fixture
def notice_app(monkeypatch, tmp_path):
    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("PURCHASE_BOOK_PATH", str(tmp_path / "purchases.db"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_" + "x" * 24)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("MAIL_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("MAIL_SMTP_USER", "hubvibe@hubvibe-io.com")
    monkeypatch.setenv("MAIL_SMTP_PASSWORD", "mail-password")
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    for var in ("AUDIT_API_KEY", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "MAIL_SMTP_PORT", "MAIL_FROM"):
        monkeypatch.delenv(var, raising=False)
    _drop_cache()
    spec = importlib.util.spec_from_file_location("wcag_main_credit_notice", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    _drop_cache()


class _Mailbox:
    """Stands in for smtplib.SMTP_SSL; `fail` makes the server refuse."""

    def __init__(self):
        self.sent, self.fail = [], False

    def factory(self, host, port, context=None, timeout=None):
        box = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                assert (host, port, user, password) == (
                    "smtp.example.test", 465, "hubvibe@hubvibe-io.com", "mail-password")

            def send_message(self, message):
                if box.fail:
                    raise OSError("mail server refused")
                box.sent.append(message)

        return _Conn()


@pytest.fixture
def notice_stripe(notice_app, monkeypatch):
    fake = _Stripe()
    monkeypatch.setattr(notice_app.billing.stripe.checkout, "Session",
                        SimpleNamespace(create=fake.create, retrieve=fake.retrieve))
    return fake


@pytest.fixture
def mailbox(monkeypatch):
    import smtplib

    box = _Mailbox()
    monkeypatch.setattr(smtplib, "SMTP_SSL", box.factory)
    return box


def _notice(client, session_id, event_type="checkout.session.completed", secret=WEBHOOK_SECRET):
    import hashlib
    import hmac
    import json

    payload = json.dumps({
        "id": "evt_test", "object": "event", "type": event_type,
        "data": {"object": {"id": session_id, "object": "checkout.session",
                            "metadata": {"kind": "credit_pack"}}},
    })
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/billing/webhook", content=payload,
                       headers={"stripe-signature": f"t={stamp},v1={signature}",
                                "content-type": "application/json"})


def test_payment_releases_and_mails_the_key_without_the_buyers_page(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "100"})
    notice_stripe.pay("cs_test_1")
    assert _notice(client, "cs_test_1").status_code == 200
    assert len(mailbox.sent) == 1
    message = mailbox.sent[0]
    assert message["To"] == "buyer@example.com"
    assert message["From"] == "HubVibe <hubvibe@hubvibe-io.com>"
    assert message["Subject"] == "Your HubVibe key ($105.00 credit)"
    # The page shows the very key that was mailed, and it carries the credit.
    key = client.get("/billing/credits/key?session_id=cs_test_1").json()["api_key"]
    assert key in message.get_content()
    assert notice_app.billing.lookup_key(key)["prepaid_balance_cents"] == 10500
    rows = notice_app.purchase_book.connect().execute("SELECT product FROM purchases").fetchall()
    assert [tuple(row) for row in rows] == [("credit_pack_100",)], "booked once, by whichever path came first"


def test_a_repeated_notice_issues_and_mails_nothing_new(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "25"})
    notice_stripe.pay("cs_test_1")
    for _ in range(3):
        assert _notice(client, "cs_test_1").status_code == 200
    assert len(mailbox.sent) == 1
    assert notice_app.purchase_book.connect().execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1


def test_a_slow_payment_is_released_when_it_clears(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "25"})
    notice_stripe.sessions["cs_test_1"].status = "complete"   # checkout done, money not yet in
    assert _notice(client, "cs_test_1").status_code == 200
    assert mailbox.sent == [] and client.get("/billing/credits/key?session_id=cs_test_1").status_code == 202
    notice_stripe.pay("cs_test_1")
    assert _notice(client, "cs_test_1", "checkout.session.async_payment_succeeded").status_code == 200
    assert len(mailbox.sent) == 1


def test_a_refused_email_is_retried_and_never_mints_twice(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "25"})
    notice_stripe.pay("cs_test_1")
    mailbox.fail = True
    assert _notice(client, "cs_test_1").status_code == 500, "a non-2xx makes Stripe send it again"
    key = client.get("/billing/credits/key?session_id=cs_test_1").json()["api_key"]
    mailbox.fail = False
    assert _notice(client, "cs_test_1").status_code == 200
    assert len(mailbox.sent) == 1 and key in mailbox.sent[0].get_content()


def test_the_buyers_page_first_still_gets_the_key_mailed(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "25"})
    notice_stripe.pay("cs_test_1")
    key = client.get("/billing/credits/key?session_id=cs_test_1").json()["api_key"]
    assert _notice(client, "cs_test_1").status_code == 200
    assert len(mailbox.sent) == 1 and key in mailbox.sent[0].get_content()
    assert notice_app.purchase_book.connect().execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1


def test_a_forged_notice_is_refused(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    client.post("/billing/credits", json={"pack": "25"})
    notice_stripe.pay("cs_test_1")
    assert _notice(client, "cs_test_1", secret="whsec_wrong_" + "w" * 20).status_code == 400
    assert mailbox.sent == []
    assert notice_app.purchase_book.connect().execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 0


def test_other_notices_are_acknowledged_and_ignored(notice_app, notice_stripe, mailbox):
    client = _client(notice_app)
    assert _notice(client, "cs_test_9", event_type="charge.succeeded").status_code == 200
    assert mailbox.sent == []


def test_without_a_signing_secret_the_notice_door_stays_shut(app):
    assert _notice(_client(app), "cs_test_1").status_code == 501

