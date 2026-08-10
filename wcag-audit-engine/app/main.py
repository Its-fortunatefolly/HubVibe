import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

try:
    from . import audits, billing, mpp_payments, x402_payments
except ImportError:
    # Loaded directly by file path (e.g. by tooling/tests) rather than as
    # part of the `app` package -- fall back to loading each sibling module
    # from its file path under a service-specific name, not a bare `import
    # billing`. Another service in this repo also has a module literally
    # named billing.py; a bare `import billing` caches whichever one loads
    # first in sys.modules and silently hands a second service the wrong
    # module if both get imported into the same process (as happens in
    # this repo's shared test suite).
    import importlib.util

    def _load_sibling_module(name: str):
        module_path = Path(__file__).resolve().parent / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"wcag_audit_engine_{name}", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    audits = _load_sibling_module("audits")  # type: ignore
    billing = _load_sibling_module("billing")  # type: ignore
    mpp_payments = _load_sibling_module("mpp_payments")  # type: ignore
    x402_payments = _load_sibling_module("x402_payments")  # type: ignore

app = FastAPI(title="WCAG Audit Engine")

STATIC_DIR = Path(__file__).resolve().parent / "static"
SERVICE_ROOT = Path(__file__).resolve().parent.parent

_axe = Axe()

# The manifest advertises "WCAG 2.1 AA" -- constrain axe-core's rule set to
# match, so a "pass" actually means what it claims instead of whatever
# axe-core's full default rule set happens to cover.
AXE_OPTIONS = {
    "resultTypes": ["violations"],
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
}

# An internal/testing key that bypasses Stripe billing entirely. Leave unset
# in production once real customers are onboarded through /billing/checkout.
API_KEY = os.environ.get("AUDIT_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional; enables remediation notes only

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))
# Best-effort, single-instance rate limiting. Cloud Run can scale to many
# instances that don't share this dict, so this alone is not sufficient for
# production abuse protection -- pair it with a quota policy at the API
# Gateway / Cloud Armor layer in front of the service.
_request_log: dict[str, deque] = defaultdict(deque)

FREE_SCAN_LIMIT_PER_DAY = int(os.environ.get("FREE_SCAN_LIMIT_PER_DAY", "3"))
# Free scans run the same real axe-core audit as the paid endpoint and cost
# real compute, so this is capped harder and keyed by IP rather than an API
# key -- it's a lead magnet, not a way to get unlimited paid-tier usage for
# free.
_free_scan_log: dict[str, deque] = defaultdict(deque)


class AuditRequest(BaseModel):
    html: Optional[str] = Field(None, description="Raw HTML source to audit")
    url: Optional[str] = Field(None, description="Live URL to audit instead of raw HTML")


class UrlAuditRequest(BaseModel):
    """For audit routes that need a live, fetchable URL -- security and
    performance checks inspect real HTTP responses and real network
    behavior, so raw HTML alone (no server to talk to) isn't enough."""

    url: str


class CheckoutRequest(BaseModel):
    email: str


class FreeScanRequest(BaseModel):
    url: str
    email: Optional[str] = None


class AuthContext:
    __slots__ = ("stripe_billable", "customer_id", "payment_method")

    def __init__(
        self,
        stripe_billable: bool,
        customer_id: Optional[str] = None,
        payment_method: str = "api_key",
    ):
        self.stripe_billable = stripe_billable
        self.customer_id = customer_id
        self.payment_method = payment_method


def _payment_required_response(host: Optional[str] = None, price_usd: float = 0.03) -> JSONResponse:
    """The 402 shape a caller needs to pay via x402, MPP, or get a Stripe
    API key -- returned whenever none of those is attached and valid (or
    a subscriber is over their included monthly quota). This is the sole
    "access denied" outcome for a paid audit route; there is no path that
    falls through to granting access.

    Carries a WWW-Authenticate: Payment header per configured MPP method
    (spec-required for MPP conformance -- see mpp_payments.py), bound to the
    request's own Host header as the MPP realm and priced at this specific
    route's rate, plus the x402-style JSON body for callers that read
    price/payTo from the body instead. Cache-Control: no-store is required
    by the MPP core spec on every 402.
    """
    response = JSONResponse(
        status_code=402, content=x402_payments.payment_required_body(price=f"${price_usd:.2f}")
    )
    response.headers["Cache-Control"] = "no-store"
    for header_value in mpp_payments.www_authenticate_headers(realm=host, price_usd=price_usd):
        response.headers.append("WWW-Authenticate", header_value)
    return response


def _authenticate(
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
    host: Optional[str] = None,
    price_usd: float = 0.03,
):
    """Returns an AuthContext on success, or a 402 JSONResponse on failure.

    Three independent paths, checked cheapest-first, any one sufficient:
    1. X-API-Key -- internal test key, or a real Stripe-issued key that
       still has quota left in its included monthly allowance (once a
       subscriber exceeds SAAS_MONTHLY_QUOTA scans this calendar month,
       the bare key stops being sufficient on its own and falls through
       to x402/MPP below, same as the landing page describes).
    2. X-PAYMENT -- x402 (crypto only), verified against a facilitator,
       for exactly `price_usd`.
    3. Authorization: Payment ... -- MPP (Stripe SPT for fiat, or Tempo for
       crypto), verified directly against Stripe / the Tempo network, for
       exactly `price_usd`. `host` (the request's own Host header) must
       match the realm the challenge was originally issued with.
    """
    if x_api_key:
        if API_KEY and secrets.compare_digest(x_api_key, API_KEY):
            # Internal/testing key: unlimited, unmetered, never billed,
            # never quota-limited.
            return AuthContext(stripe_billable=False, payment_method="internal")
        if billing.is_configured():
            record = billing.lookup_key(x_api_key)
            if record is not None and billing.check_and_increment_quota(record["customer_id"]):
                return AuthContext(
                    stripe_billable=True,
                    customer_id=record["customer_id"],
                    payment_method="stripe",
                )

    if x_payment and x402_payments.verify_and_settle_sync(x_payment, price=f"${price_usd:.2f}"):
        # Payment already verified and settled on-chain by the facilitator
        # -- nothing further to bill via Stripe.
        return AuthContext(stripe_billable=False, payment_method="x402")

    if authorization and authorization.startswith("Payment "):
        credential = authorization[len("Payment "):].strip()
        if credential and mpp_payments.verify_and_settle_sync(credential, realm=host):
            # Already charged/settled (Stripe PaymentIntent or on-chain
            # Tempo transfer) inside verify_and_settle_sync -- nothing
            # further to bill. Note the credential itself carries the
            # price (embedded in its HMAC-bound challenge), so there's
            # nothing further to pass here beyond the realm check.
            return AuthContext(stripe_billable=False, payment_method="mpp")

    return _payment_required_response(host=host, price_usd=price_usd)


def _authorize_and_rate_limit(
    x_api_key: Optional[str],
    x_payment: Optional[str],
    authorization: Optional[str],
    request: Request,
    price_usd: float,
):
    """Shared fail-closed auth + best-effort rate limiting for every paid
    audit route. Returns (AuthContext, None) on success, or (None,
    JSONResponse) when the caller should get that response immediately
    instead of the route continuing.
    """
    auth = _authenticate(x_api_key, x_payment, authorization, host=_mpp_realm(request), price_usd=price_usd)
    if isinstance(auth, JSONResponse):
        return None, auth

    # x402/MPP payers have no API key to key the limiter on -- fall back
    # to IP. Either way this is best-effort abuse protection, not the
    # billing boundary: that's Stripe usage records / on-chain settlement.
    rate_limit_key = x_api_key or (request.client.host if request.client else "unknown")
    _check_rate_limit(rate_limit_key)
    return auth, None


def _bill(auth, units: int = 1) -> Optional[str]:
    """Records usage for a Stripe-subscription caller after a real audit
    ran; a no-op for internal/x402/MPP callers, who are unmetered or
    already settled. Returns a warning string on failure (never raises --
    the customer already got a real, correct audit result, so a billing
    hiccup should never withhold or corrupt that), or None on success/no-op.
    """
    if not auth.stripe_billable:
        return None
    try:
        billing.record_usage(auth.customer_id, units=units)
        return None
    except Exception as exc:
        return f"usage recording failed: {exc}"


def _check_rate_limit(key: str) -> None:
    now = time.time()
    window = _request_log[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)


def _check_free_scan_limit(client_id: str) -> None:
    now = time.time()
    window = _free_scan_log[client_id]
    while window and now - window[0] > 86400:
        window.popleft()
    if len(window) >= FREE_SCAN_LIMIT_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Free scan limit reached for today. Sign up for monitoring to scan more.",
        )
    window.append(now)


def _run_axe(html: Optional[str], url: Optional[str]) -> dict:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        try:
            if url:
                page.goto(url, wait_until="networkidle", timeout=15000)
            else:
                page.set_content(html, wait_until="networkidle", timeout=15000)
            results = _axe.run(page, options=AXE_OPTIONS)
        finally:
            browser.close()
    return results.response


def _remediation_notes(violations: list) -> Optional[dict]:
    """Best-effort, clearly-labeled AI remediation suggestions.

    This never influences pass/fail -- axe-core's findings are the sole
    source of truth for the audit result. If this fails or is disabled,
    the audit result is returned without it; it never gets folded into
    a fabricated pass.
    """
    if not GEMINI_API_KEY or not violations:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=GEMINI_API_KEY)
        summary = "\n".join(f"- {v['id']}: {v['help']}" for v in violations[:20])
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Given these axe-core WCAG violations, write a short, "
                f"actionable remediation note for each:\n{summary}"
            ),
            config={"temperature": 0.2},
        )
        return {"ai_generated": True, "notes": response.text}
    except Exception as exc:
        return {"ai_generated": True, "notes": None, "error": str(exc)}


@app.get("/", response_class=FileResponse)
def landing_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/billing/success", response_class=FileResponse)
def checkout_success_page():
    return FileResponse(STATIC_DIR / "success.html")


@app.get("/billing/cancel", response_class=FileResponse)
def checkout_cancel_page():
    return FileResponse(STATIC_DIR / "cancel.html")


@app.get("/llms.txt", response_class=FileResponse)
def llms_txt():
    return FileResponse(STATIC_DIR / "llms.txt", media_type="text/plain")


@app.get("/mcp.json", response_class=FileResponse)
def mcp_manifest():
    return FileResponse(SERVICE_ROOT / "mcp.json", media_type="application/json")


@app.get("/healthz")
def health_check():
    return {"status": "ok", "service": "wcag-audit-engine"}


@app.post("/scan/free")
def free_scan(payload: FreeScanRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    _check_free_scan_limit(client_ip)

    try:
        raw = _run_axe(None, payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": f"Scan could not complete: {exc}"},
        )

    violations = raw.get("violations", [])
    impact_rank = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}
    FREE_SCAN_SHOWN_ISSUES = 2
    top_issues = sorted(violations, key=lambda v: impact_rank.get(v.get("impact"), 4))[
        :FREE_SCAN_SHOWN_ISSUES
    ]
    hidden_count = max(0, len(violations) - len(top_issues))

    try:
        billing.save_lead(payload.url, payload.email, len(violations))
    except Exception:
        # Lead capture is a bonus, not something that should break the free
        # scan a visitor is waiting on -- e.g. Firestore isn't configured
        # on this deployment yet.
        pass

    if violations:
        note = (
            f"Showing {len(top_issues)} of {len(violations)} issue(s) found. "
            f"{hidden_count} more not shown here -- sign up for the full "
            "list, exact locations, and continuous monitoring. Automated "
            "scanning catches a meaningful share of WCAG issues, not all of "
            "them -- this is not a compliance certification."
            if hidden_count > 0
            else "That's the only issue this scan found. Automated scanning "
            "catches a meaningful share of WCAG issues, not all of them -- "
            "this is not a compliance certification, so it's still worth a "
            "full audit."
        )
    else:
        note = (
            "No issues found in this snapshot. Automated scanning catches a "
            "meaningful share of WCAG issues, not all of them -- this is "
            "not a compliance certification."
        )

    return {
        "status": "ok",
        "pass": len(violations) == 0,
        "total_violations": len(violations),
        "top_issues": [
            {"id": v["id"], "impact": v.get("impact"), "help": v.get("help")} for v in top_issues
        ],
        "note": note,
    }


_AUTH_DESCRIPTION = (
    "One of: X-API-Key header (Stripe subscription billing, see "
    "/billing/checkout -- included scans/month, then falls back to "
    "per-call payment below); X-PAYMENT header (x402, see 402 response "
    "body for price/network/payTo); or Authorization: Payment ... (MPP -- "
    "Stripe SPT for fiat or Tempo network for crypto, see the "
    "WWW-Authenticate response headers on a 402 for both challenges)"
)
_PAYMENT_METHODS = ["stripe_api_key", "x402", "mpp"]
_URL_INPUT_SCHEMA = {"url": "string (required)"}
_HTML_OR_URL_INPUT_SCHEMA = {"html": "string (optional)", "url": "string (optional, one of html/url required)"}


@app.get("/.well-known/agent.json")
def agent_manifest():
    return {
        "schema_version": "1.0",
        "name": "HubVibe Site Compliance Auditing Suite",
        "description": (
            "Rule-based, verifiable site audits -- accessibility (axe-core), "
            "SEO, security headers, and performance -- callable a la carte "
            "or as a single bundle. Every result is a deterministic check "
            "against the actual page; nothing here is an LLM guessing at "
            "quality, and a check that couldn't run is never reported as a "
            "false pass."
        ),
        "endpoints": [
            {
                "path": "/audit",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _HTML_OR_URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "note": "Alias of /audit/wcag, kept for backward compatibility.",
            },
            {
                "path": "/audit/wcag",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _HTML_OR_URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "description": "WCAG 2.1 A/AA accessibility audit via axe-core.",
            },
            {
                "path": "/audit/seo",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _HTML_OR_URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "description": (
                    "Title, meta description, H1 structure, canonical link, "
                    "OpenGraph tags, structured data, and lang attribute."
                ),
            },
            {
                "path": "/audit/security",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "description": (
                    "HTTPS, HSTS, CSP, X-Content-Type-Options, clickjacking "
                    "protection, Referrer-Policy, and CORS from a live HTTP "
                    "response -- not a TLS/cipher scan or a penetration test."
                ),
            },
            {
                "path": "/audit/performance",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "description": (
                    "DOM node count, transferred bytes, and request count "
                    "from one real page load -- not a full Lighthouse audit."
                ),
            },
            {
                "path": "/audit/bundle",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.10,
                "input": _URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": _PAYMENT_METHODS,
                "description": "Runs wcag + seo + security + performance against one URL, billed as a single call.",
            },
            {
                "path": "/scan/free",
                "method": "POST",
                "payment_required": False,
                "rate_limit": "3/day per IP",
                "note": "WCAG only, top-2 issues shown; use /audit/wcag for the full report.",
            },
        ],
    }


@app.post("/billing/checkout")
def start_checkout(payload: CheckoutRequest, request: Request):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    # Default to this same deployment's own success/cancel pages so the
    # funnel works out of the box; override via env vars only if the
    # public-facing URL differs (e.g. a custom domain in front of Cloud Run).
    base = str(request.base_url).rstrip("/")
    success_url = os.environ.get("CHECKOUT_SUCCESS_URL", f"{base}/billing/success")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL", f"{base}/billing/cancel")
    checkout_url = billing.create_checkout_session(payload.email, success_url, cancel_url)
    return {"checkout_url": checkout_url}


@app.get("/billing/api-key")
def get_api_key(session_id: str):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    api_key = billing.api_key_for_session(session_id)
    if api_key is None:
        # The webhook that mints the key may not have landed yet -- this is
        # a normal, expected state right after checkout, not an error.
        return JSONResponse(status_code=202, content={"status": "pending"})
    return {"api_key": api_key}


@app.post("/billing/webhook")
async def stripe_webhook(request: Request):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    try:
        event = billing.verify_webhook(payload, sig_header)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook signature: {exc}")
    if event["type"] == "checkout.session.completed":
        billing.activate_customer(event["data"]["object"])
    return {"received": True}


def _mpp_realm(request: Request) -> Optional[str]:
    """MPP realm SHOULD be the server's bare hostname -- strip the port off
    the Host header (":8811" locally, absent behind Cloud Run's HTTPS
    frontend, but strip it either way rather than depend on that)."""
    host = request.headers.get("host")
    return host.split(":")[0] if host else None


@app.post("/audit")
def audit(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.03)
    if err:
        return err

    if not payload.html and not payload.url:
        raise HTTPException(status_code=400, detail="Provide 'html' or 'url'")

    try:
        raw = _run_axe(payload.html, payload.url)
    except Exception as exc:
        # Honest failure: an audit that didn't run is never reported as a
        # compliance pass, and it is never billed -- callers only pay for
        # an audit that actually happened.
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "pass": None,
                "detail": f"Audit could not complete: {exc}",
            },
        )

    violations = raw.get("violations", [])
    result = {
        "status": "ok",
        "pass": len(violations) == 0,
        "engine": "axe-core",
        "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
        "violations": [
            {
                "id": v["id"],
                "impact": v.get("impact"),
                "help": v.get("help"),
                "help_url": v.get("helpUrl"),
                "nodes_affected": len(v.get("nodes", [])),
            }
            for v in violations
        ],
    }

    warning = _bill(auth)
    if warning:
        result["billing_warning"] = warning

    remediation = _remediation_notes(violations)
    if remediation is not None:
        result["remediation"] = remediation
    return result


@app.post("/audit/wcag")
def audit_wcag(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Identical to /audit -- same axe-core check, same $0.03 price, kept
    as its own path alongside the other 4 audit dimensions so a caller can
    request accessibility specifically without relying on /audit's name."""
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.03)
    if err:
        return err

    if not payload.html and not payload.url:
        raise HTTPException(status_code=400, detail="Provide 'html' or 'url'")

    try:
        raw = _run_axe(payload.html, payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "pass": None, "detail": f"Audit could not complete: {exc}"},
        )

    violations = raw.get("violations", [])
    result = {
        "status": "ok",
        "pass": len(violations) == 0,
        "engine": "axe-core",
        "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
        "violations": [
            {
                "id": v["id"],
                "impact": v.get("impact"),
                "help": v.get("help"),
                "help_url": v.get("helpUrl"),
                "nodes_affected": len(v.get("nodes", [])),
            }
            for v in violations
        ],
    }
    warning = _bill(auth)
    if warning:
        result["billing_warning"] = warning
    return result


@app.post("/audit/seo")
def audit_seo(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.03)
    if err:
        return err

    if not payload.html and not payload.url:
        raise HTTPException(status_code=400, detail="Provide 'html' or 'url'")

    try:
        result = audits.run_seo_audit(payload.html, payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "pass": None, "detail": f"Audit could not complete: {exc}"},
        )

    warning = _bill(auth)
    if warning:
        result["billing_warning"] = warning
    return result


@app.post("/audit/security")
def audit_security(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.03)
    if err:
        return err

    try:
        result = audits.run_security_audit(payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "pass": None, "detail": f"Audit could not complete: {exc}"},
        )

    warning = _bill(auth)
    if warning:
        result["billing_warning"] = warning
    return result


@app.post("/audit/performance")
def audit_performance(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.03)
    if err:
        return err

    try:
        result = audits.run_performance_audit(payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "pass": None, "detail": f"Audit could not complete: {exc}"},
        )

    warning = _bill(auth)
    if warning:
        result["billing_warning"] = warning
    return result


@app.post("/audit/bundle")
def audit_bundle(
    payload: UrlAuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Runs all four audits against one URL. Priced and billed as a single
    $0.10 unit, not four separate $0.03 charges -- if any dimension fails
    to run, the whole call fails (502) and nothing is billed, since a
    partial bundle isn't the product being sold here."""
    auth, err = _authorize_and_rate_limit(x_api_key, x_payment, authorization, request, price_usd=0.10)
    if err:
        return err

    try:
        wcag_raw = _run_axe(None, payload.url)
        wcag_violations = wcag_raw.get("violations", [])
        wcag_result = {
            "status": "ok",
            "pass": len(wcag_violations) == 0,
            "engine": "axe-core",
            "violations": [
                {
                    "id": v["id"],
                    "impact": v.get("impact"),
                    "help": v.get("help"),
                    "help_url": v.get("helpUrl"),
                    "nodes_affected": len(v.get("nodes", [])),
                }
                for v in wcag_violations
            ],
        }
        seo_result = audits.run_seo_audit(None, payload.url)
        security_result = audits.run_security_audit(payload.url)
        performance_result = audits.run_performance_audit(payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "pass": None, "detail": f"Bundle audit could not complete: {exc}"},
        )

    result = {
        "status": "ok",
        "pass": all(
            r["pass"] for r in (wcag_result, seo_result, security_result, performance_result)
        ),
        "wcag": wcag_result,
        "seo": seo_result,
        "security": security_result,
        "performance": performance_result,
    }
    # 3 units against the existing $0.03 meter (~$0.09) for Stripe
    # subscribers -- see billing.record_usage's docstring for why this is
    # an approximation of the $0.10 price rather than exact.
    warning = _bill(auth, units=3)
    if warning:
        result["billing_warning"] = warning
    return result
