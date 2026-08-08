"""Real, rule-based cookie/tracker compliance scan.

Not an LLM guessing at compliance -- this inspects actual cookies set
before any consent interaction, matches them against a known-tracker
database, and detects known consent-management-platform (CMP) scripts and
privacy-policy links via DOM inspection. Same verifiable-signal approach
commercial tools (Cookiebot, Osano) use, not a fabricated "AI compliance
score." Like the WCAG engine, this is a risk-reduction signal, not a legal
compliance certification -- it can't see server-side tracking, and its
tracker database isn't exhaustive.
"""

from playwright.sync_api import sync_playwright

# Cookie name -> (vendor, category). Keys ending in "_" are matched as
# prefixes (e.g. GA4's "_ga_<container-id>"); everything else is an exact
# match. Not exhaustive, but covers the trackers responsible for the large
# majority of real-world pre-consent tracking findings.
KNOWN_TRACKER_COOKIES = {
    "_ga": ("Google Analytics", "analytics"),
    "_gid": ("Google Analytics", "analytics"),
    "_gat": ("Google Analytics", "analytics"),
    "_ga_": ("Google Analytics 4", "analytics"),
    "_gcl_au": ("Google Ads", "advertising"),
    "IDE": ("Google DoubleClick", "advertising"),
    "test_cookie": ("Google DoubleClick", "advertising"),
    "NID": ("Google", "advertising"),
    "_fbp": ("Meta/Facebook Pixel", "advertising"),
    "fr": ("Meta/Facebook", "advertising"),
    "_hjSession": ("Hotjar", "analytics"),
    "_hjIncludedInSessionSample": ("Hotjar", "analytics"),
    "_hjAbsoluteSessionInProgress": ("Hotjar", "analytics"),
    "li_sugr": ("LinkedIn Insight", "advertising"),
    "bcookie": ("LinkedIn", "advertising"),
    "lidc": ("LinkedIn", "advertising"),
    "_ttp": ("TikTok Pixel", "advertising"),
    "personalization_id": ("Twitter/X", "advertising"),
    "MUID": ("Microsoft Clarity/Bing Ads", "advertising"),
    "_clck": ("Microsoft Clarity", "analytics"),
    "_clsk": ("Microsoft Clarity", "analytics"),
    "__hstc": ("HubSpot", "analytics"),
    "hubspotutk": ("HubSpot", "analytics"),
    "mp_": ("Mixpanel", "analytics"),
    "amplitude_id": ("Amplitude", "analytics"),
    "ajs_": ("Segment", "analytics"),
    "cto_bundle": ("Criteo", "advertising"),
}

# (label, script-src substring) for detecting known consent platforms.
KNOWN_CMP_SCRIPT_SIGNATURES = [
    ("OneTrust", "cookielaw.org"),
    ("OneTrust", "onetrust.com"),
    ("Cookiebot", "consent.cookiebot.com"),
    ("Osano", "cmp.osano.com"),
    ("Termly", "app.termly.io"),
    ("CookieYes", "cdn-cookieyes.com"),
    ("iubenda", "cdn.iubenda.com"),
    ("TrustArc", "consent.trustarc.com"),
    ("Quantcast Choice", "quantcast.mgr.consensu.org"),
    ("Didomi", "sdk.privacy-center.org"),
]


def _classify_cookie(name: str):
    if name in KNOWN_TRACKER_COOKIES:
        return KNOWN_TRACKER_COOKIES[name]
    for prefix, classification in KNOWN_TRACKER_COOKIES.items():
        if prefix.endswith("_") and name.startswith(prefix):
            return classification
    return None


def scan_page(url: str) -> dict:
    """Load `url` fresh (no prior cookies/consent) and inspect what it does
    before any visitor interaction."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
            cookies = context.cookies()
            script_srcs = page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            link_texts = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => ({href: e.href, text: e.textContent}))"
            )
        finally:
            browser.close()

    pre_consent_trackers = []
    for cookie in cookies:
        classification = _classify_cookie(cookie["name"])
        if classification:
            vendor, category = classification
            pre_consent_trackers.append(
                {"cookie": cookie["name"], "vendor": vendor, "category": category}
            )

    detected_cmps = sorted(
        {
            label
            for label, needle in KNOWN_CMP_SCRIPT_SIGNATURES
            for src in script_srcs
            if needle in src
        }
    )

    has_privacy_link = any(
        "privacy" in (link.get("text") or "").lower() or "privacy" in (link.get("href") or "").lower()
        for link in link_texts
    )
    has_cookie_policy_link = any(
        "cookie" in (link.get("text") or "").lower() or "cookie-polic" in (link.get("href") or "").lower()
        for link in link_texts
    )

    findings = []
    if pre_consent_trackers and not detected_cmps:
        findings.append(
            {
                "id": "trackers-before-consent",
                "severity": "high",
                "detail": (
                    f"{len(pre_consent_trackers)} known tracking cookie(s) were set "
                    "before any consent-management platform was detected on the page."
                ),
            }
        )
    elif pre_consent_trackers and detected_cmps:
        findings.append(
            {
                "id": "trackers-present-cmp-detected",
                "severity": "medium",
                "detail": (
                    f"{len(pre_consent_trackers)} known tracking cookie(s) were present on "
                    f"first load, and a consent platform ({', '.join(detected_cmps)}) was "
                    "detected -- verify it actually blocks these until consent, since this "
                    "scan only confirms the cookies existed on load."
                ),
            }
        )
    if not has_privacy_link:
        findings.append(
            {
                "id": "no-privacy-policy-link",
                "severity": "medium",
                "detail": "No link containing 'privacy' was found on the page.",
            }
        )
    if not has_cookie_policy_link:
        findings.append(
            {
                "id": "no-cookie-policy-link",
                "severity": "low",
                "detail": "No link containing 'cookie' was found on the page.",
            }
        )

    return {
        "pre_consent_trackers": pre_consent_trackers,
        "detected_cmps": detected_cmps,
        "has_privacy_link": has_privacy_link,
        "has_cookie_policy_link": has_cookie_policy_link,
        "findings": findings,
    }
