import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from . import audits, billing, browser_pool, mpp_payments, x402_payments
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
    import sys

    def _load_sibling_module(name: str):
        # Register under the unique name in sys.modules BEFORE executing, so
        # a sibling that imports the same module by this name (audits.py ->
        # browser_pool) gets this exact instance instead of loading a second
        # copy with its own thread-local browser state.
        unique_name = f"wcag_audit_engine_{name}"
        cached = sys.modules.get(unique_name)
        if cached is not None:
            return cached
        module_path = Path(__file__).resolve().parent / f"{name}.py"
        spec = importlib.util.spec_from_file_location(unique_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
        return module

    browser_pool = _load_sibling_module("browser_pool")  # type: ignore
    audits = _load_sibling_module("audits")  # type: ignore
    billing = _load_sibling_module("billing")  # type: ignore
    mpp_payments = _load_sibling_module("mpp_payments")  # type: ignore
    x402_payments = _load_sibling_module("x402_payments")  # type: ignore

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://hubvibe-831480473793.us-south1.run.app"
)

# Each in-flight audit holds a Chromium browser (see browser_pool), so the
# ceiling on concurrent audits is really a memory ceiling, not a CPU one.
# FastAPI runs these sync routes in anyio's threadpool, which defaults to 40
# threads -- 40 simultaneous Chromium instances would OOM any reasonably
# sized container, so cap it explicitly and size the container to match
# (see README: --memory / --cpu / --concurrency should agree with this).
MAX_CONCURRENT_AUDITS = int(os.environ.get("MAX_CONCURRENT_AUDITS", "4"))


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = MAX_CONCURRENT_AUDITS
    except Exception:
        # Not fatal: worst case we run on anyio's default thread count.
        pass
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="HubVibe Site Compliance Auditing Suite",
    version="1.1.0",
    description=(
        "Machine-payable site compliance audits. Four deterministic audit "
        "dimensions -- accessibility (axe-core), SEO, security headers, and "
        "performance -- callable a la carte at $0.03/call or as a single "
        "$0.10 bundle.\n\n"
        "Built for agent-to-agent use: every paid route answers an "
        "unauthenticated request with HTTP 402 carrying a machine-readable "
        "payment challenge (x402 JSON body and/or MPP WWW-Authenticate "
        "headers), so a paying agent can discover the price and settle "
        "without a human in the loop.\n\n"
        "Every result is a rule-based check against the actual page. Nothing "
        "here is an LLM judging quality, and a check that could not run is "
        "reported as an error, never as a passing result.\n\n"
        "Discovery: /.well-known/agent.json, /llms.txt, /mcp.json, "
        "/openapi.json"
    ),
    servers=[{"url": PUBLIC_BASE_URL, "description": "Production"}],
    openapi_tags=[
        {"name": "audit", "description": "Paid, machine-payable audit routes."},
        {"name": "discovery", "description": "Manifests agents use to find and price these tools."},
        {"name": "billing", "description": "Human subscription checkout and key issuance."},
    ],
)

# Agents call this from browsers, edge workers, and other origins. There are
# no cookies or sessions here -- authentication is an explicit per-request
# header -- so a wildcard origin grants no ambient authority. WWW-Authenticate
# must be exposed or a browser-side caller literally cannot read the MPP
# payment challenge off a 402 and would have no way to pay.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["WWW-Authenticate", "Cache-Control"],
    max_age=86400,
)

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

# Ceiling per API key (or per IP for x402/MPP callers, who have no key).
# This exists to stop a runaway client from exhausting the browser pool --
# it is NOT a monetisation lever. Every request past the paywall has already
# been paid for, so throttling a paying agent is refusing revenue: keep this
# well above any legitimate caller's burst rate. 600/min = 10 req/s per key.
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "600"))

FREE_SCAN_LIMIT_PER_DAY = int(os.environ.get("FREE_SCAN_LIMIT_PER_DAY", "3"))


class _SlidingWindowLimiter:
    """Sliding-window rate limiter with bounded memory.

    The bounded part is the point. A plain dict-of-deques keyed by caller IP
    never removes the entry for an IP that made one request and went away,
    so the table grows for the lifetime of the process -- at the request
    volume this service is built for that is an eventual OOM, not a
    theoretical concern. Expired windows are swept periodically and the
    table is hard-capped as a backstop.

    Still best-effort across instances: Cloud Run runs many containers that
    don't share this state, so treat it as per-instance overload protection
    and put Cloud Armor in front for real abuse policy.
    """

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 50_000,
                 sweep_interval: float = 60.0):
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._sweep_interval = sweep_interval
        self._log: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def check(self, key: str) -> bool:
        """Record a hit and return True if allowed, False if over the limit.

        Returns a bool rather than raising so the caller decides the response
        shape -- agents need a machine-readable 429 with Retry-After, not an
        opaque error.
        """
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            window = self._log.get(key)
            if window is None:
                window = deque()
                self._log[key] = window
            cutoff = now - self._window
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= self._limit:
                return False
            window.append(now)
            return True

    def _sweep_locked(self, now: float) -> None:
        if now - self._last_sweep < self._sweep_interval and len(self._log) <= self._max_keys:
            return
        self._last_sweep = now
        cutoff = now - self._window
        for key in [k for k, w in self._log.items() if not w or w[-1] <= cutoff]:
            del self._log[key]
        if len(self._log) > self._max_keys:
            # Pathological key cardinality (e.g. a spoofed-source flood).
            # Drop the least-recently-seen half; discarding limiter state can
            # only ever forgive a request, never wrongly deny one.
            oldest = sorted(self._log, key=lambda k: self._log[k][-1])[: len(self._log) // 2]
            for key in oldest:
                del self._log[key]


_audit_limiter = _SlidingWindowLimiter(RATE_LIMIT_PER_MINUTE, 60.0)
# Free scans run the same real axe-core audit as the paid endpoint and cost
# real compute, so this is capped harder and keyed by IP rather than an API
# key -- it's a lead magnet, not a way to get unlimited paid-tier usage for
# free.
_free_scan_limiter = _SlidingWindowLimiter(FREE_SCAN_LIMIT_PER_DAY, 86_400.0)


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
    __slots__ = ("stripe_billable", "customer_id", "payment_method", "pending_payment")

    def __init__(
        self,
        stripe_billable: bool,
        customer_id: Optional[str] = None,
        payment_method: str = "api_key",
        pending_payment=None,
    ):
        self.stripe_billable = stripe_billable
        self.customer_id = customer_id
        self.payment_method = payment_method
        # An x402 payment that is verified but deliberately not yet settled --
        # see _bill. None for every other payment method.
        self.pending_payment = pending_payment


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
    price = f"${price_usd:.2f}"

    # `accepts` lists only the rails that can actually settle on THIS
    # deployment, so an agent can pick one programmatically instead of
    # guessing from prose or trying a method that was never configured.
    accepts = []
    x402_entry = x402_payments.accepts_entry(price=price)
    if x402_entry:
        accepts.append(x402_entry)
    accepts.extend(mpp_payments.accepts_entries(price_usd=price_usd))

    body = {
        "error": "payment_required",
        "price_usd": price_usd,
        "price": price,
        "accepts": accepts,
        "alternative": {
            "header": "X-API-Key",
            "detail": "Stripe subscription key; included scans/month, then per-call payment above.",
            "get_one": f"{PUBLIC_BASE_URL}/billing/checkout",
        },
        "docs": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
    }
    # Keep x402's standard top-level keys alongside `accepts` when x402 is
    # live, so off-the-shelf x402 clients that read the canonical shape keep
    # working unchanged.
    body.update(x402_payments.payment_required_body(price=price))

    response = JSONResponse(status_code=402, content=body)
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

    if x_payment:
        # Verify only -- do NOT settle here. Settlement happens in _bill, after
        # an audit has actually produced a result, so a caller whose audit
        # fails to run is never charged for nothing.
        pending = x402_payments.verify_only_sync(x_payment, price=f"${price_usd:.2f}")
        if pending is not None:
            return AuthContext(
                stripe_billable=False, payment_method="x402", pending_payment=pending
            )

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
    # Order matters, and it is load-bearing: _authenticate SETTLES REAL MONEY
    # for x402/MPP callers (verify_and_settle_sync moves funds on-chain or
    # confirms a Stripe PaymentIntent). Checking the rate limit after that
    # point meant an over-limit caller paid, then got a 429 -- money taken,
    # no audit delivered, no refund path. Anyone who is going to be rejected
    # must be rejected before their payment instrument is touched.
    #
    # x402/MPP payers have no API key to key the limiter on -- fall back to
    # IP. Either way this is per-instance overload protection, not the
    # billing boundary: that's Stripe usage records / on-chain settlement.
    rate_limit_key = x_api_key or (request.client.host if request.client else "unknown")
    if not _audit_limiter.check(rate_limit_key):
        return None, _rate_limited_response()

    auth = _authenticate(x_api_key, x_payment, authorization, host=_mpp_realm(request), price_usd=price_usd)
    if isinstance(auth, JSONResponse):
        return None, auth

    return auth, None


def _bill(auth, units: int = 1) -> Optional[str]:
    """Collect payment for an audit that actually produced a result.

    Called only on the success path, which is the whole point: every route
    returns 502 without reaching here when an audit fails to run, so a failed
    audit is never charged for. That guarantee used to hold only for Stripe
    subscribers -- x402 callers were settled during authentication, so they
    paid for failed audits too. Settling here closes that gap.

    Never raises: the caller already has a real, correct audit result in hand,
    and a billing hiccup must not withhold or corrupt it. Returns a warning
    string to surface on the response instead, or None on success/no-op.
    """
    if auth.pending_payment is not None:
        if not x402_payments.settle_sync(auth.pending_payment):
            # We delivered without collecting. Deliberately the lesser evil
            # versus charging for undelivered work, but it must be visible.
            return "payment settlement failed after the audit ran; this call was not charged"
        return None

    if not auth.stripe_billable:
        return None
    try:
        billing.record_usage(auth.customer_id, units=units)
        return None
    except Exception as exc:
        return f"usage recording failed: {exc}"


def _rate_limited_response() -> JSONResponse:
    """429 an agent can actually act on: Retry-After tells a machine caller
    when to come back instead of hammering the endpoint or giving up on it
    permanently. Nothing has been billed at this point -- the limiter runs
    before any payment is settled."""
    response = JSONResponse(
        status_code=429,
        content={
            "status": "error",
            "detail": (
                f"Rate limit exceeded ({RATE_LIMIT_PER_MINUTE} requests/minute). "
                "Nothing was charged for this request."
            ),
            "limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "retry_after_seconds": 60,
            "billed": False,
        },
    )
    response.headers["Retry-After"] = "60"
    return response


def _run_axe(html: Optional[str], url: Optional[str]) -> dict:
    def _audit(page) -> dict:
        if url:
            page.goto(url, wait_until="networkidle", timeout=15000)
        else:
            page.set_content(html, wait_until="networkidle", timeout=15000)
        return _axe.run(page, options=AXE_OPTIONS).response

    # Pooled browser, fresh isolated context per call -- see browser_pool.
    return browser_pool.with_page(_audit)


def _run_axe_and_performance(url: str):
    """One page load serving BOTH the accessibility and performance audits.

    /audit/bundle used to hit the target URL four times for a single call:
    two full Chromium page loads (axe, then performance) plus two separate
    HTTP GETs (SEO, then security). That is four times the latency on the
    most expensive route, and four hits on a stranger's origin per call is
    how an audit bot gets its user agent blocked -- which would cost us the
    ability to audit that site at all.

    Both browser-based checks need exactly the same thing: the page,
    rendered, once. The response listener must be attached before navigation
    or the measurement misses the requests it is meant to count.
    """
    stats = {"bytes": 0, "requests": 0}

    def _on_response(response):
        stats["requests"] += 1
        length = response.headers.get("content-length")
        if length and length.isdigit():
            stats["bytes"] += int(length)

    def _both(page):
        page.on("response", _on_response)
        page.goto(url, wait_until="networkidle", timeout=30000)
        dom_node_count = page.evaluate("document.querySelectorAll('*').length")
        # axe runs against the already-loaded page rather than reloading it.
        return _axe.run(page, options=AXE_OPTIONS).response, dom_node_count

    axe_raw, dom_node_count = browser_pool.with_page(_both, user_agent=audits.USER_AGENT)
    performance = audits.performance_result_from_metrics(
        dom_node_count, stats["bytes"], stats["requests"]
    )
    return axe_raw, performance


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
    return FileResponse(STATIC_DIR / "mcp.json", media_type="application/json")


@app.get("/favicon.svg", response_class=FileResponse, tags=["discovery"])
def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/og-image.png", response_class=FileResponse, tags=["discovery"])
def og_image():
    # Referenced by og:image/twitter:image. Social scrapers fetch this
    # unauthenticated and cache aggressively, so it must stay a stable,
    # public URL -- a link with no preview card is a link people don't click.
    return FileResponse(STATIC_DIR / "og-image.png", media_type="image/png")


@app.get("/robots.txt", response_class=FileResponse, tags=["discovery"])
def robots_txt():
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml", response_class=FileResponse)
def sitemap_xml():
    return FileResponse(STATIC_DIR / "sitemap.xml", media_type="application/xml")


@app.get("/healthz")
def health_check():
    return {"status": "ok", "service": "wcag-audit-engine"}


@app.post("/scan/free")
def free_scan(payload: FreeScanRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _free_scan_limiter.check(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Free scan limit reached for today. Sign up for monitoring to scan more.",
        )

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
_URL_INPUT_SCHEMA = {"url": "string (required)"}
_HTML_OR_URL_INPUT_SCHEMA = {"html": "string (optional)", "url": "string (optional, one of html/url required)"}

# One row per sellable route. Kept as data rather than hand-written JSON so
# the manifest, the pricing an agent reads, and the prices the routes
# actually charge cannot drift apart.
_CATALOG = [
    {
        "path": "/audit/wcag",
        "price_usd": 0.03,
        "input": _HTML_OR_URL_INPUT_SCHEMA,
        "description": "WCAG 2.1 A/AA accessibility audit via axe-core.",
        "returns": "pass (bool), violations[] with id/impact/help/help_url/nodes_affected.",
    },
    {
        "path": "/audit/seo",
        "price_usd": 0.03,
        "input": _HTML_OR_URL_INPUT_SCHEMA,
        "description": (
            "Title, meta description, H1 structure, canonical link, "
            "OpenGraph tags, structured data, and lang attribute."
        ),
        "returns": "pass (bool), findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/security",
        "price_usd": 0.03,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "HTTPS, HSTS, CSP, X-Content-Type-Options, clickjacking "
            "protection, Referrer-Policy, and CORS from a live HTTP "
            "response -- not a TLS/cipher scan or a penetration test."
        ),
        "returns": "pass (bool), findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/performance",
        "price_usd": 0.03,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "DOM node count, transferred bytes, and request count "
            "from one real page load -- not a full Lighthouse audit."
        ),
        "returns": "pass (bool), metrics{}, findings[] with id/severity/detail.",
    },
    {
        "path": "/audit/bundle",
        "price_usd": 0.10,
        "input": _URL_INPUT_SCHEMA,
        "description": (
            "Runs wcag + seo + security + performance against one URL, "
            "billed as a single call. Atomic: if any dimension fails to "
            "run, the whole call fails and nothing is billed."
        ),
        "returns": "pass (bool) plus wcag{}, seo{}, security{}, performance{} sub-results.",
    },
]


def _payment_methods_live() -> list:
    """Only the rails that can actually settle on this deployment.

    An agent picks a payment method from this list, so listing a method that
    isn't configured would send it down a path that cannot possibly succeed.
    """
    methods = []
    if x402_payments.is_configured():
        methods.append("x402")
    if mpp_payments.stripe_configured():
        methods.append("mpp-stripe")
    if mpp_payments.tempo_configured():
        methods.append("mpp-tempo")
    if billing.is_configured():
        methods.append("stripe_api_key")
    return methods


@app.get("/.well-known/agent.json", tags=["discovery"])
def agent_manifest(request: Request):
    base = PUBLIC_BASE_URL
    live_methods = _payment_methods_live()
    return {
        "schema_version": "1.0",
        "name": "HubVibe Site Compliance Auditing Suite",
        "base_url": base,
        "description": (
            "Rule-based, verifiable site audits -- accessibility (axe-core), "
            "SEO, security headers, and performance -- callable a la carte "
            "or as a single bundle. Every result is a deterministic check "
            "against the actual page; nothing here is an LLM guessing at "
            "quality, and a check that couldn't run is never reported as a "
            "false pass."
        ),
        "pricing": {
            "model": "per-call",
            "currency": "USD",
            "single_audit_usd": 0.03,
            "bundle_usd": 0.10,
            "subscription": {
                "usd_per_month": 49,
                "included_calls_per_month": billing.SAAS_MONTHLY_QUOTA,
                "overage": "falls back to per-call payment at the prices above",
                "checkout": f"{base}/billing/checkout",
            },
        },
        "payment": {
            "methods": live_methods,
            "challenge": (
                "Unauthenticated calls return HTTP 402 with a machine-readable "
                "`accepts` array in the body and, for MPP, one signed "
                "WWW-Authenticate: Payment challenge per method."
            ),
            "note": (
                "Only methods actually configured on this deployment are listed; "
                "an empty list means no machine payment rail is live right now."
            ),
        },
        "limits": {
            "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "on_limit": "HTTP 429 with Retry-After; nothing is billed.",
        },
        "discovery": {
            "openapi": f"{base}/openapi.json",
            "mcp": f"{base}/mcp.json",
            "llms_txt": f"{base}/llms.txt",
            "docs": f"{base}/docs",
        },
        "guarantees": [
            "You are charged only for an audit that produced a result. A check "
            "that could not run returns HTTP 502, is never settled, and is "
            "never reported as a pass -- x402 payments are verified to grant "
            "access but only settled after the audit has actually delivered.",
            "Rate-limited requests are rejected before any payment is settled, "
            "so a 429 never costs you anything.",
            "Results are deterministic rule-based checks against the live page, "
            "never an LLM's opinion.",
        ],
        "endpoints": [
            {
                "path": entry["path"],
                "method": "POST",
                "payment_required": True,
                "price_usd": entry["price_usd"],
                "input": entry["input"],
                "returns": entry["returns"],
                "description": entry["description"],
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": live_methods,
                "example_request": {
                    "url": f"{base}{entry['path']}",
                    "method": "POST",
                    "headers": {"Content-Type": "application/json", "X-API-Key": "<your key>"},
                    "body": {"url": "https://example.com"},
                },
            }
            for entry in _CATALOG
        ]
        + [
            {
                "path": "/audit",
                "method": "POST",
                "payment_required": True,
                "price_usd": 0.03,
                "input": _HTML_OR_URL_INPUT_SCHEMA,
                "auth": _AUTH_DESCRIPTION,
                "payment_methods": live_methods,
                "note": "Alias of /audit/wcag, kept for backward compatibility.",
            },
            {
                "path": "/scan/free",
                "method": "POST",
                "payment_required": False,
                "rate_limit": f"{FREE_SCAN_LIMIT_PER_DAY}/day per IP",
                "note": (
                    "Human-facing demo only: WCAG, top-2 issues. Machine "
                    "callers should use /audit/wcag -- this endpoint is "
                    "hard-capped per IP and is not a free tier."
                ),
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
        # Two fetches of the target, not four: one rendered page load feeding
        # both browser-based checks, and one HTTP GET feeding both
        # response-based checks. See _run_axe_and_performance.
        wcag_raw, performance_result = _run_axe_and_performance(payload.url)
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
        shared_response = audits.fetch_once(payload.url)
        seo_result = audits.run_seo_audit(None, payload.url, response=shared_response)
        security_result = audits.run_security_audit(payload.url, response=shared_response)
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
