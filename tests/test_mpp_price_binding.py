"""An MPP payment buys the route it was priced for, and nothing dearer.

An MPP challenge is signed by this node and carries the amount, but nothing
tied it to the route that issued it, and the gate never compared that amount
with the price of the route being called. So the challenge from the cheapest
route (2 cents), honestly paid, was accepted on any route: a $10 job for two
cents. A 50-cent top-up presented on a $10 route was the same hole: the job
ran and no key was owed.

The gate now refuses a credential whose own signed amount is below the price
of the route it is presented on, before anything is charged or marked as
spent, so the payment stays good for the route it was meant for.
"""

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEMPO_PAY_TO = "0xc4a6aa93ba00d5c02145c33fe6f2212654fcdfb7"
TEMPO_USDC = "0x20C000000000000000000000b9537d11c60E8b50"

CHEAP = ("/work/market/quote", {"product_id": "BTC-USD"})          # $0.02
DEAR = ("/work/research/company", {"company": "Stripe"})           # $10.00


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
def node(monkeypatch, tmp_path):
    """The real node with the Tempo rail and the Stripe top-up rail on. No
    chain and no Stripe: the two verifiers are replaced and record what they
    were asked to settle."""
    global W
    W = _load_workers()
    monkeypatch.setenv("KEY_STORE", "sqlite")
    monkeypatch.setenv("KEY_STORE_SQLITE_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("PURCHASE_BOOK_PATH", str(tmp_path / "purchases.db"))
    monkeypatch.setenv("MPP_TEMPO_RECIPIENT_ADDRESS", TEMPO_PAY_TO)
    monkeypatch.setenv("MPP_CHALLENGE_SECRET", "a-long-random-secret")
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    for var in ("AUDIT_API_KEY", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "STRIPE_SECRET_KEY",
                "STRIPE_WEBHOOK_SECRET", "MPP_TEMPO_TOKEN_ADDRESS", "MPP_TEMPO_RPC_URL"):
        monkeypatch.delenv(var, raising=False)
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_main_mpp_price_binding", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    mpp = module.mpp_payments
    monkeypatch.setattr(mpp, "_TEMPO_RECIPIENT_ADDRESS", TEMPO_PAY_TO)
    monkeypatch.setattr(mpp, "_TEMPO_TOKEN_ADDRESS", TEMPO_USDC)
    monkeypatch.setattr(mpp, "_TEMPO_CHAIN_ID", 4217)
    monkeypatch.setattr(mpp, "_TEMPO_RPC_URL", "https://rpc.tempo.xyz")

    settled = []
    monkeypatch.setattr(mpp, "_verify_tempo", lambda challenge, payload: settled.append(
        ("tempo", json.loads(mpp._b64url_decode(challenge["request"]))["amount"])) or True)
    monkeypatch.setattr(mpp, "_verify_stripe", lambda challenge, payload: settled.append(
        ("stripe", json.loads(mpp._b64url_decode(challenge["request"]))["amount"])) or True)

    ran = []

    def skill(name):
        async def run(ctx, payload):
            ran.append(name)
            return W.catalog.output_example(W.catalog.BY_NAME[name])
        return run

    registry = dict(W.router.REGISTRY)
    registry["market.quote"] = skill("market.quote")
    registry["research.company"] = skill("research.company")
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    monkeypatch.setattr(W.router, "PRECHECKS", {})
    monkeypatch.setattr(W.catalog.BY_NAME["research.company"].__class__, "available", lambda self: True)
    module.settled, module.ran = settled, ran
    yield module
    W.ledger.reset_for_tests()


def _client(module):
    from fastapi.testclient import TestClient

    return TestClient(module.app)


def _credential(module, route, method="tempo"):
    """Pay `route`'s own challenge the way an MPP client does: take the
    challenge for `method` from its 402 and present it back with a payload."""
    path, body = route
    unpaid = _client(module).post(path, json=body)
    assert unpaid.status_code == 402, unpaid.text
    offers = [h for h in unpaid.headers.get_list("www-authenticate") if f'method="{method}"' in h]
    assert offers, unpaid.headers.get_list("www-authenticate")
    fields = dict(part.split("=", 1) for part in offers[0][len("Payment "):].split(", "))
    challenge = {k: v.strip('"') for k, v in fields.items()}
    raw = json.dumps({"challenge": challenge, "payload": {"type": "hash", "hash": "0x" + "ab" * 32}})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def test_a_payment_for_the_cheapest_route_does_not_buy_a_ten_dollar_job(node):
    cheap = _credential(node, CHEAP)
    path, body = DEAR
    response = _client(node).post(path, json=body, headers={"Authorization": f"Payment {cheap}"})
    assert response.status_code == 402, (
        f"a 2-cent payment bought a $10 job: HTTP {response.status_code} {response.text[:200]}")
    assert node.ran == [], "the job ran"
    assert node.settled == [], "the cheap payment was consumed: it must stay good for its own route"


def test_the_same_payment_still_buys_the_route_it_was_priced_for(node):
    cheap = _credential(node, CHEAP)
    path, body = CHEAP
    response = _client(node).post(path, json=body, headers={"Authorization": f"Payment {cheap}"})
    assert response.status_code == 200, response.text
    assert node.ran == ["market.quote"] and node.settled == [("tempo", "20000")]


def test_a_payment_for_the_dear_route_buys_the_dear_route(node):
    dear = _credential(node, DEAR)
    path, body = DEAR
    response = _client(node).post(path, json=body, headers={"Authorization": f"Payment {dear}"})
    assert response.status_code == 200, response.text
    assert node.ran == ["research.company"] and node.settled == [("tempo", "10000000")]


def test_a_cheap_payment_does_not_buy_an_audit_either(node, monkeypatch):
    monkeypatch.setattr(node, "_bundle_inputs", lambda url: (_ for _ in ()).throw(AssertionError("audit ran")))
    cheap = _credential(node, CHEAP)
    response = _client(node).post("/audit/bundle", json={"url": "https://example.com"},
                                  headers={"Authorization": f"Payment {cheap}"})
    assert response.status_code == 402, response.text[:200]
    assert node.settled == []


def _topup_credential(module, cents=50):
    mpp = module.mpp_payments
    challenge = mpp._build_challenge("testserver", "stripe", "topup", {
        "amount": str(cents), "currency": "usd",
        "methodDetails": {"networkId": "np_test", "paymentMethodTypes": ["card", "link"]}})
    raw = json.dumps({"challenge": challenge, "payload": {"spt": "spt_test_1"}})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def test_a_fifty_cent_topup_does_not_buy_a_ten_dollar_job_and_is_not_charged(node, monkeypatch):
    monkeypatch.setattr(node.mpp_payments, "stripe_configured", lambda: True)
    path, body = DEAR
    response = _client(node).post(path, json=body,
                                  headers={"Authorization": f"Payment {_topup_credential(node)}"})
    assert response.status_code == 402, (
        f"a 50-cent top-up bought a $10 job: HTTP {response.status_code} {response.text[:200]}")
    assert node.ran == [] and node.settled == [], "the card must not be charged for a refused call"


def test_a_topup_still_buys_a_call_it_covers_and_leaves_the_rest_on_a_key(node, monkeypatch):
    monkeypatch.setattr(node.mpp_payments, "stripe_configured", lambda: True)
    path, body = CHEAP
    response = _client(node).post(path, json=body,
                                  headers={"Authorization": f"Payment {_topup_credential(node)}"})
    assert response.status_code == 200, response.text
    assert node.settled == [("stripe", "50")]
    key = response.json().get("api_key")
    assert key and node.billing.lookup_key(key)["prepaid_balance_cents"] == 48


def test_a_credential_that_cannot_be_read_is_left_to_the_verifier_as_before(node):
    mpp = node.mpp_payments
    assert mpp.covers_price("not base64 at all", 10.0) is True
    assert mpp.covers_price("", 10.0) is True
    path, body = DEAR
    response = _client(node).post(path, json=body, headers={"Authorization": "Payment not-a-credential"})
    assert response.status_code == 402 and node.ran == []


def test_covers_price_reads_each_method_in_its_own_units(node):
    mpp = node.mpp_payments

    def cred(method, amount, intent="charge"):
        challenge = mpp._build_challenge("testserver", method, intent, {"amount": str(amount)})
        raw = json.dumps({"challenge": challenge, "payload": {}})
        return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()

    assert mpp.covers_price(cred("tempo", 20000), 0.02) is True
    assert mpp.covers_price(cred("tempo", 20000), 0.05) is False
    assert mpp.covers_price(cred("evm", 150000), 0.15) is True
    assert mpp.covers_price(cred("evm", 149999), 0.15) is False
    assert mpp.covers_price(cred("stripe", 50), 0.50) is True
    assert mpp.covers_price(cred("stripe", 50), 0.51) is False
    assert mpp.covers_price(cred("stripe", 50, intent="topup"), 0.05) is True
    assert mpp.covers_price(cred("stripe", 50, intent="topup"), 10.0) is False
    assert mpp.covers_price(cred("tempo", "not-a-number"), 0.02) is False
    assert mpp.covers_price(cred("tempo", 20000), None) is True


@pytest.mark.parametrize("amount", ["Infinity", "-Infinity", "1e999", "NaN", "null", "[5]", '{"a": 1}', '"lots"'])
def test_an_amount_that_is_not_a_finite_number_is_a_402_never_a_500(node, amount):
    """JSON lets a caller write Infinity. No signature is needed to get this
    far, so it must be refused cleanly on every paid route."""
    mpp = node.mpp_payments
    request_b64 = mpp._b64url_encode(('{"amount": %s}' % amount).encode())
    challenge = {"id": "x", "realm": "testserver", "method": "tempo", "intent": "charge",
                 "request": request_b64, "expires": "2099-01-01T00:00:00Z"}
    raw = json.dumps({"challenge": challenge, "payload": {}})
    credential = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    assert mpp.covers_price(credential, 0.15) is False
    for path, body in (("/audit/bundle", {"url": "https://example.com"}), CHEAP, DEAR):
        response = _client(node).post(path, json=body, headers={"Authorization": f"Payment {credential}"})
        assert response.status_code == 402, f"{path}: HTTP {response.status_code} {response.text[:160]}"
    assert node.ran == [] and node.settled == []
