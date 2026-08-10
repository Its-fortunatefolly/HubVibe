"""Real, rule-based checks for the SEO, security, and performance audit
endpoints -- same philosophy as the WCAG/axe-core audit in app/main.py:
deterministic, verifiable signals only. Nothing here is an LLM guessing at
quality, and nothing here is ever coerced into a false pass on error; a
check that couldn't run raises, and the caller (main.py) turns that into
an honest 502, never a fabricated result.

Each function returns `{"status": "ok", "pass": bool, ..., "findings": [...]}`
where every finding is `{"id": str, "severity": str, "detail": str}`,
matching the shape used across every other product in this repo
(privacy-compliance-scanner's `findings` included).

These are real, narrow, disclosed signals -- not a replacement for a full
SEO audit, a penetration test, or a Lighthouse run. Each function's
docstring says exactly what it does and does not check.
"""

from html.parser import HTMLParser
from typing import Optional

import httpx

try:
    from . import browser_pool
except ImportError:
    # Loaded by file path rather than as part of the `app` package (see the
    # matching fallback in main.py). Register under the same canonical name
    # main.py uses and reuse anything already loaded, so both entry points
    # share ONE browser_pool -- two copies would mean two thread-local
    # browsers per worker thread, doubling memory for no benefit.
    import importlib.util
    import sys
    from pathlib import Path as _Path

    _POOL_NAME = "wcag_audit_engine_browser_pool"
    browser_pool = sys.modules.get(_POOL_NAME)
    if browser_pool is None:
        _spec = importlib.util.spec_from_file_location(
            _POOL_NAME, _Path(__file__).resolve().parent / "browser_pool.py"
        )
        browser_pool = importlib.util.module_from_spec(_spec)
        sys.modules[_POOL_NAME] = browser_pool
        _spec.loader.exec_module(browser_pool)

_USER_AGENT = "HubVibeAuditBot/1.0 (+https://hubvibe.dev)"
_HTTP_TIMEOUT = 15.0

_SEVERITY_RANK = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


def _sort_findings(findings: list) -> list:
    return sorted(findings, key=lambda f: _SEVERITY_RANK.get(f["severity"], 4))


def _has_blocking_finding(findings: list) -> bool:
    return any(f["severity"] in ("critical", "serious") for f in findings)


class _SEOParser(HTMLParser):
    """Collects only the tags an SEO/social-sharing check cares about --
    not a general-purpose HTML parser."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta: dict = {}
        self.canonical: Optional[str] = None
        self.h1_count = 0
        self.has_json_ld = False
        self.html_lang: Optional[str] = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs.get("name") or attrs.get("property")
            if name and attrs.get("content") is not None:
                self.meta[name.lower()] = attrs["content"]
        elif tag == "link" and (attrs.get("rel") or "").lower() == "canonical":
            self.canonical = attrs.get("href")
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self.has_json_ld = True
        elif tag == "html" and attrs.get("lang"):
            self.html_lang = attrs["lang"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def run_seo_audit(html: Optional[str], url: Optional[str]) -> dict:
    """Checks title, meta description, H1 structure, canonical link,
    OpenGraph tags, JSON-LD structured data, and the <html lang> attribute.

    Deliberately fetches raw HTML (no JS execution) rather than a
    Playwright-rendered DOM: that's what search-engine and social-card
    crawlers actually see, so it's the more representative signal for SEO
    specifically -- unlike WCAG/performance, which need the rendered page.
    """
    if not html and not url:
        raise ValueError("Provide 'html' or 'url'")
    if not html:
        resp = httpx.get(
            url, timeout=_HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
        resp.raise_for_status()
        html = resp.text

    parser = _SEOParser()
    parser.feed(html)

    findings = []
    title = parser.title.strip()
    if not title:
        findings.append({"id": "missing-title", "severity": "critical", "detail": "No <title> tag found"})
    elif len(title) > 60:
        findings.append(
            {
                "id": "title-too-long",
                "severity": "moderate",
                "detail": f"Title is {len(title)} characters (recommended <= 60)",
            }
        )

    description = parser.meta.get("description")
    if not description:
        findings.append(
            {"id": "missing-meta-description", "severity": "critical", "detail": "No meta description found"}
        )
    elif len(description) > 160:
        findings.append(
            {
                "id": "meta-description-too-long",
                "severity": "moderate",
                "detail": f"Meta description is {len(description)} characters (recommended <= 160)",
            }
        )

    if parser.h1_count == 0:
        findings.append({"id": "missing-h1", "severity": "serious", "detail": "No <h1> tag found"})
    elif parser.h1_count > 1:
        findings.append(
            {
                "id": "multiple-h1",
                "severity": "moderate",
                "detail": f"{parser.h1_count} <h1> tags found (expected exactly 1)",
            }
        )

    if not parser.canonical:
        findings.append(
            {"id": "missing-canonical", "severity": "minor", "detail": "No canonical <link> tag found"}
        )

    missing_og = [tag for tag in ("og:title", "og:description", "og:image", "og:type") if tag not in parser.meta]
    if missing_og:
        findings.append(
            {
                "id": "incomplete-opengraph",
                "severity": "moderate",
                "detail": f"Missing OpenGraph tags: {', '.join(missing_og)}",
            }
        )

    if not parser.has_json_ld:
        findings.append(
            {"id": "missing-structured-data", "severity": "minor", "detail": "No JSON-LD structured data found"}
        )

    if not parser.html_lang:
        findings.append(
            {"id": "missing-lang-attribute", "severity": "minor", "detail": "No lang attribute on <html>"}
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings),
        "checks": "title, meta-description, h1-structure, canonical, opengraph, structured-data, lang",
        "findings": findings,
    }


def run_security_audit(url: Optional[str]) -> dict:
    """Checks the final response's transport (HTTPS) and a handful of
    security-relevant response headers: HSTS, CSP, X-Content-Type-Options,
    clickjacking protection, Referrer-Policy, and CORS.

    This is a real HTTP response inspection, not a TLS/cipher-suite scan
    and not a penetration test -- it reports what's observable in a single
    plain request's response headers.
    """
    if not url:
        raise ValueError("Provide 'url'")
    resp = httpx.get(
        url, timeout=_HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
    )
    final_url = str(resp.url)
    headers = {k.lower(): v for k, v in resp.headers.items()}

    findings = []
    if not final_url.startswith("https://"):
        findings.append(
            {"id": "no-https", "severity": "critical", "detail": f"Final URL is not HTTPS: {final_url}"}
        )

    if "strict-transport-security" not in headers:
        findings.append(
            {"id": "missing-hsts", "severity": "serious", "detail": "No Strict-Transport-Security header"}
        )

    if "content-security-policy" not in headers:
        findings.append(
            {"id": "missing-csp", "severity": "moderate", "detail": "No Content-Security-Policy header"}
        )

    if headers.get("x-content-type-options", "").lower() != "nosniff":
        findings.append(
            {
                "id": "missing-x-content-type-options",
                "severity": "moderate",
                "detail": "X-Content-Type-Options: nosniff not set",
            }
        )

    has_frame_protection = "x-frame-options" in headers or "frame-ancestors" in headers.get(
        "content-security-policy", ""
    )
    if not has_frame_protection:
        findings.append(
            {
                "id": "missing-frame-protection",
                "severity": "moderate",
                "detail": "No X-Frame-Options header or CSP frame-ancestors directive",
            }
        )

    if "referrer-policy" not in headers:
        findings.append(
            {"id": "missing-referrer-policy", "severity": "minor", "detail": "No Referrer-Policy header"}
        )

    if headers.get("access-control-allow-origin") == "*":
        findings.append(
            {
                "id": "wildcard-cors",
                "severity": "minor",
                "detail": "Access-Control-Allow-Origin is '*' -- confirm this is intentional",
            }
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings),
        "checks": "https, hsts, csp, x-content-type-options, frame-protection, referrer-policy, cors",
        "findings": findings,
    }


def run_performance_audit(url: Optional[str]) -> dict:
    """Loads the page in a real browser (Playwright/Chromium) and reports
    DOM node count, total transferred bytes (from Content-Length response
    headers), and request count from a single page load.

    Real measured values from one load on this server's network, not a
    full Lighthouse-style audit (no field data, no repeated-run averaging,
    no render-timing metrics).
    """
    if not url:
        raise ValueError("Provide 'url'")

    resource_bytes = 0
    request_count = 0

    def _on_response(response):
        nonlocal resource_bytes, request_count
        request_count += 1
        length = response.headers.get("content-length")
        if length and length.isdigit():
            resource_bytes += int(length)

    def _measure(page) -> int:
        page.on("response", _on_response)
        page.goto(url, wait_until="networkidle", timeout=30000)
        return page.evaluate("document.querySelectorAll('*').length")

    # Pooled browser, fresh isolated context per call -- see browser_pool.
    # Isolation matters for this audit in particular: a shared cache would
    # make transferred-bytes and request-count read low on any URL a previous
    # audit had already warmed, silently reporting a page as lighter than it is.
    dom_node_count = browser_pool.with_page(_measure, user_agent=_USER_AGENT)

    findings = []
    if dom_node_count > 1500:
        findings.append(
            {
                "id": "high-dom-complexity",
                "severity": "moderate",
                "detail": f"{dom_node_count} DOM nodes (recommended <= 1500)",
            }
        )
    if resource_bytes > 3_000_000:
        findings.append(
            {
                "id": "heavy-page-weight",
                "severity": "moderate",
                "detail": f"{resource_bytes / 1_000_000:.1f} MB transferred (recommended <= 3 MB)",
            }
        )
    if request_count > 100:
        findings.append(
            {
                "id": "high-request-count",
                "severity": "minor",
                "detail": f"{request_count} network requests (recommended <= 100)",
            }
        )

    findings = _sort_findings(findings)
    return {
        "status": "ok",
        "pass": not _has_blocking_finding(findings) and len(findings) == 0,
        "metrics": {
            "dom_node_count": dom_node_count,
            "total_bytes_transferred": resource_bytes,
            "request_count": request_count,
        },
        "findings": findings,
        "disclosure": "Single-page-load measurement, not a full Lighthouse-style audit.",
    }
