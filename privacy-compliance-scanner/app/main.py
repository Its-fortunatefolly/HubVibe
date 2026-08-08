import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

try:
    from . import billing, scanner
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import billing  # type: ignore
    import scanner  # type: ignore

app = FastAPI(title="Privacy/Cookie Compliance Scanner")

STATIC_DIR = Path(__file__).resolve().parent / "static"

API_KEY = os.environ.get("SCANNER_API_KEY")  # internal/testing key, bypasses billing
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # optional; enables remediation notes only

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))
_request_log: dict[str, deque] = defaultdict(deque)

FREE_SCAN_LIMIT_PER_DAY = int(os.environ.get("FREE_SCAN_LIMIT_PER_DAY", "3"))
_free_scan_log: dict[str, deque] = defaultdict(deque)


class ScanRequest(BaseModel):
    url: str


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


def _remediation_notes(findings: list) -> Optional[dict]:
    """Best-effort, clearly-labeled AI remediation suggestions.

    Same rule as wcag-audit-engine: this never decides pass/fail -- the
    rule-based findings in scanner.py are the sole source of truth. This
    only turns those findings into plain-language "here's what to do"
    guidance, and is skipped entirely if it fails or isn't configured.
    """
    if not GEMINI_API_KEY or not findings:
        return None
    try:
        from google import genai

        summary = "\n".join(f"- {f['id']} ({f['severity']}): {f['detail']}" for f in findings)
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Given these cookie/privacy-consent scan findings, write a short, "
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
    return {"status": "ok", "service": "privacy-compliance-scanner"}


@app.get("/.well-known/agent.json")
def agent_manifest():
    return {
        "schema_version": "1.0",
        "name": "Privacy/Cookie Compliance Scanner",
        "description": (
            "Rule-based scan for tracking cookies set before consent, "
            "missing consent-management platforms, and missing privacy/"
            "cookie policy links, with optional AI-generated remediation "
            "notes. Findings and pass/fail always come from the rule "
            "engine, never the AI layer. A risk-reduction signal, not a "
            "legal compliance certification."
        ),
        "endpoints": [
            {
                "path": "/scan",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.01,
                "auth": "X-API-Key header required (see /billing/checkout)",
            },
            {
                "path": "/scan/free",
                "method": "POST",
                "payment_required": False,
                "rate_limit": "3/day per IP",
                "note": "Summary only; use /scan for the full report",
            },
        ],
    }


@app.post("/scan/free")
def free_scan(payload: ScanRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    _check_free_scan_limit(client_ip)

    try:
        result = scanner.scan_page(payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": f"Scan could not complete: {exc}"},
        )

    try:
        billing.save_lead(payload.url, None, len(result["findings"]))
    except Exception:
        pass

    return {
        "status": "ok",
        "clean": len(result["findings"]) == 0,
        "finding_count": len(result["findings"]),
        "top_findings": [f["id"] for f in result["findings"][:3]],
        "note": (
            "Summary only. This checks for known tracking cookies set before "
            "consent, known consent-platform scripts, and privacy/cookie "
            "policy links -- it isn't a legal compliance certification and "
            "can't see server-side tracking. Sign up for the full report."
        ),
    }


@app.post("/billing/checkout")
def start_checkout(payload: CheckoutRequest, request: Request):
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")
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


@app.post("/scan")
def full_scan(payload: ScanRequest, x_api_key: Optional[str] = Header(None)):
    auth = _authenticate(x_api_key)
    _check_rate_limit(x_api_key)

    try:
        result = scanner.scan_page(payload.url)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "clean": None, "detail": f"Scan could not complete: {exc}"},
        )

    response = {"status": "ok", "clean": len(result["findings"]) == 0, **result}

    if auth.billable:
        try:
            billing.record_usage(auth.customer_id)
        except Exception as exc:
            response["billing_warning"] = f"usage recording failed: {exc}"

    remediation = _remediation_notes(result["findings"])
    if remediation is not None:
        response["remediation"] = remediation

    return response
