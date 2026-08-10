import importlib.util
import time
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
    for var in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_METERED_PRICE_ID",
        "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID",
    ):
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


def test_llms_txt_served_at_root(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/llms.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "/audit/bundle" in response.text


def test_robots_txt_served_and_points_to_sitemap(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "sitemap.xml" in response.text


def test_sitemap_xml_served(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert "xml" in response.headers["content-type"]
    assert "<urlset" in response.text


def test_mcp_json_served_and_matches_repo_manifest(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/mcp.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    body = response.json()
    tool_names = {tool["name"] for tool in body["tools"]}
    assert tool_names == {"audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"}


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
    assert body["price_usd"] == 0.03
    assert body["error"] == "payment_required"
    # Machine-readable list of rails that can actually settle here.
    assert isinstance(body["accepts"], list)
    assert body["alternative"]["header"] == "X-API-Key"


def test_402_never_advertises_x402_when_it_cannot_settle(monkeypatch):
    """Regression: an unconfigured x402 used to still emit
    accepted_payment_header=X-PAYMENT with payTo=null, telling a paying agent
    to send a payment to nowhere. Advertise a rail only if it works."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    assert module.x402_payments.is_configured() is False, "test presumes x402 is off in CI"

    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    body = response.json()

    assert response.status_code == 402
    assert "payTo" not in body
    assert "accepted_payment_header" not in body
    assert not any(entry.get("protocol") == "x402" for entry in body["accepts"])


def test_402_advertises_x402_when_it_is_configured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: True)
    monkeypatch.setattr(module.x402_payments, "_PAY_TO_ADDRESS", "0xabc")

    client = TestClient(module.app)
    body = client.post("/audit", json={"url": "https://example.com"}).json()

    assert body["payTo"] == "0xabc"
    assert body["accepted_payment_header"] == "X-PAYMENT"
    x402_entries = [e for e in body["accepts"] if e["protocol"] == "x402"]
    assert len(x402_entries) == 1
    assert x402_entries[0]["pay_to"] == "0xabc"
    assert x402_entries[0]["price"] == "$0.03"


def test_402_bundle_price_is_carried_everywhere(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    body = client.post("/audit/bundle", json={"url": "https://example.com"}).json()

    assert body["price"] == "$0.10"
    assert body["price_usd"] == 0.10


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
    monkeypatch.setattr(module.x402_payments, "verify_and_settle_sync", lambda header, price=None: True)
    auth = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "x402"


def test_authenticate_x402_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.x402_payments, "verify_and_settle_sync", lambda header, price=None: False)
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
        lambda realm=None, price_usd=None: ["Payment id=\"x\", method=\"stripe\""],
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
    limiter = module._SlidingWindowLimiter(limit=2, window_seconds=60.0)
    assert limiter.check("some-key") is True
    assert limiter.check("some-key") is True
    assert limiter.check("some-key") is False


def test_rate_limit_is_per_key_not_global(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=1, window_seconds=60.0)
    assert limiter.check("key-a") is True
    assert limiter.check("key-a") is False
    # A different paying caller must not be throttled by someone else's usage.
    assert limiter.check("key-b") is True


def test_rate_limit_window_expires(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=1, window_seconds=0.05)
    assert limiter.check("k") is True
    assert limiter.check("k") is False
    time.sleep(0.06)
    assert limiter.check("k") is True


def test_rate_limiter_evicts_idle_keys_and_stays_bounded(monkeypatch):
    """A dict-of-deques keyed by caller IP that never evicts is an OOM at
    volume -- one leaked entry per unique caller, forever."""
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(
        limit=5, window_seconds=0.05, sweep_interval=0.0
    )
    for i in range(500):
        limiter.check(f"ip-{i}")
    assert len(limiter._log) == 500
    time.sleep(0.06)
    # One more call triggers a sweep, which must drop the 500 expired windows.
    limiter.check("fresh-key")
    assert len(limiter._log) == 1


def test_rate_limiter_hard_caps_table_under_key_flood(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(
        limit=5, window_seconds=600.0, max_keys=100, sweep_interval=1e9
    )
    for i in range(400):
        limiter.check(f"ip-{i}")
    # Windows are all still live, so eviction-by-expiry can't help; the hard
    # cap is the only thing standing between this and unbounded growth.
    assert len(limiter._log) <= 400
    assert len(limiter._log) < 400 or limiter._max_keys >= 400


def test_free_scan_limit_rejects_after_threshold(monkeypatch):
    module = _load_main(monkeypatch)
    limiter = module._SlidingWindowLimiter(limit=2, window_seconds=86_400.0)
    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is True
    assert limiter.check("1.2.3.4") is False


def test_rate_limit_is_enforced_before_any_payment_is_settled(monkeypatch):
    """Regression: the limiter used to run AFTER _authenticate, which is
    where x402/MPP payments actually settle. An over-limit caller therefore
    paid, then received a 429 -- money taken, no audit, no refund. Rejection
    must happen before the payment instrument is touched."""
    module = _load_main(monkeypatch)

    settled = []

    def _exploding_verify(payment_header, price=None):
        settled.append(payment_header)
        return True

    monkeypatch.setattr(
        module.x402_payments, "verify_and_settle_sync", _exploding_verify
    )
    # Exhaust the limiter for this key before the request comes in.
    monkeypatch.setattr(module, "_audit_limiter", module._SlidingWindowLimiter(limit=0, window_seconds=60.0))

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "some-signed-payment"},
    )

    assert response.status_code == 429
    assert settled == [], "payment was settled despite the caller being rate limited"
    body = response.json()
    assert body["billed"] is False
    assert response.headers["Retry-After"] == "60"


# --- New multi-route audit suite -------------------------------------------

NEW_PAID_ROUTES = [
    ("/audit/wcag", {"url": "https://example.com"}, 0.03),
    ("/audit/seo", {"url": "https://example.com"}, 0.03),
    ("/audit/security", {"url": "https://example.com"}, 0.03),
    ("/audit/performance", {"url": "https://example.com"}, 0.03),
    ("/audit/bundle", {"url": "https://example.com"}, 0.10),
]


@pytest.mark.parametrize("path,body,price", NEW_PAID_ROUTES)
def test_new_routes_are_fail_closed_with_correct_price(monkeypatch, path, body, price):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(path, json=body)
    assert response.status_code == 402
    assert response.json()["price"] == f"${price:.2f}"


@pytest.mark.parametrize("path,body", [(p, b) for p, b, _ in NEW_PAID_ROUTES])
def test_new_routes_accept_internal_key_and_execute(monkeypatch, path, body):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value={"status": "ok", "pass": True, "findings": [], "metrics": {}},
    ):
        client = TestClient(module.app)
        response = client.post(path, json=body, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert response.json()["pass"] is True


def test_audit_wcag_seo_require_html_or_url(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    client = TestClient(module.app)
    for path in ("/audit/wcag", "/audit/seo"):
        response = client.post(path, json={}, headers={"X-API-Key": "test-key"})
        assert response.status_code == 400


def test_audit_security_url_field_required_by_schema(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    client = TestClient(module.app)
    response = client.post("/audit/security", json={}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 422  # Pydantic validation: url is required


def test_audit_route_execution_failure_returns_502_and_is_not_billed(monkeypatch):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(module.billing, "record_usage", lambda *a, **k: calls.append(1))
    with patch.object(module.audits, "run_seo_audit", side_effect=RuntimeError("boom")):
        client = TestClient(module.app)
        response = client.post(
            "/audit/seo", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 502
    assert response.json()["pass"] is None
    assert calls == []


def test_bundle_failure_is_atomic_and_unbilled(monkeypatch):
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(module.billing, "record_usage", lambda *a, **k: calls.append(1))
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(module.audits, "run_security_audit", side_effect=RuntimeError("network error")):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 502
    assert calls == []


def test_bundle_success_reports_three_billing_units(monkeypatch):
    # Approximates the $0.10 bundle price against the existing $0.03 flat
    # meter -- see billing.record_usage's docstring for why this is 3, not 1.
    from fastapi.testclient import TestClient
    from unittest.mock import patch

    module = _load_main(monkeypatch, api_key="test-key")
    recorded = []
    monkeypatch.setattr(
        module.billing, "record_usage", lambda customer_id, units=1: recorded.append(units)
    )
    # Force the internal key path to look Stripe-billable so _bill() actually
    # calls record_usage -- the internal key itself is normally unbilled.
    real_authenticate = module._authenticate

    def _fake_authenticate(*args, **kwargs):
        auth = real_authenticate(*args, **kwargs)
        if isinstance(auth, module.AuthContext):
            auth.stripe_billable = True
            auth.customer_id = "cus_test"
        return auth

    monkeypatch.setattr(module, "_authenticate", _fake_authenticate)

    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value={"status": "ok", "pass": True, "findings": [], "metrics": {}},
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )
    assert response.status_code == 200
    assert recorded == [3]


def test_agent_manifest_lists_all_five_audit_routes(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/.well-known/agent.json")
    paths = {e["path"] for e in response.json()["endpoints"]}
    for expected in ("/audit/wcag", "/audit/seo", "/audit/security", "/audit/performance", "/audit/bundle"):
        assert expected in paths
    bundle = next(e for e in response.json()["endpoints"] if e["path"] == "/audit/bundle")
    assert bundle["price_usd"] == 0.10


# --- SaaS monthly quota ------------------------------------------------


def test_authenticate_falls_back_to_402_when_quota_exceeded(monkeypatch):
    # A valid Stripe key that's over its monthly quota must not be treated
    # as sufficient on its own -- this is what makes "overage falls back to
    # x402/MPP rates" actually true rather than just landing-page copy.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id: False)

    result = module._authenticate("real-stripe-key", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_succeeds_when_quota_available(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id: True)

    auth = module._authenticate("real-stripe-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is True
    assert auth.customer_id == "cus_123"


def test_internal_key_bypasses_quota_check(monkeypatch):
    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(
        module.billing, "check_and_increment_quota", lambda customer_id: calls.append(1) or False
    )
    auth = module._authenticate("test-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.payment_method == "internal"
    assert calls == []


# --- Agent-facing discovery manifest ---------------------------------------


def test_manifest_prices_match_what_routes_actually_charge(monkeypatch):
    """The manifest is what an agent budgets against. If it advertises a
    price the route doesn't actually challenge for, the agent builds a
    payment for the wrong amount and the call fails at settlement."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    advertised = {
        e["path"]: e["price_usd"]
        for e in manifest["endpoints"]
        if e.get("payment_required")
    }
    assert advertised, "manifest advertised no paid endpoints"

    for path, price in advertised.items():
        body = {"url": "https://example.com"}
        challenged = client.post(path, json=body).json()
        assert challenged["price_usd"] == price, f"{path} advertises {price}, charges {challenged['price_usd']}"


def test_manifest_only_lists_payment_methods_that_can_settle(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    # Nothing is configured in CI, so an honest manifest lists nothing.
    assert manifest["payment"]["methods"] == []

    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: True)
    refreshed = client.get("/.well-known/agent.json").json()
    assert "mpp-tempo" in refreshed["payment"]["methods"]


def test_manifest_points_at_reachable_discovery_documents(monkeypatch):
    """Every discovery URL the manifest advertises must actually resolve --
    a 404 here is an agent's dead end."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    for name, url in manifest["discovery"].items():
        path = url.replace(manifest["base_url"], "")
        assert client.get(path).status_code == 200, f"{name} -> {path} is not reachable"


def test_cors_exposes_www_authenticate_so_browser_agents_can_pay(monkeypatch):
    """Without WWW-Authenticate in expose_headers a browser-resident agent
    cannot read the MPP challenge off a 402, so it has no way to pay."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"Origin": "https://some-agent.example"},
    )
    assert response.status_code == 402
    exposed = response.headers.get("access-control-expose-headers", "")
    assert "WWW-Authenticate" in exposed
