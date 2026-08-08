import os
import secrets
import time
from collections import defaultdict, deque
from typing import Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

try:
    from . import billing
except ImportError:
    # Loaded directly by file path (e.g. by tooling/tests) rather than as
    # part of the `app` package -- fall back to a path-relative import so
    # the module still resolves without requiring package context.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import billing  # type: ignore

app = FastAPI(title="WCAG Audit Engine")

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


class AuditRequest(BaseModel):
    html: Optional[str] = Field(None, description="Raw HTML source to audit")
    url: Optional[str] = Field(None, description="Live URL to audit instead of raw HTML")


class CheckoutRequest(BaseModel):
    email: str


class AuthContext:
    __slots__ = ("billable", "customer_id")

    def __init__(self, billable: bool, customer_id: Optional[str] = None):
        self.billable = billable
        self.customer_id = customer_id


def _authenticate(x_api_key: Optional[str]) -> AuthContext:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")
    if API_KEY and secrets.compare_digest(x_api_key, API_KEY):
        # Internal/testing key: unlimited, unmetered, never billed.
        return AuthContext(billable=False)
    if not billing.is_configured():
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    record = billing.lookup_key(x_api_key)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return AuthContext(billable=True, customer_id=record["customer_id"])


def _check_rate_limit(key: str) -> None:
    now = time.time()
    window = _request_log[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
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


@app.get("/")
def health_check():
    return {"status": "ok", "service": "wcag-audit-engine"}


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
                "price_usd": 0.01,
                "auth": "X-API-Key header required (see /billing/checkout)",
            }
        ],
    }


@app.post("/billing/checkout")
def start_checkout(payload: CheckoutRequest):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
    success_url = os.environ.get("CHECKOUT_SUCCESS_URL")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL")
    if not success_url or not cancel_url:
        raise HTTPException(
            status_code=501,
            detail="CHECKOUT_SUCCESS_URL / CHECKOUT_CANCEL_URL are not configured",
        )
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
def audit(payload: AuditRequest, x_api_key: Optional[str] = Header(None)):
    auth = _authenticate(x_api_key)
    _check_rate_limit(x_api_key)

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

    if auth.billable:
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
