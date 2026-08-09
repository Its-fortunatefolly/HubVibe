import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"


def _load_main(monkeypatch, api_key="test-key"):
    if api_key is not None:
        monkeypatch.setenv("AUDIT_API_KEY", api_key)
    else:
        monkeypatch.delenv("AUDIT_API_KEY", raising=False)
    # No Stripe config in CI -- billing.is_configured() must be False, so
    # these tests only exercise the auth/rate-limit paths, never Firestore
    # or Stripe network calls.
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_METERED_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)

    spec = importlib.util.spec_from_file_location("wcag_audit_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_check(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "wcag-audit-engine"}


def test_landing_page_served_at_root(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_agent_manifest_advertises_payment(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/.well-known/agent.json")
    body = response.json()
    assert body["endpoints"][0]["payment_required"] is True


def test_audit_requires_api_key(monkeypatch):
    # Neither X-API-Key nor X-PAYMENT attached -- caller gets a 402 telling
    # them how to pay, not a silent pass and not a bare 401.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    assert response.status_code == 402
    body = response.json()
    assert body["price"] == "$0.03"
    assert body["accepted_payment_header"] == "X-PAYMENT"
    assert "payTo" in body


def test_audit_rejects_wrong_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/audit", json={"url": "https://example.com"}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 402


def test_audit_rejects_unbilled_key_when_billing_unconfigured(monkeypatch):
    # No AUDIT_API_KEY set and Stripe isn't configured -- every key must be
    # rejected rather than falling through to an unauthenticated Firestore
    # lookup or a fabricated pass.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(
        "/audit", json={"url": "https://example.com"}, headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 402


def test_audit_rejects_garbage_x_payment_header(monkeypatch):
    # A malformed/unsigned X-PAYMENT must fail closed exactly like a bad
    # API key -- never fall through to a free audit.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "not-a-real-payment"},
    )
    assert response.status_code == 402


def test_audit_rejects_x_payment_when_x402_unconfigured(monkeypatch):
    # No X402_FACILITATOR_URL / X402_PAY_TO_ADDRESS set in CI -- even a
    # well-formed-looking X-PAYMENT header must be rejected, never trusted
    # on its own.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    assert module.x402_payments.is_configured() is False
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "eyJmYWtlIjogInBheWxvYWQifQ=="},
    )
    assert response.status_code == 402


def test_authenticate_accepts_internal_key_without_x_payment(monkeypatch):
    # The existing Stripe/internal-key path must keep working untouched by
    # the x402/MPP additions -- it doesn't need an X-PAYMENT or Authorization
    # header at all.
    module = _load_main(monkeypatch, api_key="test-key")
    auth = module._authenticate("test-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "internal"


def test_authenticate_x402_success_grants_access(monkeypatch):
    # With a facilitator that confirms verification+settlement, a valid
    # X-PAYMENT alone (no API key) is sufficient.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.x402_payments, "verify_and_settle_sync", lambda header: True)
    auth = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "x402"


def test_authenticate_x402_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.x402_payments, "verify_and_settle_sync", lambda header: False)
    result = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_mpp_success_grants_access(monkeypatch):
    # A valid Authorization: Payment ... credential alone (no API key, no
    # X-PAYMENT) is sufficient when MPP verification succeeds.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: True)
    auth = module._authenticate(None, None, "Payment some-base64url-credential")
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "mpp"


def test_authenticate_mpp_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: False)
    result = module._authenticate(None, None, "Payment some-base64url-credential")
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_ignores_non_payment_authorization_scheme(monkeypatch):
    # An Authorization header using a different scheme (e.g. Bearer) must
    # never be handed to MPP verification -- only the literal "Payment "
    # prefix triggers it.
    module = _load_main(monkeypatch, api_key=None)
    calls = []
    monkeypatch.setattr(
        module.mpp_payments,
        "verify_and_settle_sync",
        lambda header, realm=None: calls.append(header) or False,
    )
    result = module._authenticate(None, None, "Bearer some-jwt")
    assert isinstance(result, module.JSONResponse)
    assert calls == []


def test_audit_accepts_mpp_authorization_header(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.mpp_payments, "verify_and_settle_sync", lambda header, realm=None: True)
    monkeypatch.setattr(
        module,
        "_run_axe",
        lambda html, url: {"violations": []},
    )
    client = TestClient(module.app)
    response = client.post(
        "/audit",
        json={"url": "https://example.com"},
        headers={"Authorization": "Payment some-base64url-credential"},
    )
    assert response.status_code == 200
    assert response.json()["pass"] is True


def test_402_response_advertises_mpp_challenges_when_configured(monkeypatch):
    # When MPP is configured, the 402 response must carry a
    # WWW-Authenticate: Payment header per spec, not just the x402 JSON body.
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(
        module.mpp_payments,
        "www_authenticate_headers",
        lambda realm=None: ["Payment id=\"x\", method=\"stripe\""],
    )
    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    assert response.status_code == 402
    assert response.headers["cache-control"] == "no-store"
    www_auth_values = response.headers.get_list("www-authenticate")
    assert any('method="stripe"' in v for v in www_auth_values)


def test_billing_endpoints_501_when_unconfigured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/billing/checkout", json={"email": "a@example.com"})
    assert response.status_code == 501


def test_rate_limit_rejects_after_threshold(monkeypatch):
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "RATE_LIMIT_PER_MINUTE", 2)
    module._check_rate_limit("some-key")
    module._check_rate_limit("some-key")
    with pytest.raises(Exception):
        module._check_rate_limit("some-key")


def test_free_scan_limit_rejects_after_threshold(monkeypatch):
    module = _load_main(monkeypatch)
    monkeypatch.setattr(module, "FREE_SCAN_LIMIT_PER_DAY", 2)
    module._check_free_scan_limit("1.2.3.4")
    module._check_free_scan_limit("1.2.3.4")
    with pytest.raises(Exception):
        module._check_free_scan_limit("1.2.3.4")
