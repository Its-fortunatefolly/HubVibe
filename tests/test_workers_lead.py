"""lead.qualify -- a business website qualified as a sales lead in one call.

Pinned: a clean site scores low and matches its published contract; a site
with no HTTPS and images without alt text scores on exactly the published
rule, with the hooks an outreach agent sends; a site that cannot be fetched
(unreachable, 5xx, 4xx, not HTML) fails the job unbilled; a supplementary
read that fails or runs out of its budget (DNS, company) becomes a note and
the job still ships; the score rule is deterministic and capped; platform
and tag detection reads the page only; the static accessibility parser;
input refused before the gate; the worker never touches the browser; paid
HTTP + MCP; the static manifests.
"""

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "lead.qualify", "/work/lead/qualify", "hubvibe_lead_qualify"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
LEAD = W.skills.lead

CLEAN_HTML = """<!doctype html><html lang="en"><head><title>Joe's Plumbing</title>
<meta name="description" content="Plumbing in Austin since 1998.">
<link rel="canonical" href="https://joesplumbing.example/">
<meta property="og:title" content="Joe's Plumbing"><meta property="og:description" content="Plumbing">
<meta property="og:image" content="https://joesplumbing.example/og.png"><meta property="og:type" content="website">
<script type="application/ld+json">{"@type": "LocalBusiness", "name": "Joe's Plumbing"}</script>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123"></script>
<script>fbq('init', '123');</script><script src="https://widget.intercom.io/widget/abc"></script>
</head><body><h1>Joe's Plumbing</h1><img src="van.jpg" alt="Our van"><img src="spacer.gif" alt="">
<form><label for="e">Email</label><input id="e" type="email"><input type="submit" value="Go"></form>
<a href="/contact">Contact us</a><a href="/home"><img src="logo.png" alt="Joe's Plumbing"></a>
</body></html>"""
BARE_HTML = """<html><head><title>Joe's Plumbing</title></head><body><h1>Hi</h1><h1>Two</h1>
<img src="a.png"><img src="b.png"><img src="c.png"><img src="d.png" alt="">
<form><input type="text" name="q"><input type="hidden" name="h"><textarea></textarea></form>
<a href="/x"></a><a href="/y">Contact</a><a href="/z"><svg aria-label="cart"></svg></a>
<link href="/wp-content/themes/x.css"><script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABCD12"></script>
</body></html>"""
SECURE_HEADERS = {"content-type": "text/html; charset=utf-8", "server": "nginx",
                  "strict-transport-security": "max-age=31536000", "content-security-policy": "default-src 'self'",
                  "x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "no-referrer"}
DNS_FULL = {"domain": "joesplumbing.example", "domain_ascii": "joesplumbing.example", "exists": True,
            "records": {"a": ["192.0.2.1"], "aaaa": [], "cname": [], "mx": ["1 aspmx.l.google.com", "5 alt1.aspmx.l.google.com"],
                        "ns": [], "txt": ["v=spf1 include:_spf.google.com ~all"], "caa": [], "soa": []},
            "email_security": {"spf": {"record": "v=spf1 include:_spf.google.com ~all", "all": "softfail", "multiple": False},
                               "dmarc": {"record": "v=DMARC1; p=reject", "policy": "reject", "subdomain_policy": None,
                                         "percent": None, "reports_to": None}},
            "tls": None, "notes": [], "checked_at": "2026-10-03T00:00:00Z"}
DNS_BARE = dict(DNS_FULL, records=dict(DNS_FULL["records"], mx=["10 mail.joesplumbing.example"], txt=[]),
                email_security={"spf": None, "dmarc": None})
ENRICHED = {"query": {"domain": "joesplumbing.example", "name": None},
            "company": {"name": "Joe's Plumbing", "legal_name": "JOE'S PLUMBING LLC", "description": "Plumber", "website": "https://joesplumbing.example/",
                        "domain": "joesplumbing.example", "founded": "1998", "employees": {"count": 12, "as_of": None, "source": "website"},
                        "industries": ["plumbing"], "headquarters": {"city": "Austin", "country": "United States", "country_code": "US", "address": None},
                        "parent": None, "ceo": None, "founders": [], "logo_url": None, "status": None, "legal_form": None, "jurisdiction": None},
            "identifiers": {"wikidata": None, "lei": None, "cik": None, "registration_number": None, "registration_authority": None,
                            "listings": [], "sec_tickers": [], "sic": None, "sic_description": None, "state_of_incorporation": None},
            "socials": {"x": None, "linkedin": None, "facebook": "https://www.facebook.com/joesplumbing", "instagram": None, "github": None, "youtube": None},
            "sources": ["Company website (self-declared)"], "field_sources": {"name": "website"}, "notes": [], "checked_at": "2026-10-03T00:00:00Z"}


def _page(url="https://joesplumbing.example/", status=200, html=CLEAN_HTML, headers=None, content_type="text/html"):
    return {"url": url, "final_url": url, "status": status, "content_type": content_type,
            "bytes": len(html or ""), "text": html, "truncated": False,
            "headers": headers if headers is not None else SECURE_HEADERS}


class _Ctx:
    """The one fetch step runs against the page given (or raises `error`);
    the supplementary reads are stubbed at the skill level."""

    def __init__(self, page=None, error=None):
        self.page, self.error = page or _page(), error
        self.steps, self.attempts, self.providers_used = [], [], []
        self.call_id, self.worker = "call-1", WORKER

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append((step, [p.id for p in providers]))
        outer = self

        class _Fetcher:
            id = providers[0].id

            async def fetch(self, url, max_chars=None):
                if outer.error is not None:
                    raise outer.error
                return W.runtime.ProviderResult(value=outer.page)
        return (await call(_Fetcher())).value


def _stub(monkeypatch, dns=DNS_FULL, company=ENRICHED):
    """Stub the two bees lead.qualify composes; a value is returned, an
    exception is raised, a coroutine function is awaited as given."""
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason", lambda url: None)

    def make(value):
        async def fake(ctx, payload):
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return await value(ctx, payload)
            return value
        return fake
    monkeypatch.setattr(W.skills.osint, "domain_dns", make(dns))
    monkeypatch.setattr(W.skills.company, "enrich", make(company))


def _qualify(body, ctx=None):
    return asyncio.run(LEAD.qualify(ctx or _Ctx(), body))


def _schema():
    return W.catalog.contract.OUTPUT_SCHEMAS[WORKER]


# --- the product ------------------------------------------------------------------------

def test_a_clean_site_scores_low_and_matches_its_contract(monkeypatch):
    _stub(monkeypatch)
    ctx = _Ctx()
    out = _qualify({"url": "https://www.joesplumbing.example/", "business_name": " Joe's Plumbing "}, ctx)
    jsonschema.Draft202012Validator(_schema()).validate(out)
    assert W.catalog.contract.check(_schema(), out) is None
    assert out["domain"] == "joesplumbing.example" and out["business_name"] == "Joe's Plumbing"
    assert out["site"]["https"] and out["site"]["status"] == 200 and out["site"]["title"] == "Joe's Plumbing"
    assert out["seo"]["pass"] and out["seo"]["findings"] == []
    assert out["security"]["pass"] and out["security"]["findings"] == []
    signals = out["accessibility_signals"]
    assert signals["images_total"] == 3 and signals["images_without_alt"] == 0
    assert signals["inputs_total"] == 1 and signals["inputs_without_label"] == 0
    assert signals["links_total"] == 2 and signals["links_without_text"] == 0
    assert signals["html_lang"] == "en" and "/audit/wcag" in signals["scope"]
    assert out["tech"]["analytics"] == ["Google Analytics"] and out["tech"]["ad_pixels"] == ["Meta Pixel"]
    assert out["tech"]["chat_widgets"] == ["Intercom"] and out["tech"]["platform"] is None
    assert out["email_domain"]["mx_present"] and out["email_domain"]["mail_provider"] == "google"
    assert out["email_domain"]["mx"] == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
    assert out["company"]["name"] == "Joe's Plumbing" and out["company"]["socials"]["facebook"]
    assert out["score"] == 0 and out["score_band"] == "low" and out["reasons"] == []
    assert out["sections_failed"] == [] and len(out["sources"]) == 3
    assert out["checked_at"].endswith("Z")
    # One fetch, by the raw HTTP provider: never the browser-first extract list.
    assert ctx.steps == [("fetch", ["http-fetch-raw"])]


def test_no_https_and_missing_alt_text_become_hooks_on_the_published_rule(monkeypatch):
    _stub(monkeypatch, dns=DNS_BARE, company=W.runtime.InvalidRequest(
        "No company was found for this domain or name in Wikidata, the LEI register or on the site itself. "
        "Nothing was charged."))
    page = _page(url="http://joesplumbing.example/", html=BARE_HTML, headers={"content-type": "text/html"})
    out = _qualify({"url": "http://joesplumbing.example/"}, _Ctx(page))
    jsonschema.Draft202012Validator(_schema()).validate(out)
    assert out["site"]["https"] is False and not out["security"]["pass"] and not out["seo"]["pass"]
    assert "Site does not load over HTTPS" in out["reasons"]
    assert "3 images have no alt text" in out["reasons"]
    assert "2 form fields have no label" in out["reasons"]
    assert "1 link has no text" in out["reasons"]
    assert "No Meta Pixel, Google Ads, TikTok or LinkedIn tag found" in out["reasons"]
    assert "No SPF record: mail from this domain is easy to spoof" in out["reasons"]
    points = {row["rule"]: row["points"] for row in out["score_breakdown"]}
    assert points["no-https"] == 20 and points["images_without_alt"] == 6 and points["no_ad_pixel"] == 8
    assert "no_analytics" not in points  # Google Tag Manager is on the page
    assert out["score"] == min(100, sum(points.values())) and out["score_band"] in ("high", "very_high")
    assert out["reasons"] == [row["detail"] for row in out["score_breakdown"]]
    assert out["score_breakdown"] == sorted(out["score_breakdown"], key=lambda r: -r["points"])
    assert out["tech"]["platform"] == "WordPress"
    assert out["company"] is None and out["sections_failed"] == ["company"]
    note = next(n for n in out["notes"] if n.startswith("company:"))
    assert "No company was found" in note and "Nothing was charged" not in note


def test_a_site_that_cannot_be_fetched_fails_the_job(monkeypatch):
    _stub(monkeypatch)
    with pytest.raises(W.runtime.TransientProviderError):
        _qualify({"url": "https://joesplumbing.example/"}, _Ctx(error=W.runtime.TransientProviderError("timed out")))
    with pytest.raises(W.runtime.TransientProviderError) as down:
        _qualify({"url": "https://joesplumbing.example/"}, _Ctx(_page(status=503)))
    assert down.value.reason == "site_error" and "503" in down.value.detail
    with pytest.raises(W.runtime.PermanentProviderError) as missing:
        _qualify({"url": "https://joesplumbing.example/"}, _Ctx(_page(status=404)))
    assert missing.value.reason == "site_error"
    with pytest.raises(W.runtime.PermanentProviderError) as pdf:
        _qualify({"url": "https://joesplumbing.example/menu.pdf"}, _Ctx(_page(content_type="application/pdf")))
    assert pdf.value.reason == "not_html"
    with pytest.raises(W.runtime.PermanentProviderError):
        _qualify({"url": "https://joesplumbing.example/"}, _Ctx(_page(html="   ")))


def test_a_failing_supplementary_step_is_a_note_not_a_failure(monkeypatch):
    _stub(monkeypatch, dns=W.runtime.TransientProviderError("No nameserver answered for joesplumbing.example."),
          company=RuntimeError("boom"))
    out = _qualify({"url": "https://joesplumbing.example/"})
    jsonschema.Draft202012Validator(_schema()).validate(out)
    assert out["email_domain"] is None and out["company"] is None
    assert out["sections_failed"] == ["email_domain", "company"]
    assert any(n.startswith("email_domain: No nameserver") for n in out["notes"])
    assert any(n.startswith("company: RuntimeError: boom") for n in out["notes"])
    assert any("sections_failed" in n for n in out["notes"])
    assert not any(row["rule"] in ("no_mx", "no_spf", "no_dmarc") for row in out["score_breakdown"])
    assert out["sources"] == ["The page itself: one plain HTTP GET of the URL (HTML and response headers), no browser"]


def test_a_slow_supplementary_step_is_cut_at_its_budget(monkeypatch):
    async def slow(ctx, payload):
        await asyncio.sleep(2)
        return ENRICHED
    _stub(monkeypatch, company=slow)
    monkeypatch.setattr(LEAD, "COMPANY_SECONDS", 0.2)
    out = _qualify({"url": "https://joesplumbing.example/"})
    assert out["company"] is None and out["sections_failed"] == ["company"]
    assert any(n.startswith("company: did not finish within") for n in out["notes"])
    assert out["email_domain"]["mx_present"]  # the other read still shipped


# --- the score rule --------------------------------------------------------------------------

def _all_findings(table):
    return {"pass": False, "checks": "", "findings": [{"id": k, "severity": "critical", "detail": k} for k in table]}


def test_the_score_rule_is_fixed_capped_and_deterministic():
    nothing = {"pass": True, "checks": "", "findings": []}
    signals = {"images_without_alt": 0, "inputs_without_label": 0, "links_without_text": 0}
    tech = {"has_analytics": True, "has_ad_pixel": True, "has_chat": True}
    mail_ok = {"mx_present": True, "spf": {"record": "v=spf1 -all"}, "dmarc": {"record": "v=DMARC1; p=reject"}}
    clean = LEAD.score_findings(nothing, nothing, signals, tech, mail_ok, 300)
    assert clean == {"score": 0, "score_band": "low", "reasons": [], "score_breakdown": []}
    worst = LEAD.score_findings(_all_findings(LEAD.SEO_POINTS), _all_findings(LEAD.SECURITY_POINTS),
                                {"images_without_alt": 50, "inputs_without_label": 50, "links_without_text": 50},
                                {"has_analytics": False, "has_ad_pixel": False, "has_chat": False},
                                {"mx_present": False, "spf": None, "dmarc": None}, 5000)
    assert worst["score"] == 100 and worst["score_band"] == "very_high"
    assert sum(r["points"] for r in worst["score_breakdown"]) > 100  # capped, not scaled
    points = {r["rule"]: r["points"] for r in worst["score_breakdown"]}
    assert points["images_without_alt"] == 10 and points["inputs_without_label"] == 6 and points["links_without_text"] == 6
    assert points["no_mx"] == 6 and "no_spf" not in points and points["slow_response"] == 4
    assert worst == LEAD.score_findings(_all_findings(LEAD.SEO_POINTS), _all_findings(LEAD.SECURITY_POINTS),
                                        {"images_without_alt": 50, "inputs_without_label": 50, "links_without_text": 50},
                                        {"has_analytics": False, "has_ad_pixel": False, "has_chat": False},
                                        {"mx_present": False, "spf": None, "dmarc": None}, 5000)
    mid = LEAD.score_findings(nothing, nothing, dict(signals, images_without_alt=1), tech,
                              {"mx_present": True, "spf": None, "dmarc": None}, 1600)
    assert [r["rule"] for r in mid["score_breakdown"]] == ["no_spf", "no_dmarc", "images_without_alt", "slow_response"]
    assert mid["reasons"][2] == "1 image has no alt text" and mid["score"] == 10
    assert LEAD.score_findings(nothing, nothing, signals, tech, None, None)["score"] == 0  # unknown mail: no points
    # The rule the result publishes names every number above.
    for n in ("20", "max 10", "max 6", "no ad pixel 8", "no MX 6", "over 3 s 4"):
        assert n in LEAD.SCORE_RULE


# --- the readers -------------------------------------------------------------------------------

def test_platform_and_tags_are_read_from_the_page_only():
    tech = LEAD.detect_tech(BARE_HTML, {"server": "Apache", "x-powered-by": "PHP/8.2"})
    assert tech["platform"] == "WordPress" and tech["analytics"] == ["Google Tag Manager"]
    assert tech["ad_pixels"] == [] and tech["chat_widgets"] == [] and tech["server"] == "Apache"
    assert tech["has_analytics"] and not tech["has_ad_pixel"] and not tech["has_chat"]
    shop = LEAD.detect_tech("<html></html>", {"x-shopid": "123"})
    assert shop["platform"] == "Shopify"
    wix = LEAD.detect_tech('<meta name="generator" content="Wix.com Website Builder">'
                           '<script src="https://static.parastorage.com/x.js"></script>', {})
    assert wix["platform"] == "Wix" and wix["generator"] == "Wix.com Website Builder"
    tags = LEAD.detect_tech("""<script src="https://www.googletagmanager.com/gtag/js?id=AW-123456789"></script>
        <script>snap.licdn.com; _linkedin_partner_id = "1";</script><script src="https://analytics.tiktok.com/i18n/pixel/events.js"></script>
        <script src="https://embed.tawk.to/abc/default"></script><script src="https://static.hotjar.com/c/hotjar-1.js"></script>""", {})
    assert tags["ad_pixels"] == ["Google Ads tag", "TikTok Pixel", "LinkedIn Insight Tag"]
    assert tags["chat_widgets"] == ["Tawk.to"] and tags["analytics"] == ["Hotjar"]
    nothing = LEAD.detect_tech("<html><body>hi</body></html>", {})
    assert nothing["platform"] is None and nothing["generator"] is None and not nothing["has_analytics"]
    assert "ad platform" in nothing["source"]


def test_accessibility_signals_count_what_the_html_says():
    s = LEAD.accessibility_signals(BARE_HTML)
    assert (s["images_total"], s["images_without_alt"]) == (4, 3)      # alt="" is present, decorative
    assert (s["inputs_total"], s["inputs_without_label"]) == (2, 2)   # hidden input excluded
    assert (s["links_total"], s["links_without_text"]) == (3, 1)      # aria-label inside counts as named
    assert s["html_lang"] is None and s["html_lang_present"] is False
    assert s["full_audit"] == "/audit/wcag"
    wrapped = LEAD.accessibility_signals('<html lang="fr"><label>Name <input type="text"></label>'
                                         '<input type="search" aria-label="Search"><select id="s"></select>'
                                         '<a href="/a" title="Home"></a><a>no href</a></html>')
    assert wrapped["inputs_total"] == 3 and wrapped["inputs_without_label"] == 1  # the select
    assert wrapped["links_total"] == 1 and wrapped["links_without_text"] == 0
    assert wrapped["html_lang"] == "fr"


# --- input and the row ------------------------------------------------------------------------

def test_bad_input_is_refused_before_the_gate(monkeypatch):
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason",
                        lambda url: "points at an internal host" if "internal" in url else None)
    for bad in ({}, {"url": "ftp://x.example"}, {"url": "https://"}, {"url": "https://box.internal/"},
                {"url": "https://x.example", "business_name": ""}, {"url": "https://x.example", "business_name": "n" * 201},
                {"url": "https://x.example", "business_name": 7}, "not an object"):
        with pytest.raises(W.runtime.InvalidRequest):
            LEAD.precheck(bad)
    assert W.skills.PRECHECKS[WORKER] is LEAD.precheck
    assert LEAD.parse({"url": "https://www.Bücher.example/shop"})["domain"] == "xn--bcher-kva.example"


def test_the_row_is_priced_on_the_ladder_and_composes_existing_bees():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.25 and w.tier == "standard"
    assert w.composes == ["domain.dns", "company.enrich"] and w.requires == ["web", "dnsintel", "company_data"]
    assert all(name in W.catalog.BY_NAME for name in w.composes)
    assert w.max_seconds == 20 < W.router.deliver_later_after()  # finishes inline, never handed back
    assert LEAD.FETCH_SECONDS <= w.max_seconds and LEAD.DNS_SECONDS <= w.max_seconds
    assert LEAD.COMPANY_SECONDS <= w.max_seconds
    assert "checked_at" in _schema()["required"] and len(w.description) <= 500
    assert "owner's decision" in w.pricing_basis
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert w.input_schema["required"] == ["url"] and "language" in w.input_schema["properties"]
    assert 2 <= len(W.catalog.contract.REPRESENTATIVE_QUERIES[WORKER]) <= 5
    assert W.skills.REGISTRY[w.skill] is LEAD.qualify and w.available()
    assert WORKER in W.catalog.LOCALIZED_WORKERS  # notes, reasons and details translate on `language`


def test_the_worker_never_touches_the_browser():
    source = (PKG / "skills" / "lead.py").read_text()
    for forbidden in ("with_page", "playwright", "browser_pool", "web.PROVIDERS", "extract_page", "goto_guarded"):
        assert forbidden not in source, forbidden
    assert re.search(r"ctx\.run\(\s*\"fetch\",\s*web\.FETCH_PROVIDERS", source)
    assert [p.id for p in LEAD.web.FETCH_PROVIDERS] == ["http-fetch-raw"]
    # The rules are the audit routes' own, not a copy.
    rules = LEAD._audits()
    assert rules.run_seo_audit is not None and rules.run_security_audit is not None
    assert rules is sys.modules.get("wcag_audit_engine_audits") or rules.__name__.endswith("audits")


# --- over HTTP and MCP ---------------------------------------------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_lead", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"url": "ftp://joesplumbing.example"})
    assert response.status_code == 400 and response.json()["billed"] is False
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"url": "http://localhost/"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    _stub(monkeypatch)

    async def skill(ctx, payload):
        return await W.skills.lead.qualify(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"url": "https://joesplumbing.example/", "business_name": "Joe's Plumbing"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 250_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.25
    assert envelope["result"]["score"] == 0 and envelope["result"]["company"]["name"] == "Joe's Plumbing"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["tech"]["ad_pixels"] == ["Meta Pixel"]


def test_a_site_that_cannot_be_fetched_is_a_502_that_bills_nothing(client, monkeypatch):
    _stub(monkeypatch)

    async def skill(ctx, payload):
        return await W.skills.lead.qualify(_Ctx(_page(status=503)), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"url": "https://joesplumbing.example/"})
    assert response.status_code == 502, response.text
    assert response.json()["billed"] is False


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.25}
    for path in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
        on_disk = next(t for t in json.loads(path.read_text())["tools"] if t["name"] == TOOL)
        for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
            assert on_disk[field] == live[field], f"{path.name} {field} is stale"
        assert on_disk["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.25}
    assert PATH in (STATIC / "llms.txt").read_text()
    index = client.get("/work").json()
    row = next(w for w in index["workers"] if w["name"] == WORKER)
    assert row["price_usd"] == 0.25 and row["max_seconds"] == 20 and row["composes"] == ["domain.dns", "company.enrich"]



def test_company_names_are_decoded_in_this_jobs_answer_only():
    """A website's HTML can carry "&amp;" in the company name. This job shows
    "&"; company.enrich itself is not changed."""
    found = {"company": {"name": "Lex Cooling, Heating Plumbing &amp; Electrical", "legal_name": None,
                         "description": "Fast &amp; fair", "website": "https://lex.example", "founded": None,
                         "employees": None, "industries": ["Heating &amp; Air"],
                         "headquarters": {"city": "Fort Worth", "country": None, "country_code": None, "address": None}},
             "identifiers": {"wikidata": None, "lei": None, "cik": None},
             "socials": {}, "sources": ["Company website (self-declared)"], "notes": []}
    out = W.skills.lead.company_summary(found)
    assert out["name"] == "Lex Cooling, Heating Plumbing & Electrical"
    assert out["description"] == "Fast & fair" and out["industries"] == ["Heating & Air"]
    assert W.skills.lead._title("<title>A &amp; B</title>") == "A & B"
