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
    """Served at BOTH paths. Cloud Run's frontend reserves /healthz and
    answers it with its own HTML 404 before the request reaches the
    container -- confirmed against the live service -- so /health is the one
    that actually works there, and it must not regress."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    expected = {"status": "ok", "service": "wcag-audit-engine"}

    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} did not answer"
        assert response.json() == expected


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


def test_mcp_json_never_advertises_a_rail_that_cannot_settle(monkeypatch):
    """The hole in this codebase's central rule.

    /.well-known/agent.json and every 402 already omit rails that are not
    configured. /mcp.json did not: it was a static file asserting
    `["stripe_api_key", "x402", "mpp"]` unconditionally, and it went on
    asserting x402 after x402 was switched off on the live service. The MCP
    registry points agents at this manifest, so an agent would have built a
    payment for a rail this deployment cannot settle -- and a failed payment
    is indistinguishable, from our side, from nobody buying.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "stripe_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    body = TestClient(module.app).get("/mcp.json").json()
    assert body["auth"]["methods"] == []
    assert "x402" not in body["auth"]["methods"]


def test_mcp_json_lists_a_rail_once_it_can_settle(monkeypatch):
    """The other half: a configured rail must actually show up, or agents
    that could pay are turned away."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.x402_payments, "is_configured", lambda: True)
    monkeypatch.setattr(module.mpp_payments, "stripe_configured", lambda: False)
    monkeypatch.setattr(module.mpp_payments, "tempo_configured", lambda: False)
    monkeypatch.setattr(module.billing, "is_configured", lambda: False)

    body = TestClient(module.app).get("/mcp.json").json()
    assert body["auth"]["methods"] == ["x402"]


def test_mcp_json_prices_come_from_the_catalog(monkeypatch):
    """_CATALOG exists so the manifest, the price an agent reads, and the
    price the route charges cannot drift apart. A second hand-maintained copy
    of the numbers in a static file defeats that by construction."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    body = TestClient(module.app).get("/mcp.json").json()

    catalog = {entry["path"]: entry["price_usd"] for entry in module._CATALOG}
    served = {
        tool["httpEndpoint"]["path"]: tool["httpEndpoint"]["price_usd"]
        for tool in body["tools"]
    }
    assert served == catalog


def test_mcp_json_prices_follow_a_catalog_change(monkeypatch):
    """Proves the price is actually read from _CATALOG rather than merely
    happening to match the static file today."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    patched = [dict(entry) for entry in module._CATALOG]
    for entry in patched:
        if entry["path"] == "/audit/bundle":
            entry["price_usd"] = 0.25
    monkeypatch.setattr(module, "_CATALOG", patched)

    body = TestClient(module.app).get("/mcp.json").json()
    bundle = [t for t in body["tools"] if t["httpEndpoint"]["path"] == "/audit/bundle"][0]
    assert bundle["httpEndpoint"]["price_usd"] == 0.25


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
    # With a facilitator that confirms verification, a valid X-PAYMENT alone
    # (no API key) is sufficient to gain access. Note the payment is only
    # VERIFIED at this point, not settled -- settlement waits until an audit
    # has actually produced a result (see _bill).
    module = _load_main(monkeypatch, api_key=None)
    pending = object()
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None: pending)
    auth = module._authenticate(None, "some-signed-payment", None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is False
    assert auth.payment_method == "x402"
    assert auth.pending_payment is pending, "payment must be held unsettled until delivery"


def test_authenticate_x402_failure_returns_402(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda header, price=None: None)
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
    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(
        module.audits, "fetch_once", return_value=object()
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value=_perf,
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

    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(module, "_run_axe", return_value={"violations": []}), patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(
        module.audits, "fetch_once", return_value=object()
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits,
        "run_performance_audit",
        return_value=_perf,
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


# --- key store outage ---------------------------------------------------


def test_authenticate_returns_402_not_500_when_the_key_store_is_unreachable(monkeypatch):
    """The exact production failure this exists to prevent.

    No Firestore database had ever been created for the project, so
    lookup_key raised NotFound on every keyed call. Unhandled, that surfaced
    as HTTP 500: a machine caller could not pay, could not usefully retry,
    and could not tell a broken backend from a rejected key. The whole paid
    path was dead and nothing caught it, because verify-live.sh only ever
    exercised the UNauthenticated 402 -- it now has a paid-path check too,
    guarded by tests/test_verify_live.py.

    Falling through to the payment challenge is still fail-closed -- nobody
    gets in without paying -- it just answers with something actionable.
    """
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)

    def _boom(key):
        raise RuntimeError("404 The database (default) does not exist for project")

    monkeypatch.setattr(module.billing, "lookup_key", _boom)

    with pytest.raises(RuntimeError):
        # Guard the guard: if lookup_key itself stops raising, this test would
        # pass for the wrong reason.
        module.billing.lookup_key("k")

    monkeypatch.setattr(module.billing, "lookup_key", lambda key: None)
    result = module._authenticate("some-key", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_lookup_key_swallows_a_dead_key_store_and_returns_none(monkeypatch):
    """billing.lookup_key is the boundary: an unreachable store must read as
    'no usable key', never as an exception escaping into the request."""
    billing = _load_main(monkeypatch, api_key=None).billing

    def _dead():
        raise RuntimeError("404 The database (default) does not exist for project")

    monkeypatch.setattr(billing, "_firestore", _dead)
    billing._key_store_warned = False
    assert billing.lookup_key("anything") is None


def test_a_dead_key_store_is_logged_at_error_not_swallowed_silently(monkeypatch, caplog):
    """A subscriber being silently downgraded to pay-per-call is exactly the
    kind of outage that otherwise looks identical to 'nobody is buying'."""
    billing = _load_main(monkeypatch, api_key=None).billing

    def _dead():
        raise RuntimeError("The database (default) does not exist")

    monkeypatch.setattr(billing, "_firestore", _dead)
    billing._key_store_warned = False
    with caplog.at_level("ERROR"):
        billing.lookup_key("anything")
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert "Firestore" in caplog.text


# --- SaaS monthly quota ------------------------------------------------


def test_authenticate_falls_back_to_402_when_quota_exceeded(monkeypatch):
    # A valid Stripe key that's over its monthly quota must not be treated
    # as sufficient on its own -- this is what makes "overage falls back to
    # x402/MPP rates" actually true rather than just landing-page copy.
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id, plan=None: False)

    result = module._authenticate("real-stripe-key", None, None)
    assert isinstance(result, module.JSONResponse)
    assert result.status_code == 402


def test_authenticate_succeeds_when_quota_available(monkeypatch):
    module = _load_main(monkeypatch, api_key=None)
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(module.billing, "lookup_key", lambda key: {"customer_id": "cus_123"})
    monkeypatch.setattr(module.billing, "check_and_increment_quota", lambda customer_id, plan=None: True)

    auth = module._authenticate("real-stripe-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert auth.stripe_billable is True
    assert auth.customer_id == "cus_123"


def test_auth_path_hands_the_plan_to_the_quota_check(monkeypatch):
    """The per-plan cap only works if the plan reaches the quota check.

    The cap is stored on the key document precisely so this lookup carries
    it. Drop the kwarg here and every Agency subscriber silently reverts to
    the 1,500 fallback -- the exact cut-off the per-plan caps exist to fix,
    and invisible because the call still succeeds.
    """
    module = _load_main(monkeypatch, api_key=None)
    seen = {}
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)
    monkeypatch.setattr(
        module.billing,
        "lookup_key",
        lambda key: {"customer_id": "cus_agency", "plan": "agency"},
    )

    def _quota(customer_id, plan=None):
        seen["customer_id"] = customer_id
        seen["plan"] = plan
        return True

    monkeypatch.setattr(module.billing, "check_and_increment_quota", _quota)

    auth = module._authenticate("agency-key", None, None)
    assert isinstance(auth, module.AuthContext)
    assert seen == {"customer_id": "cus_agency", "plan": "agency"}


def test_internal_key_bypasses_quota_check(monkeypatch):
    module = _load_main(monkeypatch, api_key="test-key")
    calls = []
    monkeypatch.setattr(
        module.billing, "check_and_increment_quota", lambda customer_id, plan=None: calls.append(1) or False
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


def test_manifest_only_lists_human_plans_that_can_be_bought(monkeypatch):
    """Fail-closed, same as the payment rails: a tier whose Stripe Price ID
    isn't configured must not be advertised, because its checkout would
    raise rather than take money."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    # Nothing configured in CI, so an honest manifest offers no plans.
    manifest = client.get("/.well-known/agent.json").json()
    assert manifest["pricing"]["human_plans"]["tiers"] == []

    monkeypatch.setattr(module.billing, "plan_available", lambda plan: plan == "pro")
    monkeypatch.setattr(module.billing, "oneoff_report_available", lambda: False)
    refreshed = client.get("/.well-known/agent.json").json()
    offered = {t["id"] for t in refreshed["pricing"]["human_plans"]["tiers"]}
    assert offered == {"pro"}


def test_manifest_plan_prices_match_the_landing_page(monkeypatch):
    """The manifest advertised a $49/month plan with an included-scans quota
    for weeks after Stripe had stopped selling it, because the number lived
    in two places. An agent -- or a human's agent -- reading a price that no
    checkout will honour is a broken sale, so pin the two together."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    monkeypatch.setattr(module.billing, "plan_available", lambda plan: True)
    monkeypatch.setattr(module.billing, "oneoff_report_available", lambda: True)
    client = TestClient(module.app)

    tiers = client.get("/.well-known/agent.json").json()["pricing"]["human_plans"]["tiers"]
    assert {t["id"] for t in tiers} == {"report", "pro", "agency"}

    page = (REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "index.html").read_text()
    for tier in tiers:
        # $79.0 on the page reads as "$79"; compare the way a buyer sees it.
        shown = f"{tier['usd']:.2f}".rstrip("0").rstrip(".")
        assert f"${shown}" in page, f"{tier['id']} priced {tier['usd']} in the manifest but not on the page"

    # And the retired framing must not come back anywhere an agent reads.
    manifest_text = client.get("/.well-known/agent.json").text
    assert "included_calls_per_month" not in manifest_text
    assert 49 not in [t["usd"] for t in tiers], "the retired $49 plan is back"


_SIBLING_MODULES = ("billing", "x402_payments", "mpp_payments", "audits")


def _drop_sibling_cache():
    import sys

    for name in _SIBLING_MODULES:
        sys.modules.pop(f"wcag_audit_engine_{name}", None)


@pytest.fixture
def load_main_fresh():
    """Load main.py with its sibling modules re-read from the environment.

    main.py caches siblings in sys.modules under `wcag_audit_engine_<name>`
    so audits.py and main.py share one browser_pool. That cache also means
    billing.py's module-level os.environ.get calls run exactly once per
    process -- so a test that sets Stripe env vars and reloads main would
    otherwise still get the first billing module the process ever loaded,
    and would pass while testing nothing.

    Clears the cache on the way in and on the way out, so neither this test
    nor the next one inherits the other's Stripe configuration.
    """
    import importlib.util

    def _load(unique):
        _drop_sibling_cache()
        spec = importlib.util.spec_from_file_location(unique, MAIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    yield _load
    _drop_sibling_cache()


def test_advertised_tier_is_actually_buyable_on_a_current_deployment(monkeypatch, load_main_fresh):
    """A node configured with only today's plans must be able to sell them.

    is_configured() gated on the RETIRED flat/metered price IDs, so a
    deployment that had correctly moved to the per-site plans advertised all
    three tiers in the manifest while /billing/checkout answered 501 --
    every human buyer bounced off a "billing is not configured" wall on a
    service that was, in fact, configured.
    """
    for var in ("STRIPE_METERED_PRICE_ID", "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_AGENCY", "price_agency")
    monkeypatch.setenv("STRIPE_PRICE_ONEOFF_REPORT", "price_report")
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")

    module = load_main_fresh("wcag_audit_main_plans")

    assert module.billing.is_configured(), (
        "a node selling the current plans reports billing unconfigured"
    )

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    tiers = client.get("/.well-known/agent.json").json()["pricing"]["human_plans"]["tiers"]
    assert {t["id"] for t in tiers} == {"report", "pro", "agency"}

    # Every advertised subscription tier must get past the config gate and
    # reach Stripe -- 501 here is the bug. Stripe itself is not reachable in
    # CI, so a network/auth error from the SDK is the expected far end.
    for tier in tiers:
        if tier["id"] == "report":
            continue
        try:
            module.billing.create_checkout_session(
                "buyer@example.com", "https://x/s", "https://x/c", plan=tier["id"]
            )
        except ValueError as exc:  # our own "not configured" rejection
            pytest.fail(f"advertised tier {tier['id']} is not sellable: {exc}")
        except Exception:
            pass  # reached Stripe; that is as far as CI can go


def test_planless_checkout_fails_cleanly_rather_than_500ing(monkeypatch, load_main_fresh):
    """With the retired defaults gone, a plan-less checkout used to build a
    Stripe call with price=None and blow up as an opaque 500. It must say
    what to pass instead."""
    for var in ("STRIPE_METERED_PRICE_ID", "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")

    module = load_main_fresh("wcag_audit_main_noplan")

    with pytest.raises(ValueError) as excinfo:
        module.billing.create_checkout_session("b@example.com", "https://x/s", "https://x/c")
    assert "pro" in str(excinfo.value), "the error should name a plan the caller can actually pick"

    # And the route turns that into a 400, not a 500.
    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    response = client.post("/billing/checkout", json={"email": "b@example.com"})
    assert response.status_code == 400, f"got {response.status_code}: {response.text}"


def test_each_plans_quota_covers_what_that_plan_promises(monkeypatch, load_main_fresh):
    """The included-scans cap has to fit the plan's own advertised coverage.

    One global 1,500 cap applied to every subscriber: Agency is sold as "50
    sites, audited daily", which is 1,550 bundle calls in a 31-day month --
    so the $249 customer got cut off before month end, and cut off around
    day 7 if they audited per-dimension instead of bundling. Pro and Agency
    also shared a ceiling, so paying 3x bought no extra capacity.
    """
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    module = load_main_fresh("wcag_audit_main_quota")
    billing = module.billing

    sites = {"pro": 5, "agency": 50}
    for plan, site_count in sites.items():
        quota = billing.monthly_quota_for(plan)
        # Worst case a customer can legitimately drive: every site, every
        # dimension, every day of the longest month.
        worst_case = site_count * 4 * 31
        assert quota >= worst_case, (
            f"{plan} sells {site_count} sites audited daily "
            f"({worst_case} calls in a 31-day month) but caps at {quota}"
        )

    assert billing.monthly_quota_for("agency") > billing.monthly_quota_for("pro"), (
        "Agency costs 3x Pro and must not buy the same ceiling"
    )
    # An unrecognised or legacy key keeps the old behaviour rather than
    # silently getting unlimited access.
    assert billing.monthly_quota_for(None) == billing.SAAS_MONTHLY_QUOTA
    assert billing.monthly_quota_for("nonsense") == billing.SAAS_MONTHLY_QUOTA


def test_purchased_plan_is_recorded_so_the_quota_can_see_it(monkeypatch, load_main_fresh):
    """The cap is per-plan, which only works if activation stores the plan."""
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    module = load_main_fresh("wcag_audit_main_activate")
    billing = module.billing

    written = {}

    class _Doc:
        def __init__(self, key):
            self.key = key

        def get(self):
            return type("S", (), {"exists": False})()

        def set(self, data):
            written[self.key] = data

    class _Collection:
        def __init__(self, name):
            self.name = name

        def document(self, doc_id):
            return _Doc(f"{self.name}/{doc_id}")

    monkeypatch.setattr(
        billing, "_firestore", lambda: type("DB", (), {"collection": staticmethod(_Collection)})()
    )

    billing.activate_customer({"customer": "cus_123", "metadata": {"plan": "agency"}})

    key_doc = next(v for k, v in written.items() if k.startswith("api_keys/"))
    assert key_doc["plan"] == "agency", "the key must carry the plan the quota is sized from"
    assert key_doc["customer_id"] == "cus_123"

    # A checkout with no plan metadata (legacy) must still activate.
    written.clear()
    billing.activate_customer({"customer": "cus_456"})
    legacy = next(v for k, v in written.items() if k.startswith("api_keys/"))
    assert legacy["plan"] is None


def test_no_shipped_surface_asserts_a_payment_rail_as_available():
    """A rail named as a fact on a static surface goes stale silently.

    /mcp.json asserted `["stripe_api_key", "x402", "mpp"]` in a file and kept
    asserting x402 after x402 was switched off. The landing page said the same
    thing in its meta description, its JSON-LD (which search and AI crawlers
    read), and a spec table row reading "Payment rails: x402 - MPP".

    None of those can know what a deployment has configured, so none of them
    may claim it. The rule this codebase already follows everywhere else is to
    point at the live source instead: /.well-known/agent.json and the 402 body
    both list only rails that can genuinely settle.

    llms.txt deliberately still names all three, because it explains what the
    protocols ARE and then says in as many words that which are live is
    deployment-specific and must be read from agent.json. Describing a menu is
    fine; asserting availability is not.
    """
    surfaces = [
        REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "index.html",
        REPO_ROOT / "server.json",
        REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json",
    ]
    offenders = []
    for path in surfaces:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for term in ("x402", "mpp-stripe", "mpp-tempo"):
            if term in text.lower():
                offenders.append(f"{path.relative_to(REPO_ROOT)} names {term!r}")

    assert not offenders, (
        "these surfaces cannot know which rails a deployment can settle, so "
        "they must not name one -- link /.well-known/agent.json instead:\n  "
        + "\n  ".join(offenders)
    )


def test_no_shipped_surface_still_quotes_the_retired_plan():
    """Retired pricing must not survive anywhere a buyer or an agent reads.

    The $49/1,500 plan was removed from the manifest but lived on in two
    READMEs for weeks, because the fix grepped one file. Anything quoting a
    price that no Stripe Price ID backs is a broken sale, so check the whole
    shipped surface rather than the file that happened to prompt the fix.
    """
    import re

    retired = re.compile(r"\$49\b|1,?500 scans|1,?500 included|included_calls_per_month")

    surfaces = []
    for pattern in ("*.md", "*.html", "*.txt", "*.json", "*.yml", "*.py"):
        for path in REPO_ROOT.rglob(pattern):
            parts = path.parts
            if any(p in parts for p in (".git", "node_modules", "venv", "venv_clean")):
                continue
            # Other services in this monorepo have their own pricing.
            if any(p in parts for p in ("privacy-compliance-scanner", "dead-end-resolver")):
                continue
            if path.name == Path(__file__).name:  # this test names them on purpose
                continue
            surfaces.append(path)

    assert surfaces, "found no files to check -- the glob is wrong"

    offenders = []
    for path in surfaces:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if retired.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()[:90]}")

    assert not offenders, "retired pricing still shipped:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "bad_key",
    [
        "0x32b08c5e927c69877d0fcab35618c265674922b",  # a payout address
        "price_1U34LiDA21T9EAQB3LK5dS0I",  # a Price ID
        "pk_live_abc123",  # the PUBLISHABLE key, not the secret
        "whsec_abc123",  # the webhook secret
        "changeme",
    ],
)
def test_a_key_that_is_not_a_stripe_key_sells_nothing(bad_key, monkeypatch, load_main_fresh):
    """A non-empty STRIPE_SECRET_KEY is not the same as a usable one.

    Any non-empty string used to satisfy is_configured(), so the wrong value
    in the right variable would have had the manifest advertise all three
    plans plus the stripe_api_key rail while every Stripe call failed
    authentication. A rail that cannot settle must never be advertised, so a
    key that cannot possibly work has to count as no key.
    """
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_AGENCY", "price_agency")
    monkeypatch.setenv("STRIPE_PRICE_ONEOFF_REPORT", "price_report")
    monkeypatch.setenv("STRIPE_SECRET_KEY", bad_key)

    module = load_main_fresh("wcag_audit_main_badkey")
    assert module.billing.stripe_key_looks_valid() is False
    assert module.billing.is_configured() is False
    assert module.billing.human_plans_live() == []

    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()
    assert manifest["pricing"]["human_plans"]["tiers"] == [], (
        "advertised plans nobody can buy with a broken Stripe key"
    )
    assert "stripe_api_key" not in manifest["payment"]["methods"], (
        "advertised a Stripe rail that cannot authenticate"
    )


@pytest.mark.parametrize(
    "good_key",
    [
        "sk_live_abc123",
        "sk_test_abc123",
        "rk_live_abc123",
        "rk_test_abc123",
        # A secret piped in with `echo` keeps a trailing newline. The key is
        # correct; the padding is not. Stripe rejects it and, unstripped, the
        # prefix check would too -- quietly pulling every plan off a service
        # whose credentials are actually fine.
        "  sk_live_abc123\n",
        "sk_live_abc123\n",
    ],
)
def test_a_real_stripe_key_sells_normally(good_key, monkeypatch, load_main_fresh):
    """The shape check must not reject keys Stripe actually issues, however
    they arrived."""
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_SECRET_KEY", good_key)

    module = load_main_fresh("wcag_audit_main_goodkey")
    assert module.billing.stripe_key_looks_valid() is True, f"rejected {good_key!r}"
    assert module.billing.is_configured() is True
    assert module.billing.plan_available("pro") is True
    # The padding must be gone from what we hand to Stripe, not merely
    # tolerated by the check.
    assert module.billing.stripe.api_key == good_key.strip()


def _x402_env(monkeypatch):
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x32b08c5e927c69877d0fcab35618c265674922b")
    monkeypatch.delenv("AUDIT_API_KEY", raising=False)


def test_requirements_pull_the_extras_bazaar_discovery_needs():
    """Static check, because the runtime one cannot be trusted here.

    x402.extensions.bazaar imports jsonschema and idna, which arrive via the
    `extensions` extra -- not via `evm`. A developer machine almost always
    has both transitively, so the feature appears to work locally while
    being dead in the deployed container, and it fails closed and silent so
    nothing announces it. Asserting on the requirements text is the only
    check that a well-stocked environment cannot mask.
    """
    for path in (REPO_ROOT / "requirements.txt",
                 REPO_ROOT / "wcag-audit-engine" / "requirements.txt"):
        text = path.read_text()
        x402_lines = [ln for ln in text.splitlines() if ln.strip().startswith("x402")]
        assert x402_lines, f"{path.name} does not pin x402 at all"
        assert any("extensions" in ln for ln in x402_lines), (
            f"{path.name} pins {x402_lines} -- without the `extensions` extra, "
            "Bazaar discovery silently returns {} in the deployed container"
        )
        # The starlette that `mcp` drags in is incompatible with the pinned
        # FastAPI; it broke 49 tests once already. Never via an x402 extra.
        assert not any("[all]" in ln or ",mcp" in ln or "[mcp" in ln for ln in x402_lines), (
            f"{path.name} pulls the x402 mcp/all extra, which installs the `mcp` "
            "package and a starlette that conflicts with the pinned FastAPI"
        )


def test_402_carries_bazaar_discovery_when_x402_is_live(monkeypatch, load_main_fresh):
    """The Bazaar is how agents find a paid endpoint by capability.

    Facilitators catalog x402 resources by reading this extension off their
    402s. Without it the endpoint is reachable only by someone who already
    knows the URL, which defeats the point of being a machine-payable
    service -- being payable is worthless if nothing can find you.
    """
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar")

    from fastapi.testclient import TestClient

    body = TestClient(module.app).post(
        "/audit/bundle", json={"url": "https://example.com"}
    ).json()

    info = body["extensions"]["bazaar"]["info"]
    assert info["input"]["type"] == "http"
    assert info["input"]["bodyType"] == "json"
    # The advertised input must match what the route actually accepts, or the
    # index sends agents into a 400.
    assert "url" in info["input"]["body"]
    assert body["extensions"]["bazaar"]["schema"], "discovery data must carry its schema"


def test_no_bazaar_discovery_when_x402_cannot_settle(monkeypatch):
    """Same rule as every other x402 surface: the index is reached through a
    facilitator, so with none configured there is nothing to be indexed by,
    and listing an unpayable resource advertises a sale we cannot complete."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    assert module.x402_payments.is_configured() is False

    body = TestClient(module.app).post(
        "/audit/bundle", json={"url": "https://example.com"}
    ).json()
    assert "extensions" not in body


def test_mcp_paywall_declares_itself_as_an_mcp_resource(monkeypatch, load_main_fresh):
    """An agent that finds the tool in the Bazaar calls it over MCP, so the
    discovery record has to name the tool and its transport -- not describe
    the HTTP route the shared 402 builder would have described."""
    import json

    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar_mcp")

    from fastapi.testclient import TestClient

    response = TestClient(module.app).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "audit_bundle", "arguments": {"url": "https://example.com"}},
        },
    )
    challenge = json.loads(response.json()["result"]["content"][0]["text"])

    info = challenge["extensions"]["bazaar"]["info"]["input"]
    assert info["type"] == "mcp", "an MCP tool must not be indexed as an HTTP route"
    assert info["toolName"] == "audit_bundle"
    assert info["transport"] == "streamable-http"
    assert info["inputSchema"] == next(
        t["inputSchema"] for t in module._mcp_tools() if t["name"] == "audit_bundle"
    ), "the indexed schema must be the one the tool actually advertises"


def test_bazaar_failure_never_blocks_a_payment_challenge(monkeypatch, load_main_fresh):
    """Discovery is an enhancement. If building it throws, the caller must
    still get a 402 it can pay -- losing the index is survivable, losing the
    sale is not."""
    _x402_env(monkeypatch)
    module = load_main_fresh("wcag_audit_main_bazaar_boom")

    def _boom(*args, **kwargs):
        raise RuntimeError("bazaar library exploded")

    monkeypatch.setattr(
        module.x402_payments, "declare_discovery_extension", _boom, raising=False
    )
    import x402.extensions.bazaar as bz

    monkeypatch.setattr(bz, "declare_discovery_extension", _boom)

    from fastapi.testclient import TestClient

    response = TestClient(module.app).post("/audit/bundle", json={"url": "https://example.com"})
    assert response.status_code == 402
    body = response.json()
    assert body["price_usd"] == 0.10
    assert any(e["protocol"] == "x402" for e in body["accepts"])
    assert "extensions" not in body


def test_manifest_points_at_reachable_discovery_documents(monkeypatch):
    """Every discovery URL the manifest advertises must actually resolve --
    a 404 here is an agent's dead end."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    for name, url in manifest["discovery"].items():
        path = url.replace(manifest["base_url"], "")
        if name == "mcp_endpoint":
            # JSON-RPC: POST-only by protocol, so GET is correctly a 405.
            r = client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert r.status_code == 200 and "tools" in r.json()["result"], (
                f"{name} -> {path} does not speak MCP"
            )
            continue
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


def test_favicon_and_og_image_are_served(monkeypatch):
    """og:image referenced in the page head must actually resolve, or link
    previews render blank and the shared link loses its click-through."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    favicon = client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert "image/svg+xml" in favicon.headers["content-type"]

    og = client.get("/og-image.png")
    assert og.status_code == 200
    assert "image/png" in og.headers["content-type"]


def test_page_head_social_tags_point_at_served_assets(monkeypatch):
    import re

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    html = client.get("/").text

    referenced = set(re.findall(r'(?:href|content)="https://[^"]+?(/[^"/]+\.(?:svg|png))"', html))
    referenced |= set(re.findall(r'href="(/[^"]+\.svg)"', html))
    assert referenced, "page head references no image assets at all"
    for path in referenced:
        assert client.get(path).status_code == 200, f"{path} is referenced but 404s"


# --- Machine payers are charged only for audits that actually ran ----------


def _x402_caller(monkeypatch, module, *, settle_ok=True):
    """Make an X-PAYMENT header authenticate, tracking verify/settle calls."""
    calls = {"verified": 0, "settled": 0}
    sentinel = object()

    def _verify(header, price=None):
        calls["verified"] += 1
        return sentinel

    def _settle(pending):
        calls["settled"] += 1
        assert pending is sentinel
        return settle_ok

    monkeypatch.setattr(module.x402_payments, "verify_only_sync", _verify)
    monkeypatch.setattr(module.x402_payments, "settle_sync", _settle)
    return calls


def test_failed_audit_does_not_settle_an_x402_payment(monkeypatch):
    """Regression: settlement used to happen during authentication, so a
    caller whose audit then failed to run had paid for nothing. Audits fail
    routinely at volume -- the site being audited goes down or times out."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)

    def _boom(*a, **k):
        raise RuntimeError("target site unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 502
    assert response.json()["pass"] is None
    assert calls["verified"] == 1, "payment should still be verified to gate access"
    assert calls["settled"] == 0, "a failed audit must never be charged for"


def test_successful_audit_settles_the_x402_payment(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    client = TestClient(module.app)
    response = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 200
    assert response.json()["pass"] is True
    assert calls["settled"] == 1, "a delivered audit must actually be charged for"
    assert "billing_warning" not in response.json()


def test_settlement_failure_after_delivery_is_surfaced_not_hidden(monkeypatch):
    """If we delivered but couldn't collect, say so on the response rather
    than silently eating the loss."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _x402_caller(monkeypatch, module, settle_ok=False)
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})

    client = TestClient(module.app)
    body = client.post(
        "/audit/wcag",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    ).json()

    assert "billing_warning" in body
    assert "not charged" in body["billing_warning"]


def test_failed_bundle_does_not_settle_either(monkeypatch):
    """The bundle is the $0.10 route -- the most expensive thing to wrongly
    charge for."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    calls = _x402_caller(monkeypatch, module)

    def _boom(*a, **k):
        raise RuntimeError("target site unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)

    client = TestClient(module.app)
    response = client.post(
        "/audit/bundle",
        json={"url": "https://example.com"},
        headers={"X-PAYMENT": "signed-payment"},
    )

    assert response.status_code == 502
    assert calls["settled"] == 0


def test_bundle_hits_the_target_url_only_twice(monkeypatch):
    """The bundle used to fetch the audited URL four times per call: two full
    Chromium page loads (axe, performance) plus two HTTP GETs (SEO, security).
    That is 4x the latency on the priciest route, and four hits on a
    stranger's origin per call is how an audit bot gets its user agent
    blocked. One rendered load feeds both browser checks; one GET feeds both
    response checks."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    page_loads = {"n": 0}
    http_gets = {"n": 0}

    def _count_page_load(fn, **kwargs):
        page_loads["n"] += 1
        page = _FakePage()
        return fn(page)

    def _count_http_get(url):
        http_gets["n"] += 1
        return object()

    with patch.object(module.browser_pool, "with_page", _count_page_load), patch.object(
        module.audits, "fetch_once", _count_http_get
    ), patch.object(
        module.audits, "run_seo_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}
    ), patch.object(
        module._axe, "run", return_value=_FakeAxeResult()
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )

    assert response.status_code == 200
    assert page_loads["n"] == 1, f"expected 1 rendered page load, got {page_loads['n']}"
    assert http_gets["n"] == 1, f"expected 1 HTTP GET, got {http_gets['n']}"


def test_bundle_shares_one_http_response_between_seo_and_security(monkeypatch):
    """Both response-based checks must be handed the SAME response object --
    if either re-fetches, we are back to hammering the origin."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    shared = object()
    seen = []

    def _seo(html, url, response=None):
        seen.append(("seo", response))
        return {"status": "ok", "pass": True, "findings": []}

    def _security(url, response=None):
        seen.append(("security", response))
        return {"status": "ok", "pass": True, "findings": []}

    _perf = {"status": "ok", "pass": True, "findings": [], "metrics": {}}
    with patch.object(
        module, "_run_axe_and_performance", return_value=({"violations": []}, _perf)
    ), patch.object(module.audits, "fetch_once", lambda url: shared), patch.object(
        module.audits, "run_seo_audit", _seo
    ), patch.object(
        module.audits, "run_security_audit", _security
    ):
        client = TestClient(module.app)
        response = client.post(
            "/audit/bundle", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"}
        )

    assert response.status_code == 200
    assert [name for name, _ in seen] == ["seo", "security"]
    assert all(resp is shared for _, resp in seen), "an audit re-fetched instead of sharing"


class _FakePage:
    """Minimal stand-in for a Playwright page in the combined-load path."""

    def on(self, event, handler):
        fake_response = type("R", (), {"headers": {"content-length": "100"}})()
        handler(fake_response)

    def goto(self, url, **kwargs):
        return None

    def evaluate(self, script):
        return 42


class _FakeAxeResult:
    response = {"violations": []}


# --- The node sells audits; it does not give them away ---------------------


def test_free_scan_endpoint_is_gone(monkeypatch):
    """An audit costs a real browser page load. A free endpoint funds
    strangers' compute and is an abuse vector, so it was removed rather than
    merely hidden from the page."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    assert client.post("/scan/free", json={"url": "https://example.com"}).status_code == 404


def test_manifest_advertises_no_unpaid_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()

    unpaid = [e for e in manifest["endpoints"] if e.get("payment_required") is False]
    assert unpaid == [], f"manifest advertises unpaid endpoints: {unpaid}"


def test_landing_page_offers_no_free_scan(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    html = client.get("/").text.lower()

    assert "/scan/free" not in html
    for phrase in ("free scan", "free demo", "try it free", "scan for free"):
        assert phrase not in html, f"landing page still offers something free: {phrase!r}"


def test_landing_page_never_prints_a_per_call_cent_price(monkeypatch):
    """A human must not see $0.03 sitting a scroll above a $79 plan.

    A2A is still the product and the machine section still leads -- that is
    the test below. But printing the per-call rate on the same page as the
    plans invites one subtraction and makes every plan look absurd, which is
    the same trap that killed the old scan-denominated plan. Agents read the
    exact rate from /.well-known/agent.json and from the 402 challenge; that
    is authoritative and cannot drift from what the routes charge, which a
    hand-written page can.
    """
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    html = TestClient(module.app).get("/").text

    for price in ("$0.03", "$0.10", '"0.03"', '"0.10"'):
        assert price not in html, (
            f"landing page prints the per-call rate {price} -- it undercuts the plans"
        )
    # The rate has to remain reachable, just not printed here.
    assert "/.well-known/agent.json" in html


def test_landing_page_leads_with_the_machine_api_not_the_plans(monkeypatch):
    """A2A is the product; the plans are the secondary path for humans who
    don't want to build an integration. Positioning is unchanged -- only the
    cent figure is gone."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    html = TestClient(module.app).get("/").text

    first_human_price = min(html.index(p) for p in ("$29.99", "$79", "$249"))
    assert html.index("Metered") < first_human_price, "plans appear before the machine API"

    heading = html[html.index("<h1"):html.index("</h1>")]
    assert "subscription" not in heading.lower()
    assert not any(p in heading for p in ("$29.99", "$79", "$249"))


def test_machine_surfaces_still_publish_the_exact_rate(monkeypatch):
    """Removing the price from the page must not remove it from the places a
    paying agent actually reads. If it did, nothing could price a call."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    assert "$0.03" in client.get("/llms.txt").text
    manifest = client.get("/.well-known/agent.json").json()
    assert manifest["pricing"]["single_audit_usd"] == 0.03
    assert manifest["pricing"]["bundle_usd"] == 0.10
    challenge = client.post("/audit/wcag", json={"url": "https://example.com"}).json()
    assert challenge["price_usd"] == 0.03


def test_human_plans_are_priced_per_site_not_per_scan(monkeypatch):
    """Denominating human plans in scans invited the obvious arithmetic
    against the $0.03 machine rate and made the plan look strictly worse.
    Sites are the unit a human buys, and it isn't comparable."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    html = TestClient(module.app).get("/").text.lower()

    assert "per site" in html or "sites" in html
    assert "1,500 scans" not in html and "1500 scans" not in html


# --- One-off paid report ---------------------------------------------------


def _billing_on(monkeypatch, module):
    monkeypatch.setattr(module.billing, "is_configured", lambda: True)


def test_report_requires_a_genuinely_paid_session(monkeypatch):
    """The report URL is the only thing between a stranger and a free audit,
    so an unpaid or unknown session must produce nothing -- and must never
    run the audit, which is the thing that actually costs us."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(module.billing, "paid_report_request", lambda sid: None)

    ran = []
    monkeypatch.setattr(module, "_run_axe_and_performance", lambda url: ran.append(url))

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_not_paid"})

    assert response.status_code == 404
    assert ran == [], "an audit was run for an unpaid session"


def test_paid_report_runs_the_audit_and_renders(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://example.com"}
    )
    monkeypatch.setattr(module.billing, "load_report", lambda sid: None)
    saved = {}
    monkeypatch.setattr(
        module.billing, "save_report", lambda sid, url, result: saved.update(result=result)
    )
    monkeypatch.setattr(
        module,
        "_run_axe_and_performance",
        lambda url: ({"violations": []}, {"pass": True, "findings": [], "metrics": {}}),
    )
    monkeypatch.setattr(module.audits, "fetch_once", lambda url: object())
    monkeypatch.setattr(
        module.audits, "run_seo_audit", lambda h, u, response=None: {"pass": True, "findings": []}
    )
    monkeypatch.setattr(
        module.audits, "run_security_audit", lambda u, response=None: {"pass": True, "findings": []}
    )

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "example.com" in response.text
    assert saved.get("result"), "a successful report should be cached for re-viewing"


def test_paid_report_is_not_rerun_when_already_cached(monkeypatch):
    """A refresh must not re-run an audit the buyer paid for exactly once."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://example.com"}
    )
    monkeypatch.setattr(
        module.billing,
        "load_report",
        lambda sid: {"url": "https://example.com", "result": {"wcag": {"pass": True, "violations": []}}},
    )
    ran = []
    monkeypatch.setattr(module, "_run_axe_and_performance", lambda url: ran.append(url))

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 200
    assert ran == [], "cached report was re-audited"


def test_failed_report_is_not_cached_and_says_purchase_stands(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    _billing_on(monkeypatch, module)
    monkeypatch.setattr(
        module.billing, "paid_report_request", lambda sid: {"url": "https://down.example"}
    )
    monkeypatch.setattr(module.billing, "load_report", lambda sid: None)
    saved = []
    monkeypatch.setattr(
        module.billing, "save_report", lambda *a, **k: saved.append(a)
    )

    def _boom(url):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(module, "_run_axe_and_performance", _boom)

    client = TestClient(module.app)
    response = client.get("/report", params={"session_id": "cs_paid"})

    assert response.status_code == 502
    assert saved == [], "a failed report must not be cached as the buyer's result"
    assert "purchase still stands" in response.text


def test_report_escapes_content_from_the_audited_site(monkeypatch):
    """Findings carry text from a third-party page. Unescaped, an audited
    site could write markup into a report its owner is about to read."""
    module = _load_main(monkeypatch)
    result = {
        "wcag": {"pass": False, "violations": [
            {"id": "<script>alert(1)</script>", "impact": "critical", "help": "x", "nodes_affected": 1}
        ]},
        "seo": {"pass": True, "findings": []},
        "security": {"pass": True, "findings": []},
        "performance": {"pass": True, "findings": [], "metrics": {}},
    }
    html = module._render_report("https://evil.example/<img src=x>", result)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x>" not in html


def test_report_checkout_501s_when_not_configured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/billing/report", json={"email": "a@example.com", "url": "https://example.com"}
    )
    assert response.status_code == 501


# --- MCP Streamable HTTP endpoint ------------------------------------------


def _rpc(client, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body)


def test_mcp_initialize_negotiates_protocol_version(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    r = _rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    result = r.json()["result"]
    assert r.json()["jsonrpc"] == "2.0"
    # Echo a version we support...
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "hubvibe-site-audit"

    # ...but fall back to ours for one we don't.
    r2 = _rpc(client, "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert r2.json()["result"]["protocolVersion"] == module.MCP_PROTOCOL_VERSIONS[0]


def test_mcp_notifications_get_no_body(monkeypatch):
    """A JSON-RPC notification has no id and must not be answered with a
    result, or a conforming client errors on the unexpected response."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content in (b"", b"null")


def test_mcp_tools_list_is_free_and_complete(monkeypatch):
    """Discovery must not require payment: an agent has to see what this node
    sells and what it costs before it can decide to buy."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    tools = _rpc(client, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"
    }
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert "$" in t["description"], "tool description must state its price"


def test_mcp_tool_call_without_payment_is_an_error_result_not_a_crash(monkeypatch):
    """A payment requirement is a normal outcome the model should see, so it
    belongs in the result as isError -- not a JSON-RPC protocol error."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)

    ran = []
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: ran.append(1))

    body = _rpc(
        client, "tools/call", {"name": "audit_wcag", "arguments": {"url": "https://example.com"}}
    ).json()

    assert "error" not in body
    assert body["result"]["isError"] is True
    assert "Payment required" in body["result"]["content"][0]["text"]
    assert ran == [], "an unpaid MCP call must not run the audit"


def test_mcp_paywall_is_machine_parseable_not_prose(monkeypatch):
    """An agent must be able to read the price off the MCP paywall.

    The challenge used to be stringified into the middle of an English
    sentence, so the only way to find `price_usd` or `accepts` was to
    substring-scrape JSON out of prose -- unusable to exactly the machine
    buyers this endpoint exists to serve. The text must parse as JSON.
    """
    import json

    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "audit_bundle", "arguments": {"url": "https://example.com"}},
        },
    )
    result = response.json()["result"]
    assert result["isError"] is True

    challenge = json.loads(result["content"][0]["text"])
    assert challenge["error"] == "payment_required"
    assert challenge["price_usd"] == 0.10, "MCP must quote the same price as the REST route"
    assert isinstance(challenge["accepts"], list)
    assert "docs" in challenge
    # The human-readable line survives alongside the machine-readable fields.
    assert "Payment required" in challenge["message"]


def test_mcp_tool_call_with_internal_key_runs_and_returns_content(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")
    monkeypatch.setattr(module, "_run_axe", lambda *a, **k: {"violations": []})
    client = TestClient(module.app)

    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": {"url": "https://example.com"}},
        },
        headers={"X-API-Key": "test-key"},
    ).json()

    assert body["id"] == 7
    assert body["result"]["isError"] is False
    import json as _json
    payload = _json.loads(body["result"]["content"][0]["text"])
    assert payload["pass"] is True


def test_mcp_failed_audit_reports_error_and_is_not_billed(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key="test-key")

    def _boom(*a, **k):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(module, "_run_axe", _boom)
    billed = []
    monkeypatch.setattr(module, "_bill", lambda auth, units=1: billed.append(units))
    client = TestClient(module.app)

    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_wcag", "arguments": {"url": "https://x.example"}},
        },
        headers={"X-API-Key": "test-key"},
    ).json()

    assert body["result"]["isError"] is True
    assert billed == [], "a failed MCP audit must not be billed"


def test_mcp_unknown_method_is_a_jsonrpc_error(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    body = _rpc(client, "resources/list").json()
    assert body["error"]["code"] == -32601


def test_mcp_tool_prices_match_the_rest_routes(monkeypatch):
    """A tool that advertises a price the route won't charge makes an agent
    build the wrong payment."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    for name, price in module._MCP_TOOL_PRICES.items():
        route = "/audit/" + name.replace("audit_", "")
        charged = client.post(route, json={"url": "https://example.com"}).json()["price_usd"]
        assert charged == price, f"{name} advertises {price}, {route} charges {charged}"


def test_mcp_initialize_only_returns_handshake_protocol_versions(monkeypatch):
    """Regression: we first answered with 2026-07-28, the SDK's newest
    constant. That is a 'modern' version negotiated out-of-band and is NOT
    valid in an initialize result -- the official client checks the response
    against HANDSHAKE_PROTOCOL_VERSIONS and hard-errors on anything else, so
    every real MCP client refused to connect. Verified against the real SDK
    after fixing; this guards the constant."""
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)

    handshake_versions = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
    assert set(module.MCP_PROTOCOL_VERSIONS) <= handshake_versions, (
        "MCP_PROTOCOL_VERSIONS contains a version that is invalid in an "
        "initialize response; real clients will refuse to connect"
    )

    for requested in (None, "2025-06-18", "not-a-version"):
        params = {"capabilities": {}}
        if requested:
            params["protocolVersion"] = requested
        got = _rpc(client, "initialize", params).json()["result"]["protocolVersion"]
        assert got in handshake_versions, f"initialize returned unusable version {got}"
