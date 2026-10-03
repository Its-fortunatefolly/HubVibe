"""lead.qualify -- one business website in, one agency-ready qualification out.

What a lead-qualifying pipeline buys as five or six separate tools -- the
website's SEO and security-header findings, static accessibility signals,
the platform and marketing tags it runs, whether its domain takes mail with
SPF and DMARC, and who the company is -- in one priced job, finished with a
0-100 opportunity score and the plain-English hooks an outreach agent can
send.

Built by CALLING what already exists, never by reimplementing it: the SEO
and security rules are app/audits.run_seo_audit and run_security_audit (the
same rules /audit/seo and /audit/security sell), the domain read is
domain.dns, the company read is company.enrich. The page is fetched ONCE,
by plain HTTP (fetch.raw's provider, every redirect hop guarded): this
worker never touches the browser pool the paid audits run on.

The page fetch is the only hard requirement. Every other step that fails is
named in `sections_failed` and `notes`, and the result still ships.
"""

import asyncio
import importlib.util
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .. import runtime
from ..providers import mailcheck, web
from . import company as company_skill
from . import osint as osint_skill
from .extract import validate_url

# Seconds per step. The page is the hard requirement; the two reads beside it
# run in parallel and are capped so a slow registry can never hold the job
# past the deliver-later window (22 s) -- typical total is well under 15 s.
FETCH_SECONDS = 15
DNS_SECONDS = 8
COMPANY_SECONDS = 12
MAX_HTML_CHARS = 1_500_000
MAX_BUSINESS_NAME = 200
FULL_WCAG_AUDIT = "/audit/wcag"
A11Y_SCOPE = ("Static signals read from the HTML only; not a WCAG audit. The full browser "
              f"WCAG audit (axe-core on the rendered page) is POST {FULL_WCAG_AUDIT}.")

_audit_rules = None


def _audits():
    """app/audits.py: run_seo_audit and run_security_audit, the rules the
    audit routes sell. Loaded on first use, under the same name main.py
    registers the module, so both share one instance and importing this
    worker package never pulls the browser pool in."""
    global _audit_rules
    if _audit_rules is not None:
        return _audit_rules
    try:
        from ... import audits  # the workers package is app.workers
    except ImportError:
        unique = "wcag_audit_engine_audits"
        audits = sys.modules.get(unique)
        if audits is None:
            spec = importlib.util.spec_from_file_location(
                unique, Path(__file__).resolve().parents[2] / "audits.py")
            audits = importlib.util.module_from_spec(spec)
            sys.modules[unique] = audits
            spec.loader.exec_module(audits)
    _audit_rules = audits
    return audits


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- input ----------------------------------------------------------------------

def domain_of(url: str) -> str:
    """The business's domain as the buyer knows it: the URL's host without a
    leading www, in ASCII. Used for the mail and company reads whatever the
    page later redirects to."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    url = validate_url(payload.get("url"))
    name = payload.get("business_name")
    if name is not None:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_BUSINESS_NAME:
            raise runtime.InvalidRequest(f"`business_name`, when given, is 1 to {MAX_BUSINESS_NAME} characters.")
        name = name.strip() or None
    return {"url": url, "business_name": name, "domain": domain_of(url)}


def precheck(payload: dict) -> None:
    parse(payload)


# --- the page: one plain HTTP GET ----------------------------------------------------

class _Fetched:
    """The two things run_security_audit reads off an httpx response: the
    final URL and the headers."""

    def __init__(self, url: str, headers: dict):
        self.url = url
        self.headers = headers


async def fetch_site(ctx, url: str) -> tuple:
    """(page, html, response_time_ms). Raises when there is no page to
    qualify: unreachable, an error status, or not HTML -- all unbilled."""
    async def call(provider):
        return await provider.fetch(url, max_chars=MAX_HTML_CHARS)
    started = time.monotonic()
    page = await ctx.run("fetch", web.FETCH_PROVIDERS, call, per_attempt_seconds=FETCH_SECONDS, max_attempts=1)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    status = page.get("status")
    final_url = page.get("final_url") or url
    if not isinstance(status, int) or status >= 500:
        raise runtime.TransientProviderError(
            f"{url} answered HTTP {status}: there is no page to qualify.", reason="site_error")
    if status >= 400:
        raise runtime.PermanentProviderError(
            f"{url} answered HTTP {status}: there is no page to qualify.", reason="site_error")
    content_type = (page.get("content_type") or "").lower()
    html = page.get("text") or ""
    if (content_type and "html" not in content_type and "xml" not in content_type) or not html.strip():
        raise runtime.PermanentProviderError(
            f"{final_url} is {content_type or 'an empty response'}, not an HTML page; nothing to qualify.",
            reason="not_html")
    return page, html, elapsed_ms


def _title(html: str):
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return None
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return text[:200] or None


# --- accessibility signals, from the HTML alone --------------------------------------

class _A11yParser(HTMLParser):
    """Counts the four static signals. Not a WCAG audit: no rendering, no
    contrast, no keyboard, no ARIA semantics beyond a label."""

    _SKIP_INPUTS = {"hidden", "submit", "button", "reset", "image"}

    def __init__(self):
        super().__init__()
        self.html_lang = None
        self.images = 0
        self.images_without_alt = 0
        self.inputs = []            # (id, labelled-by-attribute-or-wrapping-label)
        self.label_for = set()
        self._in_label = 0
        self.links = 0
        self.links_without_text = 0
        self._link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            lang = (attrs.get("lang") or "").strip()
            self.html_lang = lang or None
        elif tag == "img":
            self.images += 1
            if "alt" not in attrs:  # alt="" is a deliberate decorative image and counts as present
                self.images_without_alt += 1
            elif self._link is not None and (attrs.get("alt") or "").strip():
                self._link["named"] = True
        elif tag in ("input", "select", "textarea"):
            if tag == "input" and (attrs.get("type") or "text").lower() in self._SKIP_INPUTS:
                return
            labelled = bool(attrs.get("aria-label") or attrs.get("aria-labelledby")
                            or attrs.get("title") or self._in_label)
            self.inputs.append((attrs.get("id"), labelled))
        elif tag == "label":
            self._in_label += 1
            if attrs.get("for"):
                self.label_for.add(attrs["for"])
        elif tag == "a":
            if attrs.get("href") is None:
                return
            self.links += 1
            self._link = {"text": "", "named": bool(attrs.get("aria-label") or attrs.get("title"))}
        elif self._link is not None and attrs.get("aria-label"):
            self._link["named"] = True

    def handle_endtag(self, tag):
        if tag == "label" and self._in_label:
            self._in_label -= 1
        elif tag == "a" and self._link is not None:
            if not (self._link["text"].strip() or self._link["named"]):
                self.links_without_text += 1
            self._link = None

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] += data

    @property
    def inputs_without_label(self) -> int:
        return sum(1 for id_, labelled in self.inputs
                   if not labelled and not (id_ and id_ in self.label_for))


def accessibility_signals(html: str) -> dict:
    parser = _A11yParser()
    parser.feed(html)
    parser.close()
    return {
        "images_total": parser.images,
        "images_without_alt": parser.images_without_alt,
        "html_lang": parser.html_lang,
        "html_lang_present": parser.html_lang is not None,
        "inputs_total": len(parser.inputs),
        "inputs_without_label": parser.inputs_without_label,
        "links_total": parser.links,
        "links_without_text": parser.links_without_text,
        "scope": A11Y_SCOPE,
        "full_audit": FULL_WCAG_AUDIT,
    }


# --- platform and tags, from the page itself ------------------------------------------
# Detection reads the HTML and the response headers and nothing else: no ad
# platform or registry is asked, so nothing here is anybody's platform data.

def _rx(*patterns):
    return tuple(re.compile(p, re.I) for p in patterns)


PLATFORMS = (
    ("Shopify", _rx(r"cdn\.shopify\.com", r"Shopify\.theme", r"\.myshopify\.com")),
    ("WordPress", _rx(r"/wp-content/", r"/wp-includes/", r"/wp-json/", r'generator"[^>]*content="WordPress')),
    ("Wix", _rx(r"static\.parastorage\.com", r"wixstatic\.com", r'generator"[^>]*content="Wix')),
    ("Squarespace", _rx(r"static1\.squarespace\.com", r'generator"[^>]*content="Squarespace', r"squarespace-cdn\.com")),
    ("Webflow", _rx(r"data-wf-page", r'generator"[^>]*content="Webflow', r"assets\.website-files\.com")),
    ("GoDaddy Website Builder", _rx(r"img1\.wsimg\.com", r"Go ?Daddy Website Builder", r"godaddysites\.com")),
    ("Weebly", _rx(r"editmysite\.com", r"weeblycloud\.com")),
    ("Duda", _rx(r"cdn-website\.com", r"multiscreensite\.com")),
    ("HubSpot CMS", _rx(r'generator"[^>]*content="HubSpot', r"\.hs-sites\.com")),
    ("Framer", _rx(r"framerusercontent\.com", r'generator"[^>]*content="Framer')),
    ("BigCommerce", _rx(r"cdn\d*\.bigcommerce\.com")),
    ("Drupal", _rx(r'generator"[^>]*content="Drupal', r"/sites/default/files/")),
    ("Joomla", _rx(r'generator"[^>]*content="Joomla')),
    ("Jimdo", _rx(r"jimdo\.com", r"jimdosite\.com")),
    ("Next.js", _rx(r"/_next/static/")),
)
PLATFORM_HEADERS = {"x-shopify-stage": "Shopify", "x-shopid": "Shopify", "x-wix-request-id": "Wix",
                    "x-github-request-id": "GitHub Pages"}
ANALYTICS = (
    ("Google Analytics", _rx(r"gtag/js\?id=(?:G|UA)-", r"google-analytics\.com/(?:analytics|ga|gtag)",
                             r"\bga\(\s*['\"]create", r"\bgtag\(\s*['\"]config['\"]\s*,\s*['\"](?:G|UA)-")),
    ("Google Tag Manager", _rx(r"googletagmanager\.com/gtm\.js", r"\bGTM-[A-Z0-9]{4,}\b")),
    ("Microsoft Clarity", _rx(r"\bclarity\.ms\b")),
    ("Hotjar", _rx(r"static\.hotjar\.com")),
    ("HubSpot tracking", _rx(r"js\.hs-scripts\.com", r"js\.hsforms\.net")),
    ("Matomo", _rx(r"\bmatomo\.js\b", r"\bpiwik\.js\b")),
    ("Plausible", _rx(r"plausible\.io/js")),
    ("Fathom", _rx(r"usefathom\.com")),
)
AD_PIXELS = (
    ("Meta Pixel", _rx(r"connect\.facebook\.net/[^\"']*fbevents\.js", r"\bfbq\(")),
    ("Google Ads tag", _rx(r"\bAW-\d{6,}\b", r"googleadservices\.com", r"googleads\.g\.doubleclick\.net")),
    ("TikTok Pixel", _rx(r"analytics\.tiktok\.com", r"\bttq\.(?:load|page)\(")),
    ("LinkedIn Insight Tag", _rx(r"snap\.licdn\.com", r"_linkedin_partner_id")),
    ("Pinterest Tag", _rx(r"\bpintrk\(", r"s\.pinimg\.com/ct/")),
    ("Snap Pixel", _rx(r"sc-static\.net/scevent", r"\bsnaptr\(")),
    ("Microsoft Ads UET", _rx(r"bat\.bing\.com")),
    ("X Pixel", _rx(r"static\.ads-twitter\.com", r"\btwq\(")),
)
CHAT_WIDGETS = (
    ("Intercom", _rx(r"widget\.intercom\.io", r"\bintercomSettings\b")),
    ("Drift", _rx(r"js\.driftt\.com")),
    ("Tawk.to", _rx(r"embed\.tawk\.to")),
    ("Crisp", _rx(r"client\.crisp\.chat")),
    ("LiveChat", _rx(r"cdn\.livechatinc\.com")),
    ("Zendesk", _rx(r"static\.zdassets\.com")),
    ("Tidio", _rx(r"code\.tidio\.co")),
    ("Facebook Customer Chat", _rx(r"fb-customerchat", r"customerchat\.js")),
    ("Freshchat", _rx(r"wchat\.freshchat\.com")),
    ("Olark", _rx(r"static\.olark\.com")),
    ("Gorgias", _rx(r"config\.gorgias\.chat")),
)
_GENERATOR = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', re.I)
_GENERATOR_REVERSED = re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']generator["\']', re.I)


def _matches(table, html: str) -> list:
    return [name for name, patterns in table if any(p.search(html) for p in patterns)]


def detect_tech(html: str, headers: dict) -> dict:
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    platform = next((p for h, p in PLATFORM_HEADERS.items() if h in headers), None)
    if platform is None:
        found = _matches(PLATFORMS, html)
        platform = found[0] if found else None
    generator = _GENERATOR.search(html) or _GENERATOR_REVERSED.search(html)
    analytics, ad_pixels, chat = _matches(ANALYTICS, html), _matches(AD_PIXELS, html), _matches(CHAT_WIDGETS, html)
    return {
        "platform": platform,
        "generator": generator.group(1).strip()[:120] if generator else None,
        "server": (headers.get("server") or None) and headers["server"][:120],
        "powered_by": (headers.get("x-powered-by") or None) and headers["x-powered-by"][:120],
        "analytics": analytics,
        "ad_pixels": ad_pixels,
        "chat_widgets": chat,
        "has_analytics": bool(analytics),
        "has_ad_pixel": bool(ad_pixels),
        "has_chat": bool(chat),
        "source": "Detected from the page's own HTML and response headers only; no ad platform or registry was asked.",
    }


# --- the two reads beside the page -------------------------------------------------------

def email_summary(dns: dict) -> dict:
    """What an outreach agent needs from domain.dns: does the domain take
    mail, through whom, and is it protected against spoofing."""
    hosts = [r.split(" ", 1)[1] if " " in r else r for r in dns["records"]["mx"]]
    hosts = [h for h in hosts if h not in ("", ".")]  # a null MX ("0 .") is "no mail"
    security = dns["email_security"]
    return {"domain": dns["domain_ascii"], "exists": dns["exists"], "mx_present": bool(hosts), "mx": hosts,
            "mail_provider": mailcheck.mail_provider(hosts), "spf": security["spf"], "dmarc": security["dmarc"]}


def company_summary(found: dict) -> dict:
    company, ids = found["company"], found["identifiers"]
    return {
        "name": company["name"], "legal_name": company["legal_name"], "description": company["description"],
        "website": company["website"], "founded": company["founded"], "employees": company["employees"],
        "industries": company["industries"], "headquarters": company["headquarters"], "socials": found["socials"],
        "identifiers": {"wikidata": ids["wikidata"], "lei": ids["lei"], "cik": ids["cik"]},
        "sources": found["sources"], "notes": found["notes"],
    }


async def _step(ctx, name: str, seconds: float, coro, failed: list, notes: list):
    """Run one supplementary read inside its own budget. It fails into
    `sections_failed` and `notes`, never the job: only the page is required."""
    budget = min(seconds, ctx.remaining())
    try:
        return await asyncio.wait_for(coro, timeout=max(0.1, budget))
    except asyncio.TimeoutError:
        failed.append(name)
        notes.append(f"{name}: did not finish within its {budget:.0f} s budget.")
    except runtime.WorkerError as exc:
        failed.append(name)
        # A bee's own refusal ends "Nothing was charged." -- true of it sold
        # alone, not of this job, which is delivered and charged without it.
        detail = re.sub(r"\s*Nothing was charged\.?\s*$", "", exc.detail)
        notes.append(f"{name}: {detail}"[:240])
    except Exception as exc:  # a supplementary read must never sink the job; say why it is missing
        failed.append(name)
        notes.append(f"{name}: {type(exc).__name__}: {exc}"[:240])
    return None


# --- the score: a fixed, published rule -----------------------------------------------------
# Points are "how much there is for an agency to fix", summed and capped at
# 100. No model, no weights that move: the same page always scores the same.

SECURITY_POINTS = {
    "no-https": (20, "Site does not load over HTTPS"),
    "missing-hsts": (4, "No HSTS header"),
    "invalid-hsts": (4, "HSTS header is unreadable"),
    "hsts-disabled": (4, "HSTS is switched off (max-age=0)"),
    "missing-csp": (3, "No Content-Security-Policy header"),
    "missing-x-content-type-options": (2, "No X-Content-Type-Options header"),
    "missing-frame-protection": (2, "No clickjacking protection (X-Frame-Options or frame-ancestors)"),
    "missing-referrer-policy": (1, "No Referrer-Policy header"),
    "wildcard-cors": (1, "CORS is open to every origin"),
}
SEO_POINTS = {
    "missing-title": (8, "No page title"),
    "title-too-long": (2, "Page title is too long for search results"),
    "missing-meta-description": (8, "No meta description"),
    "meta-description-too-long": (2, "Meta description is too long for search results"),
    "missing-h1": (5, "No H1 heading"),
    "multiple-h1": (2, "More than one H1 heading"),
    "missing-canonical": (2, "No canonical link"),
    "incomplete-opengraph": (3, "Incomplete social-sharing (OpenGraph) tags"),
    "missing-structured-data": (3, "No structured data (JSON-LD)"),
    "missing-lang-attribute": (2, "No language declared on the page"),
}
COUNT_POINTS = {  # per item, cap
    "images_without_alt": (2, 10),
    "inputs_without_label": (2, 6),
    "links_without_text": (2, 6),
}
NO_ANALYTICS_POINTS = 8
NO_AD_PIXEL_POINTS = 8
NO_CHAT_POINTS = 2
NO_MX_POINTS = 6
NO_SPF_POINTS = 3
NO_DMARC_POINTS = 3
SLOW_POINTS = ((3000, 4), (1500, 2))
SCORE_RULE = (
    "Fixed points per finding, summed and capped at 100; higher means more for an agency to fix. "
    "No HTTPS 20; security headers 1-4 each (HSTS 4, CSP 3, X-Content-Type-Options 2, frame protection 2, "
    "Referrer-Policy 1, open CORS 1); SEO findings 2-8 each (no title 8, no meta description 8, no H1 5, "
    "OpenGraph 3, structured data 3, canonical 2, lang 2, long title/description 2, extra H1 2); "
    "2 per image without alt (max 10), 2 per unlabelled form field (max 6), 2 per link without text (max 6); "
    "no analytics tag 8; no ad pixel 8; no chat widget 2; no MX 6, else no SPF 3 and no DMARC 3; "
    "page response over 1.5 s 2, over 3 s 4."
)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def score_findings(seo: dict, security: dict, signals: dict, tech: dict, email_domain, response_time_ms) -> dict:
    """(score, band, reasons, breakdown) from the sections above. Pure."""
    breakdown = []

    def add(rule, points, detail):
        if points > 0:
            breakdown.append({"rule": rule, "points": points, "detail": detail})

    for finding in security.get("findings", []):
        points, hook = SECURITY_POINTS.get(finding["id"], (0, finding.get("detail", "")))
        add(finding["id"], points, hook)
    for finding in seo.get("findings", []):
        points, hook = SEO_POINTS.get(finding["id"], (0, finding.get("detail", "")))
        add(finding["id"], points, hook)
    add("images_without_alt", min(COUNT_POINTS["images_without_alt"][1],
                                  COUNT_POINTS["images_without_alt"][0] * signals["images_without_alt"]),
        _plural(signals["images_without_alt"], "image has", "images have") + " no alt text")
    add("inputs_without_label", min(COUNT_POINTS["inputs_without_label"][1],
                                    COUNT_POINTS["inputs_without_label"][0] * signals["inputs_without_label"]),
        _plural(signals["inputs_without_label"], "form field has", "form fields have") + " no label")
    add("links_without_text", min(COUNT_POINTS["links_without_text"][1],
                                  COUNT_POINTS["links_without_text"][0] * signals["links_without_text"]),
        _plural(signals["links_without_text"], "link has", "links have") + " no text")
    if not tech["has_analytics"]:
        add("no_analytics", NO_ANALYTICS_POINTS, "No analytics tag found (no Google Analytics or Tag Manager)")
    if not tech["has_ad_pixel"]:
        add("no_ad_pixel", NO_AD_PIXEL_POINTS, "No Meta Pixel, Google Ads, TikTok or LinkedIn tag found")
    if not tech["has_chat"]:
        add("no_chat_widget", NO_CHAT_POINTS, "No chat widget on the site")
    if email_domain is not None:
        if not email_domain["mx_present"]:
            add("no_mx", NO_MX_POINTS, "Domain has no mail server (no MX record): no email at this domain")
        else:
            if not email_domain["spf"]:
                add("no_spf", NO_SPF_POINTS, "No SPF record: mail from this domain is easy to spoof")
            if not email_domain["dmarc"]:
                add("no_dmarc", NO_DMARC_POINTS, "No DMARC policy on the domain")
    if response_time_ms is not None:
        for threshold, points in SLOW_POINTS:
            if response_time_ms > threshold:
                add("slow_response", points, f"Homepage took {response_time_ms} ms to answer")
                break
    score = min(100, sum(row["points"] for row in breakdown))
    band = "very_high" if score >= 75 else "high" if score >= 50 else "moderate" if score >= 25 else "low"
    ordered = sorted(breakdown, key=lambda row: -row["points"])
    return {"score": score, "score_band": band, "reasons": [row["detail"] for row in ordered],
            "score_breakdown": ordered}


# --- the job -----------------------------------------------------------------------------

async def qualify(ctx, payload: dict) -> dict:
    req = parse(payload)
    url, domain = req["url"], req["domain"]
    notes, failed = [], []

    page, html, response_time_ms = await fetch_site(ctx, url)
    final_url = page.get("final_url") or url
    headers = page.get("headers") or {}
    rules = _audits()
    fetched = _Fetched(final_url, headers)
    seo = rules.run_seo_audit(html, final_url, response=fetched)
    security = rules.run_security_audit(final_url, response=fetched)
    signals = accessibility_signals(html)
    tech = detect_tech(html, headers)

    company_body = {"domain": domain}
    if req["business_name"]:
        company_body["name"] = req["business_name"]
    dns, found = await asyncio.gather(
        _step(ctx, "email_domain", DNS_SECONDS,
              osint_skill.domain_dns(ctx, {"domain": domain, "tls": False}), failed, notes),
        _step(ctx, "company", COMPANY_SECONDS, company_skill.enrich(ctx, company_body), failed, notes),
    )
    email_domain = email_summary(dns) if dns else None
    company = company_summary(found) if found else None

    scored = score_findings(seo, security, signals, tech, email_domain, response_time_ms)
    sources = ["The page itself: one plain HTTP GET of the URL (HTML and response headers), no browser"]
    if email_domain is not None:
        sources.append("Public DNS for the domain (MX, SPF, DMARC), via domain.dns")
    if company is not None:
        sources.extend(f"{s}, via company.enrich" for s in company["sources"])
    notes.append("SEO and security findings apply the same rules as /audit/seo and /audit/security to one plain "
                 "fetch of the page; the browser audits (/audit/wcag, /audit/performance) are separate.")
    notes.append("Platform and tags are read from the page's own code only; nothing is looked up at any ad platform.")
    if failed:
        notes.append("Sections named in sections_failed are missing from the score; the rest still ship.")
    return {
        "url": url,
        "business_name": req["business_name"],
        "domain": domain,
        "site": {
            "final_url": final_url,
            "status": page["status"],
            "https": final_url.startswith("https://"),
            "redirected": final_url != url,
            "response_time_ms": response_time_ms,
            "content_type": page.get("content_type"),
            "bytes": int(page.get("bytes") or 0),
            "title": _title(html),
        },
        "seo": {"pass": seo["pass"], "checks": seo["checks"], "findings": seo["findings"]},
        "security": {"pass": security["pass"], "checks": security["checks"], "findings": security["findings"]},
        "accessibility_signals": signals,
        "tech": tech,
        "email_domain": email_domain,
        "company": company,
        **scored,
        "score_rule": SCORE_RULE,
        "sections_failed": failed,
        "sources": sources,
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"lead.qualify": qualify}
PRECHECKS = {"lead.qualify": precheck}
