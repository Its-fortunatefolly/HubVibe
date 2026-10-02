"""/terms and /privacy: served, linked, and still true.

Routers and directories read both before listing a provider, and both
answered 404 until 2026-10-02. The privacy page states retention periods;
those numbers live in code, so each one is checked against its constant --
a page that drifts from what the node actually keeps would be a false
statement to every buyer who reads it.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"
STATIC = APP / "static"
MAIN_PATH = APP / "main.py"


def _drop_sibling_cache():
    for name in [n for n in sys.modules
                 if n.startswith("wcag_audit_engine_") and n != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _isolated():
    _drop_sibling_cache()
    yield
    _drop_sibling_cache()


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    spec = importlib.util.spec_from_file_location("wcag_audit_main_legal_pages", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client(module):
    from fastapi.testclient import TestClient

    return TestClient(module.app)


@pytest.mark.parametrize("path, title", [("/terms", "Terms of Service"), ("/privacy", "Privacy")])
def test_the_pages_are_served(app_module, path, title):
    response = _client(app_module).get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert f"<h1>{title}</h1>" in response.text
    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_the_site_links_both_pages():
    index = (STATIC / "index.html").read_text()
    assert 'href="/terms"' in index and 'href="/privacy"' in index
    sitemap = (STATIC / "sitemap.xml").read_text()
    assert "https://hubvibe-io.com/terms" in sitemap and "https://hubvibe-io.com/privacy" in sitemap


def test_openapi_points_at_the_terms(app_module):
    info = _client(app_module).get("/openapi.json").json()["info"]
    assert info["termsOfService"] == f"{app_module.PUBLIC_BASE_URL}/terms"


def test_the_pages_name_only_the_business():
    for name in ("terms.html", "privacy.html"):
        text = (STATIC / name).read_text()
        assert "hubvibe@hubvibe-io.com" in text
        for personal in ("gmail.com", "Amanda", "fortunatefool"):
            assert personal not in text, f"{name} names a person"


def test_no_page_runs_analytics():
    for name in ("index.html", "terms.html", "privacy.html"):
        text = (STATIC / name).read_text().lower()
        for tracker in ("gtag(", "googletagmanager", "plausible", "segment.com", "hotjar", "clarity.ms"):
            assert tracker not in text, f"{name} loads {tracker}"


def test_the_terms_promise_only_what_the_node_does():
    text = (STATIC / "terms.html").read_text()
    assert "Only when a result is delivered" in text
    assert "/work/receipts/{id}" in text
    # Decided 2026-10-02: no redelivery or refund promise for delivered results.
    assert "refund" not in text.lower()


def test_the_privacy_page_states_the_retention_the_code_has(app_module):
    text = (STATIC / "privacy.html").read_text()
    workers = sys.modules["wcag_audit_engine_workers"]
    assert workers.ledger.DEFERRED_KEEP_SECONDS == 48 * 3600
    assert workers.media_store.KEEP_SECONDS == 48 * 3600
    assert "48 hours" in text
    a2a = sys.modules.get("wcag_audit_engine_a2a") or app_module.a2a
    assert a2a.TaskStore()._ttl == 15 * 60 and "15 minutes" in text
    book = app_module.purchase_book
    assert book._DEFAULT_REQUEST_MAX_BYTES == 16 * 1024 and "up to 16 KB" in text
    assert "accounting and tax record" in text
