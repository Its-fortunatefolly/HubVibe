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


def test_configured_offers_both_method_challenges(monkeypatch):
    module = _load_mpp(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_fake",
        MPP_STRIPE_NETWORK_PROFILE_ID="profile_test123",
        MPP_TEMPO_RPC_URL="https://tempo-rpc.example.com",
        MPP_TEMPO_TOKEN_ADDRESS="0x20c0000000000000000000000000000000000000",
        MPP_TEMPO_RECIPIENT_ADDRESS="0x742d35Cc6634C0532925a3b844Bc9e7595f8fE00",
    )
    headers = module.www_authenticate_headers(realm="api.example.com")
    assert len(headers) == 2
    assert any('method="stripe"' in h for h in headers)
    assert any('method="tempo"' in h for h in headers)
    assert all('realm="api.example.com"' in h for h in headers)


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
