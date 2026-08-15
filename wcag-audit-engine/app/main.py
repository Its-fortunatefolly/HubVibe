import json
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from axe_playwright_python.sync_playwright import Axe
from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
    plan: Optional[str] = None


class ReportCheckoutRequest(BaseModel):
    email: str
    url: str


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


def _bazaar_extension_for_path(path: Optional[str]) -> dict:
    """Bazaar discovery data for the route this 402 is answering for.

    Reuses the same JSON Schemas the MCP tools advertise rather than writing
    a second copy: a discovery index that describes a different input shape
    than the route accepts sends agents to a call that 400s.

    Returns {} for an unknown path or when x402 is not configured, so this
    can be spliced into any 402 unconditionally.
    """
    if not path:
        return {}
    entry = next((e for e in _CATALOG if e["path"] == path), None)
    if entry is None:
        return {}
    schema = (
        _MCP_URL_SCHEMA if entry["input"] is _URL_INPUT_SCHEMA else _MCP_HTML_OR_URL_SCHEMA
    )
    return x402_payments.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema=schema,
        output_example={"pass": True},
    )


def _payment_required_response(
    host: Optional[str] = None, price_usd: float = 0.03, path: Optional[str] = None
) -> JSONResponse:
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
            "detail": (
                "Key issued with a human plan, priced per site watched. "
                "For machine volume, pay per call with a rail in `accepts`."
            ),
            "get_one": f"{PUBLIC_BASE_URL}/billing/checkout",
        },
        "docs": f"{PUBLIC_BASE_URL}/.well-known/agent.json",
    }
    # Keep x402's standard top-level keys alongside `accepts` when x402 is
    # live, so off-the-shelf x402 clients that read the canonical shape keep
    # working unchanged.
    body.update(x402_payments.payment_required_body(price=price))

    # Bazaar discovery. Facilitators catalog x402 resources by reading this
    # off their 402s, and agents shop that index by capability -- without it
    # this endpoint is findable only by someone who already has the URL.
    bazaar = _bazaar_extension_for_path(path)
    if bazaar:
        body["extensions"] = bazaar

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
    path: Optional[str] = None,
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
            if record is not None and billing.check_and_increment_quota(
                record["customer_id"], plan=record.get("plan")
            ):
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

    return _payment_required_response(host=host, price_usd=price_usd, path=path)


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

    auth = _authenticate(
        x_api_key,
        x_payment,
        authorization,
        host=_mpp_realm(request),
        price_usd=price_usd,
        path=request.url.path,
    )
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


@app.get("/mcp.json", tags=["discovery"])
def mcp_manifest():
    """The MCP tool manifest, with prices and rails taken from live config.

    The tool names, descriptions and input schemas come from the static file
    -- they are documentation and change with the product, not with the
    deployment. Two things do NOT come from it, because they are deployment
    state and the file cannot know them:

    `auth.methods`, because the static file asserted x402 unconditionally. It
    went on asserting it after x402 was switched off, so an agent reading this
    manifest -- which is what the MCP registry points at -- would construct a
    payment for a rail this deployment cannot settle. That is the one thing
    this codebase refuses to do everywhere else: /.well-known/agent.json and
    every 402 already omit rails that cannot settle. This route was the hole
    in that rule.

    Per-tool prices, because _CATALOG exists precisely so the manifest, the
    price an agent reads, and the price the route actually charges cannot
    drift apart -- and a second hand-maintained copy of the numbers defeats
    that by construction.
    """
    with open(STATIC_DIR / "mcp.json", encoding="utf-8") as handle:
        manifest = json.load(handle)

    live_methods = _payment_methods_live()
    prices = {entry["path"]: entry["price_usd"] for entry in _CATALOG}

    manifest["auth"]["methods"] = live_methods
    manifest["auth"]["description"] = (
        "Every tool below requires one of the methods in `methods`, which "
        "lists only the rails this deployment can actually settle. An "
        "unauthenticated call returns HTTP 402 with the price and payment "
        "challenge, not an error."
    )

    for tool in manifest.get("tools", []):
        endpoint = tool.get("httpEndpoint") or {}
        path = endpoint.get("path")
        if path in prices:
            endpoint["price_usd"] = prices[path]

    return JSONResponse(content=manifest, media_type="application/json")


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


# Registered at BOTH paths on purpose. Google Cloud Run's frontend
# intercepts /healthz and answers it itself -- a request never reaches this
# container, and the caller gets Google's own HTML 404 page rather than
# anything FastAPI produced. Verified against the live service: the body is
# Google's "Error 404 (Not Found)!!1" page, not FastAPI's
# {"detail":"Not Found"}. No application code can serve /healthz on this
# platform, so /health is the one that actually works here, while /healthz
# is kept for any environment (local, other hosts) that does not reserve it.
@app.get("/health", tags=["discovery"])
@app.get("/healthz", tags=["discovery"])
def health_check():
    return {"status": "ok", "service": "wcag-audit-engine"}


_AUTH_DESCRIPTION = (
    "One of: X-API-Key header (a key issued with a human plan; plans are "
    "priced per site watched, so a machine caller wanting volume should use "
    "a per-call rail below instead); X-PAYMENT header (x402, see 402 response "
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
            "note": (
                "Per-call pricing is the product and is what a machine caller "
                "should use -- no account, no minimum, no subscription."
            ),
            "human_plans": {
                "billed_by": "sites watched, not scans",
                "audience": (
                    "People who want a recurring report rather than an "
                    "integration. Not a cheaper way to buy calls."
                ),
                "checkout": f"{base}/billing/checkout",
                "tiers": [
                    {
                        "id": plan["id"],
                        "name": plan["name"],
                        "usd": plan["usd"],
                        "interval": plan["interval"],
                        "covers": plan["covers"],
                    }
                    for plan in billing.human_plans_live()
                ],
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
            "mcp_endpoint": f"{base}/mcp",
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
        ],
    }


_REPORT_CSS = """
body{margin:0;background:#000;color:#f4ede6;font-family:Inter,-apple-system,
BlinkMacSystemFont,'Segoe UI',sans-serif;line-height:1.6}
.w{max-width:820px;margin:0 auto;padding:56px 28px 80px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 6px}
.sub{color:#8d8d94;font-size:15px;margin:0 0 40px;word-break:break-all}
h2{font-size:13px;font-family:ui-monospace,monospace;letter-spacing:.14em;
text-transform:uppercase;color:#63636a;font-weight:500;margin:38px 0 14px}
.card{border:1px solid #1e1e21;border-radius:10px;padding:22px;margin-bottom:14px}
.verdict{display:flex;justify-content:space-between;align-items:center;gap:16px;
font-weight:600;margin-bottom:14px}
.ok{color:#4ade80}.bad{color:#ff8a2a}
ul{list-style:none;padding:0;margin:0}
li{padding:11px 0;border-top:1px solid #1e1e21;color:#8d8d94;font-size:14.5px}
li b{color:#f4ede6;font-weight:600}
.tag{font-family:ui-monospace,monospace;font-size:11px;color:#ff8a2a;
text-transform:uppercase;letter-spacing:.08em}
.metrics{display:flex;gap:28px;flex-wrap:wrap;color:#8d8d94;font-size:14px}
.metrics b{display:block;color:#f4ede6;font-size:20px;font-weight:700}
footer{margin-top:48px;padding-top:22px;border-top:1px solid #1e1e21;
color:#63636a;font-size:13px}
"""


def _esc(value) -> str:
    """Escape before interpolating into the report.

    Everything in an audit finding originates from a third-party page we were
    asked to audit -- element snippets, header values, URLs. Injecting that
    into HTML unescaped would let an audited site write markup into a report
    its owner is about to read.
    """
    import html as _html

    return _html.escape(str(value), quote=True)


def _verdict(passed: bool) -> str:
    cls, label = ("ok", "PASS") if passed else ("bad", "ATTENTION NEEDED")
    return f'<span class="{cls}">{label}</span>'


def _findings_list(findings: list) -> str:
    if not findings:
        return '<ul><li>No issues found in this check.</li></ul>'
    rows = "".join(
        f'<li><span class="tag">{_esc(f.get("severity", "info"))}</span> '
        f'<b>{_esc(f.get("id", "finding"))}</b><br>{_esc(f.get("detail", ""))}</li>'
        for f in findings
    )
    return f"<ul>{rows}</ul>"


def _render_report(url: str, result: dict) -> str:
    wcag = result.get("wcag", {})
    violations = wcag.get("violations", [])
    wcag_rows = "".join(
        f'<li><span class="tag">{_esc(v.get("impact") or "unknown")}</span> '
        f'<b>{_esc(v.get("id", ""))}</b><br>{_esc(v.get("help", ""))} '
        f'&middot; {_esc(v.get("nodes_affected", 0))} element(s)</li>'
        for v in violations
    ) or "<li>No accessibility violations found in this snapshot.</li>"

    perf = result.get("performance", {})
    m = perf.get("metrics", {})
    seo = result.get("seo", {})
    sec = result.get("security", {})

    overall = all(
        section.get("pass") for section in (wcag, seo, sec, perf) if isinstance(section, dict)
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compliance report — {_esc(url)}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="robots" content="noindex">
<style>{_REPORT_CSS}</style></head><body><div class="w">
<h1>Site compliance report</h1>
<p class="sub">{_esc(url)}</p>

<div class="card"><div class="verdict"><span>Overall</span>{_verdict(overall)}</div>
<p style="color:#8d8d94;margin:0;font-size:14.5px">Four independent checks run against
the live page. Every result below is a deterministic rule, not an opinion.</p></div>

<h2>Accessibility — WCAG 2.1 A/AA</h2>
<div class="card"><div class="verdict"><span>axe-core</span>
{_verdict(bool(wcag.get("pass")))}</div><ul>{wcag_rows}</ul></div>

<h2>SEO</h2>
<div class="card"><div class="verdict"><span>Structure &amp; metadata</span>
{_verdict(bool(seo.get("pass")))}</div>{_findings_list(seo.get("findings", []))}</div>

<h2>Security headers</h2>
<div class="card"><div class="verdict"><span>Response headers</span>
{_verdict(bool(sec.get("pass")))}</div>{_findings_list(sec.get("findings", []))}</div>

<h2>Performance</h2>
<div class="card"><div class="verdict"><span>Single page load</span>
{_verdict(bool(perf.get("pass")))}</div>
<div class="metrics">
<div><b>{_esc(m.get("dom_node_count", "-"))}</b>DOM nodes</div>
<div><b>{_esc(round(m.get("total_bytes_transferred", 0) / 1000))} KB</b>transferred</div>
<div><b>{_esc(m.get("request_count", "-"))}</b>requests</div>
</div>{_findings_list(perf.get("findings", []))}</div>

<footer>Generated by HubVibe. These are narrow, automated checks — a meaningful
share of issues, not all of them. Automated scanning is not a compliance
certification and does not replace a manual accessibility audit.
Bookmark this page to return to the report.</footer>
</div></body></html>"""


def _render_report_error(url: str, detail: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report could not be generated</title>
<style>{_REPORT_CSS}</style></head><body><div class="w">
<h1>We couldn't complete this report</h1>
<p class="sub">{_esc(url)}</p>
<div class="card"><p style="margin:0;color:#8d8d94">The audit could not run against
that URL: {_esc(detail)}</p></div>
<footer>Your purchase still stands and nothing partial was saved — reload this page
to try again once the site is reachable. If it keeps failing, reply to your Stripe
receipt and we'll sort it out.</footer>
</div></body></html>"""


# --- MCP over Streamable HTTP ----------------------------------------------
#
# A real MCP endpoint, not the static /mcp.json manifest. The official MCP
# registry accepts remote servers via `remotes: [{type: "streamable-http"}]`,
# which needs a live endpoint at a public URL -- that is what this is, and it
# is what makes this node listable there without publishing a package.
#
# Implemented directly rather than with the `mcp` SDK on purpose: that package
# requires a newer Starlette than this service pins for FastAPI (which is why
# integrations/mcp_server.py has to be a standalone script). A tools-only MCP
# server over Streamable HTTP is just JSON-RPC 2.0 over POST, so hand-rolling
# the five methods avoids dragging an incompatible dependency into the
# deployed image.
#
# Shapes below were taken from the official SDK's own types rather than from
# memory, and the endpoint was driven with the real SDK client to confirm it.
#
# These are specifically the HANDSHAKE versions. The SDK's newest constant is
# 2026-07-28, but that is a "modern" version negotiated out-of-band and is NOT
# valid to return from initialize -- a client checks the initialize result
# against HANDSHAKE_PROTOCOL_VERSIONS and hard-errors on anything else. Echoing
# the newest constant here made the real client refuse to connect at all, so
# the list below is the handshake set, newest first.
MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

_MCP_URL_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "Live URL to audit"}},
    "required": ["url"],
}
_MCP_HTML_OR_URL_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "Live URL to audit"},
        "html": {"type": "string", "description": "Raw HTML to audit instead of a URL"},
    },
}


def _mcp_tools() -> list:
    """Tool list, derived from the same catalog the REST routes and the agent
    manifest use, so a tool can never advertise a price the route won't
    charge."""
    tools = []
    for entry in _CATALOG:
        name = "audit_" + entry["path"].rsplit("/", 1)[-1]
        tools.append(
            {
                "name": name,
                "description": (
                    f"{entry['description']} ${entry['price_usd']:.2f} per call. "
                    f"Returns: {entry['returns']}"
                ),
                "inputSchema": (
                    _MCP_URL_SCHEMA
                    if entry["input"] is _URL_INPUT_SCHEMA
                    else _MCP_HTML_OR_URL_SCHEMA
                ),
            }
        )
    return tools


_MCP_TOOL_PRICES = {
    "audit_" + entry["path"].rsplit("/", 1)[-1]: entry["price_usd"] for entry in _CATALOG
}


def _mcp_run_tool(name: str, args: dict) -> dict:
    """Execute one audit tool. Assumes payment has already been authorised."""
    url = args.get("url")
    html = args.get("html")

    if name == "audit_wcag":
        raw = _run_axe(html, url)
        violations = raw.get("violations", [])
        return {
            "status": "ok",
            "pass": len(violations) == 0,
            "engine": "axe-core",
            "violations": [
                {
                    "id": v["id"],
                    "impact": v.get("impact"),
                    "help": v.get("help"),
                    "nodes_affected": len(v.get("nodes", [])),
                }
                for v in violations
            ],
        }
    if name == "audit_seo":
        return audits.run_seo_audit(html, url)
    if name == "audit_security":
        return audits.run_security_audit(url)
    if name == "audit_performance":
        return audits.run_performance_audit(url)
    if name == "audit_bundle":
        wcag_raw, performance = _run_axe_and_performance(url)
        violations = wcag_raw.get("violations", [])
        shared = audits.fetch_once(url)
        wcag = {
            "pass": len(violations) == 0,
            "violations": [
                {"id": v["id"], "impact": v.get("impact"), "help": v.get("help")}
                for v in violations
            ],
        }
        seo = audits.run_seo_audit(None, url, response=shared)
        security = audits.run_security_audit(url, response=shared)
        return {
            "status": "ok",
            "pass": all(r["pass"] for r in (wcag, seo, security, performance)),
            "wcag": wcag,
            "seo": seo,
            "security": security,
            "performance": performance,
        }
    raise KeyError(name)


def _jsonrpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _mcp_tool_error(request_id, message: str, details: Optional[dict] = None) -> dict:
    """A tool-level failure is a RESULT with isError, not a JSON-RPC error.

    JSON-RPC errors mean the protocol call itself was malformed; a payment
    requirement or an unreachable audit target is a normal outcome the model
    should see and can act on, so it belongs in the result.

    When there is machine-readable detail -- above all the 402 challenge,
    which carries the price and the rails that can settle it -- the text is
    the JSON itself with the sentence inside it, not a sentence with JSON
    stringified into the middle. An agent should be able to json.loads() the
    content and read `price_usd` and `accepts`, rather than substring-scrape
    a payment challenge out of prose. That prose-embedding is exactly what
    made this endpoint's paywall unusable to the machine buyers it exists
    for.
    """
    if details is None:
        text = message
    else:
        import json as _json

        text = _json.dumps({"message": message, **details}, indent=2)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


@app.post("/mcp", tags=["discovery"])
def mcp_streamable_http(
    payload: dict = Body(...),
    request: Request = None,
    x_api_key: Optional[str] = Header(None),
    x_payment: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """MCP Streamable HTTP endpoint.

    Discovery (initialize, tools/list) is free and unauthenticated -- an agent
    must be able to find out what this node sells and what it costs before
    deciding to buy. Execution (tools/call) goes through exactly the same
    fail-closed authorisation as the REST routes, including verify-then-settle,
    rather than a second copy of the payment logic that could drift from it.
    """
    method = payload.get("method")
    request_id = payload.get("id")

    # Notifications carry no id and must not be answered with a body.
    if request_id is None and isinstance(method, str) and method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        client_version = (payload.get("params") or {}).get("protocolVersion")
        version = (
            client_version if client_version in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hubvibe-site-audit", "version": "1.1.0"},
                "instructions": (
                    "Rule-based site compliance audits. Every tool costs money and "
                    "returns a deterministic result, never an LLM's opinion. Calls "
                    "need an X-API-Key header, or an x402/MPP payment -- see "
                    f"{PUBLIC_BASE_URL}/.well-known/agent.json. A tool that cannot "
                    "run reports an error and is not charged for."
                ),
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _mcp_tools()}}

    if method == "tools/call":
        params = payload.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        price = _MCP_TOOL_PRICES.get(name)
        if price is None:
            return _mcp_tool_error(request_id, f"Unknown tool: {name}")
        if not args.get("url") and not args.get("html"):
            return _mcp_tool_error(request_id, "Provide 'url' (or 'html' for wcag/seo).")

        auth, err = _authorize_and_rate_limit(
            x_api_key, x_payment, authorization, request, price_usd=price
        )
        if err is not None:
            import json as _json

            try:
                challenge = _json.loads(err.body.decode())
            except Exception:
                # Never let a formatting problem turn a payment prompt into a
                # crash: the caller still needs to know what it costs.
                challenge = {"error": "payment_required", "price_usd": price}

            # Index this as an MCP resource in the Bazaar, not as the HTTP
            # route the shared 402 builder described. An agent that finds the
            # tool there calls it over MCP, so the discovery record has to
            # name the tool and its transport.
            tool = next((t for t in _mcp_tools() if t["name"] == name), None)
            if tool is not None:
                mcp_bazaar = x402_payments.bazaar_extension_for_mcp_tool(
                    tool_name=name,
                    description=tool["description"],
                    input_schema=tool["inputSchema"],
                    example={"url": "https://example.com"},
                )
                if mcp_bazaar:
                    challenge["extensions"] = mcp_bazaar

            return _mcp_tool_error(
                request_id,
                f"Payment required (${price:.2f} for {name}). Attach X-API-Key, "
                f"or pay per call with a rail listed in `accepts`.",
                details=challenge,
            )

        try:
            result = _mcp_run_tool(name, args)
        except Exception as exc:
            # Not billed: _bill only runs on success, same as the REST routes.
            return _mcp_tool_error(request_id, f"Audit could not complete: {exc}")

        warning = _bill(auth, units=3 if name == "audit_bundle" else 1)
        if warning:
            result["billing_warning"] = warning

        import json as _json

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": _json.dumps(result, indent=2)}],
                "isError": False,
            },
        }

    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


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
    try:
        checkout_url = billing.create_checkout_session(
            payload.email, success_url, cancel_url, plan=payload.plan
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"checkout_url": checkout_url}


@app.post("/billing/report", tags=["billing"])
def start_report_checkout(payload: ReportCheckoutRequest, request: Request):
    """One-time purchase of a single full-bundle report on one URL."""
    if not billing.oneoff_report_available():
        raise HTTPException(
            status_code=501, detail="One-off reports are not configured on this deployment"
        )
    base = str(request.base_url).rstrip("/")
    success_url = os.environ.get("REPORT_SUCCESS_URL", f"{base}/report")
    cancel_url = os.environ.get("CHECKOUT_CANCEL_URL", f"{base}/billing/cancel")
    try:
        checkout_url = billing.create_report_checkout(
            payload.email, payload.url, success_url, cancel_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"checkout_url": checkout_url}


@app.get("/report", response_class=HTMLResponse, tags=["billing"])
def report_page(session_id: str):
    """Render a purchased report.

    Payment is re-verified against Stripe on every view rather than trusting
    that a webhook landed, because this URL is the only thing between a
    stranger and a free audit -- an unpaid or unknown session gets nothing.

    The audit runs on first view and is then cached, so a refresh re-reads
    the stored result instead of re-running (and re-costing) an audit the
    buyer already paid for exactly once.
    """
    if not billing.is_configured():
        raise HTTPException(status_code=501, detail="Billing is not configured on this deployment")

    order = billing.paid_report_request(session_id)
    if order is None:
        # Deliberately identical for unpaid, unknown, and malformed sessions:
        # no oracle for probing which session IDs exist.
        raise HTTPException(status_code=404, detail="No paid report found for that session")

    cached = billing.load_report(session_id)
    if cached and cached.get("result"):
        return HTMLResponse(_render_report(cached["url"], cached["result"]))

    url = order["url"]
    try:
        wcag_raw, performance_result = _run_axe_and_performance(url)
        wcag_violations = wcag_raw.get("violations", [])
        shared_response = audits.fetch_once(url)
        result = {
            "wcag": {
                "pass": len(wcag_violations) == 0,
                "violations": [
                    {
                        "id": v["id"],
                        "impact": v.get("impact"),
                        "help": v.get("help"),
                        "nodes_affected": len(v.get("nodes", [])),
                    }
                    for v in wcag_violations
                ],
            },
            "seo": audits.run_seo_audit(None, url, response=shared_response),
            "security": audits.run_security_audit(url, response=shared_response),
            "performance": performance_result,
        }
    except Exception as exc:
        # They paid and we could not deliver. Say so plainly and tell them
        # the purchase still stands -- the report is cached only on success,
        # so a retry re-runs rather than serving a broken result forever.
        return HTMLResponse(
            _render_report_error(url, str(exc)),
            status_code=502,
        )

    billing.save_report(session_id, url, result)
    return HTMLResponse(_render_report(url, result))


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
