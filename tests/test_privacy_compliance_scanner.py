import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_PATH = REPO_ROOT / "privacy-compliance-scanner" / "app" / "main.py"


def _load_main(monkeypatch, api_key="test-key"):
    if api_key is not None:
        monkeypatch.setenv("SCANNER_API_KEY", api_key)
    else:
        monkeypatch.delenv("SCANNER_API_KEY", raising=False)
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "PRIVACY_STRIPE_METERED_PRICE_ID"):
        monkeypatch.delenv(var, raising=False)

    spec = importlib.util.spec_from_file_location("privacy_scanner_main", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_check(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "privacy-compliance-scanner"}


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


def test_scan_requires_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/scan", json={"url": "https://example.com"})
    assert response.status_code == 401


def test_scan_rejects_wrong_api_key(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch)
    client = TestClient(module.app)
    response = client.post("/scan", json={"url": "https://example.com"}, headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_scan_rejects_unbilled_key_when_billing_unconfigured(monkeypatch):
    from fastapi.testclient import TestClient

    module = _load_main(monkeypatch, api_key=None)
    client = TestClient(module.app)
    response = client.post(
        "/scan", json={"url": "https://example.com"}, headers={"X-API-Key": "anything"}
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


def _load_scanner():
    path = REPO_ROOT / "privacy-compliance-scanner" / "app" / "scanner.py"
    spec = importlib.util.spec_from_file_location("privacy_scanner_scanner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classify_cookie_matches_known_exact_and_prefix_names():
    scanner = _load_scanner()
    assert scanner._classify_cookie("_fbp") == ("Meta/Facebook Pixel", "advertising")
    assert scanner._classify_cookie("_ga_G-ABC123") == ("Google Analytics 4", "analytics")
    assert scanner._classify_cookie("not_a_known_cookie") is None


def test_scan_page_detects_pre_consent_trackers_and_missing_links(monkeypatch):
    scanner = _load_scanner()

    class FakeContext:
        def cookies(self):
            return [{"name": "_ga"}, {"name": "session_id"}]

        def new_page(self):
            return FakePage()

    class FakePage:
        def goto(self, *a, **k):
            pass

        def eval_on_selector_all(self, selector, script):
            if selector == "script[src]":
                return []
            return [{"href": "/terms", "text": "Terms"}]

    class FakeBrowser:
        def new_context(self):
            return FakeContext()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(scanner, "sync_playwright", lambda: FakePlaywright())

    result = scanner.scan_page("https://example.com")
    assert result["pre_consent_trackers"] == [
        {"cookie": "_ga", "vendor": "Google Analytics", "category": "analytics"}
    ]
    assert result["detected_cmps"] == []
    assert result["has_privacy_link"] is False
    assert result["has_cookie_policy_link"] is False
    finding_ids = {f["id"] for f in result["findings"]}
    assert "trackers-before-consent" in finding_ids
    assert "no-privacy-policy-link" in finding_ids
