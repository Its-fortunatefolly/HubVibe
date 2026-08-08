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
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/audit", json={"url": "https://example.com"})
    assert response.status_code == 401


def test_audit_rejects_wrong_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post(
        "/audit", json={"url": "https://example.com"}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


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
    assert response.status_code == 401


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
