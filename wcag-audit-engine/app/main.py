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
    from . import billing, x402_payments
except ImportError:
    # Loaded directly by file path (e.g. by tooling/tests) rather than as
    # part of the `app` package -- fall back to a path-relative import so
    # the module still resolves without requiring package context.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import billing  # type: ignore
    import x402_payments  # type: ignore

app = FastAPI(title="WCAG Audit Engine")

STATIC_DIR = Path(__file__).resolve().parent / "static"

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


def _payment_required_response() -> JSONResponse:
    """The 402 shape a caller needs to either pay via x402 or get a Stripe
    API key -- returned whenever neither a valid X-API-Key nor a valid
    X-PAYMENT is attached. This is the sole "access denied" outcome for
    /audit; there is no path that falls through to granting access."""
    return JSONResponse(status_code=402, content=x402_payments.payment_required_body())


def _authenticate(x_api_key: Optional[str], x_payment: Optional[str]):
    """Returns an AuthContext on success, or a 402 JSONResponse on failure.

    X-API-Key (internal test key or a real Stripe-issued key) is checked
    first since it's the cheaper check; X-PAYMENT (x402) is only verified
    against the facilitator if no valid API key was presented. Either path
    succeeding is sufficient -- this never requires both.
    """
    if x_api_key:
        if API_KEY and secrets.compare_digest(x_api_key, API_KEY):
            # Internal/testing key: unlimited, unmetered, never billed.
            return AuthContext(stripe_billable=False, payment_method="internal")
        if billing.is_configured():
            record = billing.lookup_key(x_api_key)
            if record is not None:
                return AuthContext(
                    stripe_billable=True,
                    customer_id=record["customer_id"],
                    payment_method="stripe",
                )

    if x_payment and x402_payments.verify_and_settle_sync(x_payment):
        # Payment already verified and settled on-chain by the facilitator
        # -- nothing further to bill via Stripe.
        return AuthContext(stripe_billable=False, payment_method="x402")

    return _payment_required_response()


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


@app.get("/.well-known/agent.json")
def agent_manifest():
    return {
        "schema_version": "1.0",
        "name": "WCAG Audit Engine",
        "description": (
            "Rule-based WCAG 2.1 A/AA accessibility audit powered by "
            "axe-core, with optional AI-generated remediation notes. "
            "Pass/fail is always determined by axe-core's rule engine, "
            "never by the AI layer."
        ),
        "endpoints": [
            {
                "path": "/audit",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "auth": (
                    "Either an X-API-Key header (Stripe billing, see "
                    "/billing/checkout) or an X-PAYMENT header (x402, see "
                    "402 response body for price/network/payTo)"
                ),
                "payment_methods": ["stripe_api_key", "x402"],
            },
            {
                "path": "/scan/free",
                "method": "POST",
                "payment_required": False,
                "rate_limit": "3/day per IP",
                "note": "Top-2 issues only; use /audit for the full report",
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


@app.post("/audit")
def audit(
    payload: AuditRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
):
    auth = _authenticate(x_api_key, x_payment)
    if isinstance(auth, JSONResponse):
        return auth

    # x402 payers have no API key to key the limiter on -- fall back to IP.
    # Either way this is best-effort abuse protection, not the billing
    # boundary: that's Stripe usage records / on-chain settlement, above.
    rate_limit_key = x_api_key or (request.client.host if request.client else "unknown")
    _check_rate_limit(rate_limit_key)

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

    if auth.stripe_billable:
        try:
            billing.record_usage(auth.customer_id)
        except Exception as exc:
            # Revenue-affecting, but the customer already got a real,
            # correct audit result -- never withhold or corrupt that
            # because our own billing call failed.
            result["billing_warning"] = f"usage recording failed: {exc}"

    remediation = _remediation_notes(violations)
    if remediation is not None:
        result["remediation"] = remediation
    return result
