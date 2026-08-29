import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MPP_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "mpp_payments.py"


def _load_mpp(monkeypatch, **env):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("MPP_STRIPE_NETWORK_PROFILE_ID", raising=False)
    monkeypatch.delenv("MPP_TEMPO_RPC_URL", raising=False)
    monkeypatch.delenv("MPP_TEMPO_TOKEN_ADDRESS", raising=False)
    monkeypatch.delenv("MPP_TEMPO_RECIPIENT_ADDRESS", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    spec = importlib.util.spec_from_file_location("mpp_payments", MPP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.stripe.api_key = env.get("STRIPE_SECRET_KEY")
    return module


def test_not_configured_offers_no_challenges(monkeypatch):
    module = _load_mpp(monkeypatch)
    assert module.is_configured() is False
    assert module.www_authenticate_headers(realm="api.example.com") == []


def _both_rails(monkeypatch, **extra):
    return _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_STRIPE_NETWORK_PROFILE_ID="profile_test123",
        MPP_TEMPO_RPC_URL="https://tempo-rpc.example.com",
        MPP_TEMPO_TOKEN_ADDRESS="0x20c0000000000000000000000000000000000000",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
        **extra,
    )


def test_configured_offers_both_method_challenges(monkeypatch):
    module = _both_rails(monkeypatch)
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.50)
    assert len(headers) == 2
    assert any('method="stripe"' in h for h in headers)
    assert any('method="tempo"' in h for h in headers)
    assert all('realm="api.example.com"' in h for h in headers)


def test_the_spt_rail_is_not_offered_below_stripes_minimum(monkeypatch):
    """Stripe rejects a card SPT charge under 0.50 USD outright.

    Every route here is priced at $0.03-$0.10, so offering this rail on them
    would hand an agent a challenge, take its single-use token, and fail at
    the API every time -- a rail advertised and unable to settle, which is the
    exact shape of the bug that made the x402 rail unpayable for months. Tempo
    has no such floor and is unaffected.
    """
    module = _both_rails(monkeypatch)
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.03)
    assert len(headers) == 1
    assert 'method="tempo"' in headers[0]

    entries = module.accepts_entries(price_usd=0.10)
    assert [entry["method"] for entry in entries] == ["tempo"]


def test_the_spt_floor_is_stripes_number_not_ours(monkeypatch):
    """A deployment whose Stripe account carries a different minimum says so
    with a variable, not a patch."""
    module = _both_rails(monkeypatch, MPP_STRIPE_MIN_CENTS="3")
    headers = module.www_authenticate_headers(realm="api.example.com", price_usd=0.03)
    assert any('method="stripe"' in h for h in headers)


def test_a_stale_challenge_under_the_minimum_is_not_charged(monkeypatch):
    """Challenges live for their whole TTL, so one minted before the floor
    existed can still arrive. Burning a caller's single-use SPT on a charge
    Stripe will reject is worse than refusing it."""
    module = _both_rails(monkeypatch)
    challenge = module._build_challenge(
        "api.example.com", "stripe", "charge", {"amount": "3", "currency": "usd"}
    )

    # Recorded rather than raised: _verify_stripe catches every exception and
    # fails closed, so an AssertionError in here would be swallowed and the
    # test would pass whether or not Stripe was called.
    calls = []
    monkeypatch.setattr(
        module.stripe.PaymentIntent,
        "create",
        lambda **kwargs: calls.append(kwargs) or {"status": "succeeded"},
    )
    assert module._verify_stripe(challenge, {"spt": "spt_123"}) is False
    assert calls == [], "a charge under Stripe's own minimum was sent anyway"


def test_challenge_binding_rejects_tampered_id(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    assert module._verify_challenge_binding(challenge, "api.example.com") is True

    tampered = dict(challenge)
    tampered["id"] = "x" + tampered["id"]
    assert module._verify_challenge_binding(tampered, "api.example.com") is False


def test_challenge_binding_rejects_wrong_realm(monkeypatch):
    # A challenge issued for one hostname must not validate against another
    # -- this is what stops a challenge from being replayed cross-deployment.
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    assert module._verify_challenge_binding(challenge, "evil.example.com") is False


def test_challenge_binding_rejects_expired(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "stripe", "charge", {"amount": "3"})
    expired = dict(challenge)
    expired["expires"] = "2020-01-01T00:00:00Z"
    expired["id"] = module._challenge_id(
        expired["realm"], expired["method"], expired["intent"], expired["request"], expired["expires"], None, None
    )
    assert module._verify_challenge_binding(expired, "api.example.com") is False


def test_verify_and_settle_rejects_malformed_credential(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    assert module.verify_and_settle_sync("not-valid-base64url-json", realm="api.example.com") is False


def test_verify_and_settle_rejects_unknown_method(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    challenge = module._build_challenge("api.example.com", "paypal", "charge", {"amount": "3"})
    credential = module._b64url_encode(json.dumps({"challenge": challenge, "payload": {}}).encode())
    assert module.verify_and_settle_sync(credential, realm="api.example.com") is False


def test_verify_tempo_rejects_non_hash_payload_types(monkeypatch):
    # "transaction" (pull mode) and "proof" (zero-amount) aren't implemented
    # -- must fail closed, not be silently treated as valid.
    module = _load_mpp(
        monkeypatch,
        MPP_TEMPO_RPC_URL="https://tempo-rpc.example.com",
        MPP_TEMPO_TOKEN_ADDRESS="0x20c0000000000000000000000000000000000000",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
        STRIPE_SECRET_KEY="sk_test_fake",
    )
    assert module._verify_tempo({}, {"type": "transaction", "signature": "0xabc"}) is False
    assert module._verify_tempo({}, {"type": "proof", "signature": "0xabc"}) is False
    assert module._verify_tempo({}, {}) is False


def test_verify_stripe_rejects_non_spt_token(monkeypatch):
    module = _load_mpp(monkeypatch, STRIPE_SECRET_KEY="sk_test_fake", MPP_STRIPE_NETWORK_PROFILE_ID="p")
    assert module._verify_stripe({}, {"spt": "not-an-spt"}) is False
    assert module._verify_stripe({}, {}) is False
