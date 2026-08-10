import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDITS_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "audits.py"

spec = importlib.util.spec_from_file_location("audits", AUDITS_PATH)
audits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audits)


# --- SEO audit -----------------------------------------------------------

GOOD_HTML = """<html lang="en"><head>
<title>A reasonably short title</title>
<meta name="description" content="A reasonably short description">
<link rel="canonical" href="https://example.com">
<meta property="og:title" content="A"><meta property="og:description" content="B">
<meta property="og:image" content="C"><meta property="og:type" content="website">
<script type="application/ld+json">{}</script>
</head><body><h1>Hello</h1></body></html>"""


def test_seo_audit_passes_on_well_formed_page():
    result = audits.run_seo_audit(GOOD_HTML, None)
    assert result["status"] == "ok"
    assert result["pass"] is True
    assert result["findings"] == []


def test_seo_audit_flags_missing_title_and_description():
    html = "<html><body><h1>Hi</h1></body></html>"
    result = audits.run_seo_audit(html, None)
    ids = {f["id"] for f in result["findings"]}
    assert "missing-title" in ids
    assert "missing-meta-description" in ids
    assert "missing-h1" not in ids
    assert result["pass"] is False


def test_seo_audit_flags_missing_and_multiple_h1():
    no_h1 = "<html><head><title>T</title><meta name=\"description\" content=\"d\"></head><body></body></html>"
    result = audits.run_seo_audit(no_h1, None)
    assert any(f["id"] == "missing-h1" for f in result["findings"])
    assert result["pass"] is False

    two_h1 = (
        "<html><head><title>T</title><meta name=\"description\" content=\"d\"></head>"
        "<body><h1>One</h1><h1>Two</h1></body></html>"
    )
    result = audits.run_seo_audit(two_h1, None)
    assert any(f["id"] == "multiple-h1" for f in result["findings"])


def test_seo_audit_requires_html_or_url():
    with pytest.raises(ValueError):
        audits.run_seo_audit(None, None)


def test_seo_audit_fetches_url_when_no_html_given():
    fake_resp = MagicMock()
    fake_resp.text = GOOD_HTML
    fake_resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        result = audits.run_seo_audit(None, "https://example.com")
    mock_get.assert_called_once()
    assert result["pass"] is True


# --- Security audit --------------------------------------------------------


def _fake_security_response(url="https://example.com/", headers=None):
    resp = MagicMock()
    resp.url = url
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


def test_security_audit_passes_with_all_headers_present():
    headers = {
        "strict-transport-security": "max-age=31536000",
        "content-security-policy": "default-src 'self'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    with patch("httpx.get", return_value=_fake_security_response(headers=headers)):
        result = audits.run_security_audit("https://example.com")
    assert result["pass"] is True
    assert result["findings"] == []


def test_security_audit_flags_non_https_as_critical():
    with patch("httpx.get", return_value=_fake_security_response(url="http://example.com/")):
        result = audits.run_security_audit("http://example.com")
    ids = {f["id"] for f in result["findings"]}
    assert "no-https" in ids
    assert result["pass"] is False


def test_security_audit_flags_wildcard_cors():
    headers = {"access-control-allow-origin": "*"}
    with patch("httpx.get", return_value=_fake_security_response(headers=headers)):
        result = audits.run_security_audit("https://example.com")
    ids = {f["id"] for f in result["findings"]}
    assert "wildcard-cors" in ids


def test_security_audit_requires_url():
    with pytest.raises(ValueError):
        audits.run_security_audit(None)


# --- Performance audit -----------------------------------------------------


def test_performance_audit_requires_url():
    with pytest.raises(ValueError):
        audits.run_performance_audit(None)


def test_performance_audit_flags_high_dom_complexity_and_heavy_page():
    fake_page = MagicMock()
    fake_page.evaluate.return_value = 2000  # over the 1500 threshold

    def _capture_response_handler(event_name, handler):
        # Simulate one large response so total bytes crosses the 3MB threshold.
        fake_response = MagicMock()
        fake_response.headers = {"content-length": "4000000"}
        handler(fake_response)

    fake_page.on.side_effect = _capture_response_handler

    # The audit now runs on a pooled browser (see app/browser_pool.py) rather
    # than launching its own, so stand in for the pool's page handout.
    def _fake_with_page(fn, **context_kwargs):
        return fn(fake_page)

    with patch.object(audits.browser_pool, "with_page", _fake_with_page):
        result = audits.run_performance_audit("https://example.com")

    ids = {f["id"] for f in result["findings"]}
    assert "high-dom-complexity" in ids
    assert "heavy-page-weight" in ids
    assert result["pass"] is False
    assert result["metrics"]["dom_node_count"] == 2000
    assert result["metrics"]["total_bytes_transferred"] == 4000000


def test_performance_audit_gets_an_isolated_context_not_a_shared_cache():
    """Reusing a warmed cache across audits would under-report transferred
    bytes and request count, quietly scoring a heavy page as light."""
    fake_page = MagicMock()
    fake_page.evaluate.return_value = 10
    fake_page.on.side_effect = lambda event_name, handler: None

    seen = {}

    def _fake_with_page(fn, **context_kwargs):
        seen.update(context_kwargs)
        return fn(fake_page)

    with patch.object(audits.browser_pool, "with_page", _fake_with_page):
        audits.run_performance_audit("https://example.com")

    # A per-call context is what provides isolation; assert we asked for one
    # with our own user agent rather than reusing a default shared page.
    assert seen.get("user_agent") == audits._USER_AGENT
