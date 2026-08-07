import os
import secrets
import time
from collections import defaultdict, deque
from typing import Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from playwright.sync_api import sync_playwright
from pydantic import BaseModel, Field

app = FastAPI(title="WCAG Audit Engine")

_axe = Axe()

# The manifest advertises "WCAG 2.1 AA" -- constrain axe-core's rule set to
# match, so a "pass" actually means what it claims instead of whatever
# axe-core's full default rule set happens to cover.
AXE_OPTIONS = {
    "resultTypes": ["violations"],
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
}

API_KEY = os.environ.get("AUDIT_API_KEY")  # provisioned via Secret Manager at deploy time
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


def _check_auth(x_api_key: Optional[str]) -> None:
    if not API_KEY:
        # Fail closed: never serve a paid, unauthenticated endpoint because
        # the deployment forgot to set a key.
        raise HTTPException(status_code=500, detail="Service misconfigured: no API key set")
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


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
                "auth": "X-API-Key header required",
            }
        ],
    }


@app.post("/audit")
def audit(payload: AuditRequest, x_api_key: Optional[str] = Header(None)):
    _check_auth(x_api_key)
    _check_rate_limit(x_api_key)

    if not payload.html and not payload.url:
        raise HTTPException(status_code=400, detail="Provide 'html' or 'url'")

    try:
        raw = _run_axe(payload.html, payload.url)
    except Exception as exc:
        # Honest failure: an audit that didn't run is never reported as a
        # compliance pass. Callers can distinguish "compliant" from
        # "we don't know" and retry or alert accordingly.
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
    remediation = _remediation_notes(violations)
    if remediation is not None:
        result["remediation"] = remediation
    return result
