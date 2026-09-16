"""The machine-service catalog: paid capabilities beside the audits.

The audit suite stays exactly what it was -- five deterministic checks with
their own catalog, routes and tests. This module adds the rest of what an
autonomous agent keeps coming back to buy: LLM inference, web search and
extraction, chain data, market and prediction-market data, media generation,
sandboxed compute, and a composite research job -- each one a paid route
under /svc/*, each one flowing through the SAME payment gate main.py already
runs the audits through. Nothing here touches how money moves: x402 verify
-before-work / settle-after-result, MPP, prepaid keys, replay protection and
the no-charge-on-failure guarantee are inherited from main.py's existing
helpers, not reimplemented.

The catalog rules, inherited from the audits and enforced here too:

- One row per sellable route. Price, description, schemas, MCP tool and
  Bazaar record all derive from the row, so nothing an agent reads can
  drift from what the route charges.
- A capability whose providers are not configured is ABSENT: no route, no
  price, no tool, no manifest entry. Advertising a service that cannot run
  is the same lie as advertising a rail that cannot settle.
- Requests are validated before any payment is read; an invalid request is
  a free 400, not a burned nonce.
- A call that produced no result is never billed; main.py's failure path
  hands back whatever authentication took.

What this module DOES own is reliability and the ledger: per-provider
timeouts, transient-error retries with exponential backoff, provider
fallback, a circuit breaker so a dead vendor stops being tried on every
paid call, an idempotency cache for keyed callers, and a SQLite ledger
recording per-call provider, latency, price and estimated provider cost --
the numbers that say which capabilities actually earn.

Deliberately NOT here: video generation. Veo-class APIs are long-running
operations (minutes, polled), and this service's contract settles payment
only after delivery inside one synchronous call -- holding a worker thread
for minutes starves the audit pool that pays the bills. Adding video
honestly needs an async job rail first; claiming it without one would be
the fabricated integration this repo exists to refuse.
"""

import copy
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from typing import Callable, Optional

try:
    from . import audits, service_providers
except ImportError:
    # Loaded by file path rather than as part of the `app` package (see the
    # matching fallback in main.py). Register under the shared canonical
    # names so every entry point gets ONE copy of each sibling.
    import importlib.util
    import sys
    from pathlib import Path as _Path

    def _load_sibling(name: str):
        unique = f"wcag_audit_engine_{name}"
        cached = sys.modules.get(unique)
        if cached is not None:
            return cached
        spec = importlib.util.spec_from_file_location(
            unique, _Path(__file__).resolve().parent / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        return module

    audits = _load_sibling("audits")
    service_providers = _load_sibling("service_providers")

ProviderError = service_providers.ProviderError

logger = logging.getLogger(__name__)


class ServiceFailure(Exception):
    """Every eligible provider failed. `detail` is safe to put on the 502:
    one line per provider naming what went wrong, so a paying agent can tell
    'your input is unsupported' from 'the vendor is down, retry later'."""

    def __init__(self, service_id: str, failures: list, call_id: Optional[int] = None):
        self.service_id = service_id
        self.failures = list(failures)
        self.call_id = call_id
        detail = "; ".join(self.failures) or "no provider is available"
        super().__init__(detail)
        self.detail = detail


# --------------------------------------------------------------------------
# Reliability primitives.
# --------------------------------------------------------------------------

_BREAKER_THRESHOLD = 3     # consecutive failures before a provider is rested
_BREAKER_COOLDOWN = 60.0   # seconds before it is probed again
_BACKOFF_BASE = 0.4        # first retry delay; doubles per attempt, jittered


class _CircuitBreaker:
    """Per-provider consecutive-failure breaker.

    Purpose: a vendor that is hard-down turns every paid call into its full
    timeout. Skipping it for a cooldown keeps the caller's latency at the
    fallback's speed instead of (dead vendor timeout + fallback). It never
    opens the LAST eligible provider's slot into a refusal by itself --
    eligibility is decided by the engine, which always tries at least one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: dict = {}
        self._opened_until: dict = {}

    def is_open(self, provider: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            until = self._opened_until.get(provider, 0.0)
            if until > now:
                return True
            if until:
                # Cooldown over: half-open. One probe call decides.
                self._opened_until.pop(provider, None)
            return False

    def record_failure(self, provider: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            count = self._failures.get(provider, 0) + 1
            self._failures[provider] = count
            if count >= _BREAKER_THRESHOLD:
                self._opened_until[provider] = now + _BREAKER_COOLDOWN

    def record_success(self, provider: str) -> None:
        with self._lock:
            self._failures.pop(provider, None)
            self._opened_until.pop(provider, None)

    def state(self, provider: str) -> str:
        return "open" if self.is_open(provider) else "closed"


_breaker = _CircuitBreaker()

# Test hook: sleeping between retries is correct in production and pure
# waste in a test. Monkeypatch this, never time.sleep directly.
_sleep = time.sleep


class _IdempotencyCache:
    """Best-effort duplicate suppression for KEYED callers.

    Scope: a caller holding an API key sends X-Idempotency-Key; a retried
    request with the same key returns the first delivery without touching
    payment or providers. Scoped to (api key, path, idempotency key) so one
    caller can never read another's result. Deliberately NOT offered to
    x402 payers: their replay story is the protocol's own nonce ledger, and
    serving cached work to an unauthenticated retry would be a free-result
    oracle. In-memory and per-instance -- documented as best-effort.
    """

    _TTL = 600.0
    _MAX = 512

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict = {}

    @staticmethod
    def _key(api_key: str, path: str, idem_key: str) -> str:
        raw = f"{api_key}\x00{path}\x00{idem_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, api_key: str, path: str, idem_key: str) -> Optional[dict]:
        digest = self._key(api_key, path, idem_key)
        now = time.time()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None or entry[0] < now - self._TTL:
                self._entries.pop(digest, None)
                return None
            result = copy.deepcopy(entry[1])
        result["idempotent_replay"] = True
        return result

    def store(self, api_key: str, path: str, idem_key: str, result: dict) -> None:
        digest = self._key(api_key, path, idem_key)
        with self._lock:
            if len(self._entries) >= self._MAX:
                oldest = sorted(self._entries, key=lambda k: self._entries[k][0])
                for stale in oldest[: self._MAX // 4]:
                    self._entries.pop(stale, None)
            self._entries[digest] = (time.time(), copy.deepcopy(result))


_idempotency = _IdempotencyCache()


def idempotent_replay(api_key: Optional[str], idem_key: Optional[str], path: str) -> Optional[dict]:
    if not api_key or not idem_key or len(idem_key) > 200:
        return None
    return _idempotency.get(api_key, path, idem_key)


def idempotent_store(api_key: Optional[str], idem_key: Optional[str], path: str, result: dict) -> None:
    if not api_key or not idem_key or len(idem_key) > 200:
        return
    try:
        _idempotency.store(api_key, path, idem_key, result)
    except Exception:
        # Duplicate suppression is a convenience; it must never break a
        # delivery that already happened.
        pass


# --------------------------------------------------------------------------
# The usage/margin ledger. SQLite beside the key store: one row per executed
# call with provider, latency, price and the provider-cost estimate, updated
# with the billing outcome after settlement. This is what answers "which
# capability earns" with measurements instead of hopes. Best-effort by
# design: a ledger problem must never fail a paid call.
# --------------------------------------------------------------------------


def _ledger_path() -> Optional[str]:
    configured = os.environ.get("SVC_LEDGER_PATH")
    if configured:
        return configured or None
    keystore = os.environ.get("KEY_STORE_SQLITE_PATH")
    if keystore:
        return os.path.join(os.path.dirname(keystore) or ".", "hubvibe-services.db")
    return "hubvibe-services.db"


_ledger_lock = threading.Lock()
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    service TEXT NOT NULL,
    provider TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    latency_ms INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    est_cost_microusd INTEGER NOT NULL DEFAULT 0,
    payment_method TEXT,
    billed INTEGER NOT NULL DEFAULT 0
)
"""


def _ledger_connect():
    path = _ledger_path()
    if not path:
        return None
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute(_LEDGER_SCHEMA)
    return connection


def _record_call(service: str, provider: Optional[str], ok: bool, error: Optional[str],
                 latency_ms: int, price_cents: int, est_cost_microusd: int) -> Optional[int]:
    try:
        with _ledger_lock:
            connection = _ledger_connect()
            if connection is None:
                return None
            try:
                cursor = connection.execute(
                    "INSERT INTO service_calls "
                    "(ts, service, provider, ok, error, latency_ms, price_cents,"
                    " est_cost_microusd) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), service, provider, 1 if ok else 0,
                     (error or None) and str(error)[:400], latency_ms, price_cents,
                     est_cost_microusd),
                )
                connection.commit()
                return cursor.lastrowid
            finally:
                connection.close()
    except Exception as exc:
        logger.warning("service ledger write failed: %s", exc)
        return None


def finalize_call(call_id: Optional[int], payment_method: Optional[str], billed: bool) -> None:
    """Attach the billing outcome to a recorded call. Called by the route
    after main.py's billing helpers have decided what actually happened."""
    if call_id is None:
        return
    try:
        with _ledger_lock:
            connection = _ledger_connect()
            if connection is None:
                return
            try:
                connection.execute(
                    "UPDATE service_calls SET payment_method = ?, billed = ? WHERE id = ?",
                    (payment_method, 1 if billed else 0, call_id),
                )
                connection.commit()
            finally:
                connection.close()
    except Exception as exc:
        logger.warning("service ledger finalize failed: %s", exc)


def metrics_summary(days: Optional[float] = None) -> dict:
    """Aggregates per service and provider: calls, successes, failures,
    latency, revenue (billed successes x price), estimated provider cost and
    the margin between them. Estimates are labeled estimates."""
    since = time.time() - days * 86400 if days else 0
    per_service: dict = {}
    per_provider: dict = {}
    try:
        with _ledger_lock:
            connection = _ledger_connect()
            if connection is None:
                return {"error": "ledger unavailable"}
            try:
                rows = connection.execute(
                    "SELECT service, provider, ok, latency_ms, price_cents,"
                    " est_cost_microusd, billed FROM service_calls WHERE ts >= ?",
                    (since,),
                ).fetchall()
            finally:
                connection.close()
    except Exception as exc:
        return {"error": f"ledger unavailable: {exc}"}

    for service, provider, ok, latency_ms, price_cents, cost_micro, billed in rows:
        for table, key in ((per_service, service), (per_provider, provider or "none")):
            entry = table.setdefault(key, {
                "calls": 0, "ok": 0, "failed": 0, "latency_ms_total": 0,
                "revenue_cents": 0, "est_cost_microusd": 0,
            })
            entry["calls"] += 1
            entry["ok"] += 1 if ok else 0
            entry["failed"] += 0 if ok else 1
            entry["latency_ms_total"] += latency_ms
            entry["est_cost_microusd"] += cost_micro
            if ok and billed:
                entry["revenue_cents"] += price_cents

    def _finish(table: dict) -> dict:
        out = {}
        for key, entry in sorted(table.items()):
            calls = entry["calls"]
            revenue_usd = entry["revenue_cents"] / 100
            cost_usd = entry["est_cost_microusd"] / 1_000_000
            out[key] = {
                "calls": calls,
                "ok": entry["ok"],
                "failed": entry["failed"],
                "success_rate": round(entry["ok"] / calls, 4) if calls else None,
                "avg_latency_ms": round(entry["latency_ms_total"] / calls) if calls else None,
                "revenue_usd": round(revenue_usd, 4),
                "est_provider_cost_usd": round(cost_usd, 6),
                "est_margin_usd": round(revenue_usd - cost_usd, 4),
            }
        return out

    return {
        "window_days": days,
        "services": _finish(per_service),
        "providers": _finish(per_provider),
        "note": (
            "revenue counts billed successful calls at list price; provider "
            "cost is an estimate from measured usage at published list rates"
        ),
    }


# --------------------------------------------------------------------------
# Validation helpers. Every rule here runs BEFORE payment is read, so a
# refused request is free and says exactly what to fix.
# --------------------------------------------------------------------------


def _need_str(args: dict, field: str, max_len: int, required: bool = True) -> Optional[str]:
    value = args.get(field)
    if value is None or value == "":
        return f"'{field}' is required" if required else None
    if not isinstance(value, str):
        return f"'{field}' must be a string"
    if len(value) > max_len:
        return f"'{field}' is {len(value)} characters; the limit is {max_len}"
    return None


def _need_int(args: dict, field: str, lo: int, hi: int) -> Optional[str]:
    value = args.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return f"'{field}' must be an integer"
    if not lo <= value <= hi:
        return f"'{field}' must be between {lo} and {hi}"
    return None


def _need_url(args: dict, field: str = "url") -> Optional[str]:
    value = args.get(field)
    if not value or not isinstance(value, str):
        return f"'{field}' is required"
    problem = audits.blocked_target_reason(value)
    if problem is not None:
        return f"'{field}' {problem}"
    return None


_PAIR_RE = service_providers._PAIR_RE
_CURRENCY_RE = service_providers._CURRENCY_RE
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,200}$")
_VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _validate_llm(args: dict) -> Optional[str]:
    problem = (
        _need_str(args, "prompt", 12_000)
        or _need_str(args, "system", 2_000, required=False)
        or _need_int(args, "max_tokens", 1, 1024)
    )
    if problem:
        return problem
    temperature = args.get("temperature")
    if temperature is not None:
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            return "'temperature' must be a number"
        if not 0 <= temperature <= 2:
            return "'temperature' must be between 0 and 2"
    provider = args.get("provider")
    live = _llm_provider_ids()
    if provider is not None:
        if provider not in ("anthropic", "gemini", "openai"):
            return "'provider' must be one of: anthropic, gemini, openai"
        if provider not in live:
            return f"provider '{provider}' is not configured on this deployment"
    model = args.get("model")
    if model is not None:
        if not isinstance(model, str):
            return "'model' must be a string"
        allowed = _llm_models_for(provider) if provider else _all_llm_models()
        if model not in allowed:
            return (
                f"model '{model}' is not offered at this price; allowed: "
                + ", ".join(sorted(allowed))
            )
    return None


def _validate_search(args: dict) -> Optional[str]:
    return _need_str(args, "query", 400) or _need_int(args, "count", 1, 10)


def _validate_fetch(args: dict) -> Optional[str]:
    return _need_url(args)


def _validate_rpc(args: dict) -> Optional[str]:
    method = args.get("method")
    if not method or not isinstance(method, str):
        return "'method' is required"
    params = args.get("params")
    if params is not None and not isinstance(params, list):
        return "'params' must be an array"
    try:
        if params is not None and len(json.dumps(params)) > 20_000:
            return "'params' is too large"
    except (TypeError, ValueError):
        return "'params' must be JSON-serialisable"
    return service_providers.check_rpc_request(method, params or [])


def _validate_market(args: dict) -> Optional[str]:
    op = args.get("op")
    if op not in ("spot", "rates", "ticker"):
        return "'op' must be one of: spot, rates, ticker"
    if op in ("spot", "ticker"):
        pair = args.get("pair")
        if not pair or not isinstance(pair, str) or not _PAIR_RE.match(pair):
            return "'pair' must look like BTC-USD"
    if op == "rates":
        currency = args.get("currency")
        if not currency or not isinstance(currency, str) or not _CURRENCY_RE.match(currency):
            return "'currency' must be an asset code like USD or ETH"
    return None


def _validate_prediction(args: dict) -> Optional[str]:
    op = args.get("op")
    if op not in ("markets", "market", "events"):
        return "'op' must be one of: markets, market, events"
    problem = _need_int(args, "limit", 1, 50)
    if problem:
        return problem
    if op == "market":
        slug = args.get("slug")
        if not slug or not isinstance(slug, str) or not _SLUG_RE.match(slug):
            return "'slug' is required for op=market (lowercase letters, digits, hyphens)"
    return None


def _validate_image(args: dict) -> Optional[str]:
    return _need_str(args, "prompt", 2_000)


def _validate_tts(args: dict) -> Optional[str]:
    problem = _need_str(args, "text", 1_500)
    if problem:
        return problem
    voice = args.get("voice")
    if voice is not None and (not isinstance(voice, str) or not _VOICE_RE.match(voice)):
        return "'voice' must be a short provider voice name"
    return None


def _validate_code(args: dict) -> Optional[str]:
    problem = (
        _need_str(args, "code", 20_000)
        or _need_str(args, "stdin", 10_000, required=False)
        or _need_int(args, "timeout_seconds", 1, 10)
    )
    if problem:
        return problem
    language = args.get("language")
    if language is not None and language != "python":
        return "'language' must be 'python' (the only sandbox this node runs)"
    return None


def _validate_research(args: dict) -> Optional[str]:
    return (
        _need_str(args, "query", 400)
        or _need_str(args, "instructions", 2_000, required=False)
        or _need_int(args, "max_sources", 1, 4)
    )


# --------------------------------------------------------------------------
# Provider wiring per capability.
# --------------------------------------------------------------------------


def _llm_provider_ids() -> list:
    order = [
        p.strip()
        for p in os.environ.get("SVC_LLM_PROVIDER_ORDER", "anthropic,gemini,openai").split(",")
        if p.strip()
    ]
    keys = {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "gemini": os.environ.get("GEMINI_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
    }
    return [p for p in order if keys.get(p)]


def _llm_models_for(provider: str) -> tuple:
    return {
        "anthropic": service_providers.anthropic_models(),
        "gemini": service_providers.gemini_models(),
        "openai": service_providers.openai_models(),
    }.get(provider, ())


def _all_llm_models() -> tuple:
    models: tuple = ()
    for provider in ("anthropic", "gemini", "openai"):
        models += _llm_models_for(provider)
    return models


def _llm_chain(args: dict) -> list:
    calls = {
        "anthropic": service_providers.call_anthropic,
        "gemini": service_providers.call_gemini,
        "openai": service_providers.call_openai,
    }
    requested = args.get("provider")
    model = args.get("model")
    ids = [requested] if requested else _llm_provider_ids()
    if model and not requested:
        # A model names its provider implicitly; routing it anywhere else
        # would silently answer with a different model than was asked for.
        ids = [p for p in ids if model in _llm_models_for(p)]
    return [
        _Provider(f"llm:{p}", calls[p], timeout=40.0, attempts=2)
        for p in ids
        if p in calls
    ]


def _search_provider_ids() -> list:
    ids = []
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        ids.append("brave")
    if os.environ.get("SERPER_API_KEY"):
        ids.append("serper")
    return ids


def _search_chain(args: dict) -> list:
    chain = []
    if os.environ.get("BRAVE_SEARCH_API_KEY"):
        chain.append(_Provider("search:brave", service_providers.call_brave_search,
                               timeout=12.0, attempts=2))
    if os.environ.get("SERPER_API_KEY"):
        chain.append(_Provider("search:serper", service_providers.call_serper_search,
                               timeout=12.0, attempts=2))
    return chain


def _rpc_chain(args: dict) -> list:
    chain = []
    for upstream in service_providers.rpc_upstreams():
        host = upstream.split("//", 1)[-1].split("/", 1)[0]

        def _call(call_args, timeout, _upstream=upstream):
            return service_providers.call_chain_rpc(call_args, timeout, _upstream)

        chain.append(_Provider(f"rpc:{host}", _call, timeout=12.0, attempts=2))
    return chain


def _market_chain(args: dict) -> list:
    chain = [_Provider("market:coinbase", service_providers.call_coinbase_market,
                       timeout=10.0, attempts=2)]
    if args.get("op") == "spot":
        chain.append(_Provider(
            "market:coinbase-exchange",
            service_providers.call_coinbase_exchange_spot_fallback,
            timeout=10.0, attempts=2,
        ))
    return chain


def _prediction_chain(args: dict) -> list:
    return [_Provider("prediction:polymarket", service_providers.call_polymarket,
                      timeout=12.0, attempts=2)]


def _fetch_chain(args: dict) -> list:
    return [_Provider("self:fetch", service_providers.call_web_fetch,
                      timeout=20.0, attempts=1)]


def _extract_chain(args: dict) -> list:
    return [_Provider("self:extract", service_providers.call_web_extract,
                      timeout=20.0, attempts=1)]


def _image_chain(args: dict) -> list:
    # attempts=1 for generative media: a retry re-bills the vendor at the
    # dearest per-call cost in the catalog while the caller's price is
    # fixed. The fallback provider is the retry.
    chain = []
    if os.environ.get("GEMINI_API_KEY"):
        chain.append(_Provider("image:gemini", service_providers.call_gemini_image,
                               timeout=75.0, attempts=1))
    if os.environ.get("OPENAI_API_KEY"):
        chain.append(_Provider("image:openai", service_providers.call_openai_image,
                               timeout=75.0, attempts=1))
    return chain


def _tts_chain(args: dict) -> list:
    chain = []
    if os.environ.get("OPENAI_API_KEY"):
        chain.append(_Provider("tts:openai", service_providers.call_openai_tts,
                               timeout=45.0, attempts=1))
    if os.environ.get("GEMINI_API_KEY"):
        chain.append(_Provider("tts:gemini", service_providers.call_gemini_tts,
                               timeout=45.0, attempts=1))
    return chain


def _code_chain(args: dict) -> list:
    return [_Provider("sandbox:local", service_providers.run_code_sandboxed,
                      timeout=15.0, attempts=1)]


class _Provider:
    __slots__ = ("name", "call", "timeout", "attempts")

    def __init__(self, name: str, call: Callable, timeout: float, attempts: int):
        self.name = name
        self.call = call
        self.timeout = timeout
        self.attempts = attempts


# --------------------------------------------------------------------------
# The engine: run one service call through its provider chain.
# --------------------------------------------------------------------------


def _run_chain(spec: dict, args: dict, deadline: float):
    """Try providers in order; within one provider retry transient failures
    with exponential backoff; honor the circuit breaker and the request
    deadline. Returns (result, provider_name, est_cost_microusd) or raises
    ServiceFailure carrying one line per thing that went wrong."""
    chain = spec["chain"](args)
    failures = []
    if not chain:
        raise ServiceFailure(spec["id"], ["no provider is configured for this capability"])

    tripped = [p for p in chain if _breaker.is_open(p.name)]
    live = [p for p in chain if p not in tripped]
    if not live:
        # Every provider is resting after consecutive failures. Trying the
        # first anyway beats refusing outright -- the breaker exists to cut
        # latency, never to manufacture an outage on its own.
        live = chain[:1]
        tripped = [p for p in tripped if p is not chain[0]]
    failures.extend(
        f"{p.name}: skipped (circuit open after repeated failures)" for p in tripped
    )

    for provider in live:
        for attempt in range(provider.attempts):
            remaining = deadline - time.time()
            if remaining <= 0.5:
                failures.append("request deadline exhausted")
                raise ServiceFailure(spec["id"], failures)
            try:
                result = provider.call(args, min(provider.timeout, remaining))
            except ProviderError as exc:
                failures.append(f"{provider.name}: {exc.reason}")
                _breaker.record_failure(provider.name)
                if exc.transient and attempt + 1 < provider.attempts:
                    _sleep(min(_BACKOFF_BASE * (2 ** attempt) * random.uniform(0.75, 1.25),
                               max(deadline - time.time(), 0)))
                    continue
                break  # permanent for this provider, or out of attempts: next provider
            except Exception as exc:  # an adapter bug is a provider failure, not a 500
                logger.exception("service %s provider %s crashed", spec["id"], provider.name)
                failures.append(f"{provider.name}: internal error ({type(exc).__name__})")
                _breaker.record_failure(provider.name)
                break
            problem = spec["check_result"](result) if spec.get("check_result") else None
            if problem:
                failures.append(f"{provider.name}: invalid response ({problem})")
                _breaker.record_failure(provider.name)
                break
            _breaker.record_success(provider.name)
            cost = int(result.pop("cost_microusd", 0) or 0)
            return result, provider.name, cost
    raise ServiceFailure(spec["id"], failures)


def _require_keys(*keys: str) -> Callable:
    def _check(result: dict) -> Optional[str]:
        missing = [k for k in keys if k not in result]
        return f"missing {', '.join(missing)}" if missing else None
    return _check


def _run_research(args: dict, deadline: float):
    """The composite job: search -> extract -> synthesize, sold as one
    completed piece of work. Partial source failures are tolerated and
    disclosed; no sources at all, or no synthesis, is a failure that bills
    nothing -- an empty 'research report' is not a smaller product."""
    query = args["query"]
    max_sources = args.get("max_sources") or 3
    failures = []

    search_result, search_provider, search_cost = _run_chain(
        _SPEC_BY_ID["search"], {"query": query, "count": 6}, deadline
    )
    results = search_result.get("results") or []
    if not results:
        raise ServiceFailure("research", [f"{search_provider}: search returned no results"])

    sources = []
    seen_hosts = set()
    for item in results:
        if len(sources) >= max_sources:
            break
        url = item.get("url") or ""
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if not url or host in seen_hosts:
            continue
        if audits.blocked_target_reason(url) is not None:
            continue
        seen_hosts.add(host)
        if deadline - time.time() <= 10:
            failures.append("deadline reached before all sources were read")
            break
        try:
            extracted = service_providers.call_web_extract({"url": url}, timeout=15.0)
        except ProviderError as exc:
            failures.append(f"source {host}: {exc.reason}")
            continue
        sources.append({
            "url": url,
            "title": extracted.get("title") or item.get("title") or url,
            "text": (extracted.get("text") or "")[:6_000],
        })
    if not sources:
        raise ServiceFailure(
            "research",
            failures + ["no search result could be fetched and extracted"],
        )

    numbered = "\n\n".join(
        f"[{i + 1}] {s['title']} ({s['url']})\n{s['text']}" for i, s in enumerate(sources)
    )
    instructions = args.get("instructions") or (
        "Write a concise, factual research summary answering the query."
    )
    prompt = (
        f"Query: {query}\n\n{instructions}\n"
        "Use ONLY the numbered sources below. Cite them inline as [1], [2] "
        "after each claim they support. If the sources do not answer part of "
        "the query, say so explicitly instead of guessing.\n\n"
        f"Sources:\n\n{numbered}"
    )
    llm_result, llm_provider, llm_cost = _run_chain(
        _SPEC_BY_ID["llm"], {"prompt": prompt, "max_tokens": 1024}, deadline
    )
    answer = (llm_result.get("text") or "").strip()
    if not answer:
        raise ServiceFailure("research", [f"{llm_provider}: synthesis returned no text"])

    result = {
        "query": query,
        "answer": answer,
        "sources": [{"url": s["url"], "title": s["title"]} for s in sources],
        "model": llm_result.get("model"),
        "providers_used": {"search": search_provider, "llm": llm_provider},
    }
    if failures:
        result["partial"] = failures
    return result, "composite", search_cost + llm_cost


# --------------------------------------------------------------------------
# Availability. A capability is advertised iff it could actually run right
# now -- same rule as the payment rails, one level up.
# --------------------------------------------------------------------------


def _disabled_ids() -> set:
    raw = os.environ.get("SVC_DISABLED", "")
    ids = {token.strip() for token in raw.split(",") if token.strip()}
    return ids


def _available(spec: dict) -> bool:
    disabled = _disabled_ids()
    if "all" in disabled or spec["id"] in disabled:
        return False
    return spec["is_live"]()


def _always_live() -> bool:
    return True


def _llm_live() -> bool:
    return bool(_llm_provider_ids())


def _search_live() -> bool:
    return bool(_search_provider_ids())


def _image_live() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def _tts_live() -> bool:
    return _image_live()


def _code_live() -> bool:
    return service_providers.code_exec_enabled() and service_providers.sandbox_mode() is not None


def _research_live() -> bool:
    return _search_live() and _llm_live()


# --------------------------------------------------------------------------
# Input/output schemas. Advertised over MCP, in agent.json and in the
# Bazaar record; they must describe exactly what the validators accept.
# --------------------------------------------------------------------------

_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_LLM_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "LLM inference request",
    "properties": {
        "prompt": {"type": "string", "maxLength": 12000, "description": "The user prompt."},
        "system": {"type": "string", "maxLength": 2000, "description": "Optional system prompt."},
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 1024,
                       "description": "Output token cap; the flat price assumes it.",
                       "default": 1024},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "provider": {"type": "string", "enum": ["anthropic", "gemini", "openai"],
                     "description": "Pin one provider; omit to let the node route."},
        "model": {"type": "string",
                  "description": "One of the allowlisted cheap-tier models; omit for the default."},
    },
    "required": ["prompt"],
}
_LLM_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "text": {"type": "string"},
        "model": {"type": "string"},
        "provider": {"type": "string"},
        "finish_reason": {"type": ["string", "null"]},
        "usage": {"type": "object", "properties": {
            "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"}}},
    },
    "required": ["text"],
}

_SEARCH_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Web search request",
    "properties": {
        "query": {"type": "string", "maxLength": 400},
        "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
    },
    "required": ["query"],
}
_SEARCH_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "status": {"type": "string", "const": "ok"},
        "query": {"type": "string"},
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string", "format": "uri"},
            "snippet": {"type": "string"}}, "required": ["url"]}},
        "provider": {"type": "string"},
    },
    "required": ["results"],
}

_URL_ONLY_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "URL to fetch",
    "properties": {"url": {"type": "string", "format": "uri",
                           "description": "Public http(s) URL; private/internal targets are refused."}},
    "required": ["url"],
}
_FETCH_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "status_field": {"type": "string"},
        "final_url": {"type": "string"}, "status": {"type": "integer"},
        "content_type": {"type": ["string", "null"]}, "bytes": {"type": "integer"},
        "text": {"type": ["string", "null"]}, "truncated": {"type": "boolean"},
        "headers": {"type": "object"},
    },
    "required": ["status", "final_url"],
}
_EXTRACT_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]}, "description": {"type": ["string", "null"]},
        "lang": {"type": ["string", "null"]}, "text": {"type": "string"},
        "word_count": {"type": "integer"}, "truncated": {"type": "boolean"},
        "links": {"type": "array", "items": {"type": "object", "properties": {
            "href": {"type": "string"}, "text": {"type": "string"}}}},
    },
    "required": ["text"],
}

_RPC_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Read-only JSON-RPC call",
    "properties": {
        "method": {"type": "string", "enum": sorted(service_providers.RPC_ALLOWED_METHODS)},
        "params": {"type": "array", "description": "Positional params, exactly as JSON-RPC takes them."},
    },
    "required": ["method"],
}
_RPC_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "network": {"type": "string"}, "method": {"type": "string"},
        "result": {"description": "The chain's answer, verbatim."},
        "error": {"description": "The chain's JSON-RPC error object, verbatim, when it answered with one."},
        "provider": {"type": "string"},
    },
    "required": ["network", "method"],
}

_MARKET_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Market data request",
    "properties": {
        "op": {"type": "string", "enum": ["spot", "rates", "ticker"]},
        "pair": {"type": "string", "pattern": "^[A-Z0-9]{2,12}-[A-Z0-9]{2,12}$",
                 "description": "Trading pair for spot/ticker, e.g. BTC-USD."},
        "currency": {"type": "string", "pattern": "^[A-Z0-9]{2,12}$",
                     "description": "Base asset for rates, e.g. ETH."},
    },
    "required": ["op"],
}
_MARKET_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "op": {"type": "string"}, "pair": {"type": "string"}, "amount": {"type": "string"},
        "price": {"type": "string"}, "rates": {"type": "object"}, "source": {"type": "string"},
    },
    "required": ["op"],
}

_PREDICTION_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Prediction-market data request",
    "properties": {
        "op": {"type": "string", "enum": ["markets", "market", "events"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "active": {"type": "boolean", "default": True},
        "closed": {"type": "boolean", "default": False},
        "slug": {"type": "string", "pattern": "^[a-z0-9-]{1,200}$",
                 "description": "Market slug for op=market."},
    },
    "required": ["op"],
}
_PREDICTION_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "op": {"type": "string"},
        "markets": {"type": "array", "items": {"type": "object"}},
        "market": {"type": "object"},
        "events": {"type": "array", "items": {"type": "object"}},
        "source": {"type": "string"},
    },
    "required": ["op"],
}

_IMAGE_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Image generation request",
    "properties": {"prompt": {"type": "string", "maxLength": 2000}},
    "required": ["prompt"],
}
_IMAGE_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "image_base64": {"type": "string", "description": "The image, base64-encoded."},
        "mime_type": {"type": "string"}, "model": {"type": "string"},
        "provider": {"type": "string"},
    },
    "required": ["image_base64", "mime_type"],
}

_TTS_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Text-to-speech request",
    "properties": {
        "text": {"type": "string", "maxLength": 1500},
        "voice": {"type": "string", "maxLength": 40,
                  "description": "Provider voice name; omit for the default."},
    },
    "required": ["text"],
}
_TTS_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "audio_base64": {"type": "string"}, "mime_type": {"type": "string"},
        "voice": {"type": "string"}, "model": {"type": "string"}, "provider": {"type": "string"},
    },
    "required": ["audio_base64", "mime_type"],
}

_CODE_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Sandboxed code execution request",
    "properties": {
        "code": {"type": "string", "maxLength": 20000, "description": "Python source to run."},
        "language": {"type": "string", "enum": ["python"], "default": "python"},
        "stdin": {"type": "string", "maxLength": 10000},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
    },
    "required": ["code"],
}
_CODE_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "stdout": {"type": "string"}, "stderr": {"type": "string"},
        "exit_code": {"type": ["integer", "null"]}, "timed_out": {"type": "boolean"},
        "truncated": {"type": "boolean"}, "sandbox": {"type": "string"},
    },
    "required": ["stdout", "stderr"],
}

_RESEARCH_INPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object", "title": "Research job request",
    "properties": {
        "query": {"type": "string", "maxLength": 400},
        "instructions": {"type": "string", "maxLength": 2000,
                         "description": "Optional steering for the synthesis."},
        "max_sources": {"type": "integer", "minimum": 1, "maximum": 4, "default": 3},
    },
    "required": ["query"],
}
_RESEARCH_OUTPUT_SCHEMA = {
    "$schema": _SCHEMA_DRAFT, "type": "object",
    "properties": {
        "query": {"type": "string"},
        "answer": {"type": "string", "description": "Cited synthesis, [n] refers to sources[n-1]."},
        "sources": {"type": "array", "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "title": {"type": "string"}}}},
        "model": {"type": "string"},
        "partial": {"type": "array", "items": {"type": "string"},
                    "description": "Present when some sources could not be read."},
    },
    "required": ["answer", "sources"],
}


# --------------------------------------------------------------------------
# The catalog itself. One row per sellable service; every advertised
# surface derives from these rows.
#
# Prices are flat per call, two-decimal USD -- the shape the existing 402
# plumbing settles -- with the input caps above keeping the worst-case
# provider cost under the price. The margin ledger verifies that claim with
# measurements instead of leaving it asserted.
# --------------------------------------------------------------------------

_READ_ANNOTATIONS = {
    "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True,
}
_GENERATIVE_ANNOTATIONS = {
    "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
}
_CODE_ANNOTATIONS = {
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
}

CATALOG = [
    {
        "id": "llm", "path": "/svc/llm", "price_usd": 0.02,
        "mcp_name": "llm_generate", "title": "LLM inference (Claude / Gemini / OpenAI)",
        "category": "ai",
        "description": (
            "One LLM completion from a configured provider -- Anthropic Claude, "
            "Google Gemini, or OpenAI -- routed to whichever is available, with "
            "automatic failover. Cheap-tier models only at this flat price; "
            "output capped at 1024 tokens, prompt at 12000 characters. Returns "
            "the text, the exact model that ran, and measured token usage."
        ),
        "returns": "text, model, provider, finish_reason, usage{input_tokens,output_tokens}.",
        "input_schema": _LLM_INPUT_SCHEMA, "output_schema": _LLM_OUTPUT_SCHEMA,
        "input_example": {"prompt": "Summarize what HTTP 402 is for in two sentences."},
        "output_example": {"text": "...", "model": "claude-haiku-4-5", "provider": "llm:anthropic"},
        "annotations": _GENERATIVE_ANNOTATIONS,
        "validate": _validate_llm, "chain": _llm_chain, "is_live": _llm_live,
        "check_result": _require_keys("text", "model"),
        "deadline_seconds": 50, "deliver_note": None,
    },
    {
        "id": "search", "path": "/svc/search", "price_usd": 0.02,
        "mcp_name": "web_search", "title": "Web search",
        "category": "research",
        "description": (
            "Live web search with ranked results -- title, URL, snippet -- from "
            "a configured search provider (Brave or Serper) with automatic "
            "failover. Up to 10 results per call."
        ),
        "returns": "results[] with title/url/snippet, provider.",
        "input_schema": _SEARCH_INPUT_SCHEMA, "output_schema": _SEARCH_OUTPUT_SCHEMA,
        "input_example": {"query": "x402 payment protocol", "count": 5},
        "output_example": {"results": [{"title": "...", "url": "https://...", "snippet": "..."}]},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_search, "chain": _search_chain, "is_live": _search_live,
        "check_result": _require_keys("results"),
        "deadline_seconds": 30, "deliver_note": None,
    },
    {
        "id": "fetch", "path": "/svc/fetch", "price_usd": 0.01,
        "mcp_name": "web_fetch", "title": "Web fetch",
        "category": "research",
        "description": (
            "Fetch one public URL from this node and return the response: "
            "status, headers, content type, and up to 500 KB of body text for "
            "textual content. Redirects are followed with every hop re-checked "
            "against the same private-address gate the audits use."
        ),
        "returns": "status, final_url, headers{}, content_type, text, bytes, truncated.",
        "input_schema": _URL_ONLY_SCHEMA, "output_schema": _FETCH_OUTPUT_SCHEMA,
        "input_example": {"url": "https://example.com"},
        "output_example": {"status": 200, "final_url": "https://example.com/", "text": "..."},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_fetch, "chain": _fetch_chain, "is_live": _always_live,
        "check_result": _require_keys("status", "final_url"),
        "deadline_seconds": 30, "deliver_note": None,
    },
    {
        "id": "extract", "path": "/svc/extract", "price_usd": 0.01,
        "mcp_name": "web_extract", "title": "Web content extraction",
        "category": "research",
        "description": (
            "Fetch one public HTML page and return its readable content as "
            "structured data: title, meta description, language, canonical "
            "URL, plain text with scripts and styles stripped, word count and "
            "outbound links. Raw parsing of the served page, no browser and no "
            "LLM in the loop."
        ),
        "returns": "title, description, lang, text, word_count, links[], truncated.",
        "input_schema": _URL_ONLY_SCHEMA, "output_schema": _EXTRACT_OUTPUT_SCHEMA,
        "input_example": {"url": "https://example.com"},
        "output_example": {"title": "Example Domain", "text": "...", "word_count": 28},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_fetch, "chain": _extract_chain, "is_live": _always_live,
        "check_result": _require_keys("text"),
        "deadline_seconds": 30, "deliver_note": None,
    },
    {
        "id": "rpc", "path": "/svc/rpc", "price_usd": 0.01,
        "mcp_name": "chain_rpc", "title": "Blockchain RPC (Base, read-only)",
        "category": "blockchain",
        "description": (
            "Read-only JSON-RPC against Base mainnet with upstream failover: "
            "balances, blocks, transactions, receipts, logs, eth_call and gas "
            "queries. The method allowlist is in the input schema; transaction "
            "broadcasting is deliberately not offered. A JSON-RPC error object "
            "from the chain (an eth_call revert, say) is the chain's answer and "
            "is returned as the result."
        ),
        "returns": "network, method, result (or the chain's error object), provider.",
        "input_schema": _RPC_INPUT_SCHEMA, "output_schema": _RPC_OUTPUT_SCHEMA,
        "input_example": {"method": "eth_blockNumber", "params": []},
        "output_example": {"network": "base", "method": "eth_blockNumber", "result": "0x30f7edc"},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_rpc, "chain": _rpc_chain, "is_live": _always_live,
        "check_result": _require_keys("network", "method"),
        "deadline_seconds": 30, "deliver_note": None,
    },
    {
        "id": "market", "path": "/svc/market", "price_usd": 0.01,
        "mcp_name": "market_data", "title": "Market data (crypto/fiat)",
        "category": "financial",
        "description": (
            "Current market data from Coinbase's public data APIs: spot price "
            "for any listed pair, full exchange-rate table for one asset, or "
            "the exchange ticker (price, bid, ask, 24h volume). Live quotes, "
            "not advice, and not a licensed market-data feed."
        ),
        "returns": "op-dependent: amount/currency, rates{}, or price/bid/ask/volume.",
        "input_schema": _MARKET_INPUT_SCHEMA, "output_schema": _MARKET_OUTPUT_SCHEMA,
        "input_example": {"op": "spot", "pair": "ETH-USD"},
        "output_example": {"op": "spot", "pair": "ETH-USD", "amount": "4321.00", "currency": "USD"},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_market, "chain": _market_chain, "is_live": _always_live,
        "check_result": _require_keys("op"),
        "deadline_seconds": 25, "deliver_note": None,
    },
    {
        "id": "prediction", "path": "/svc/prediction", "price_usd": 0.01,
        "mcp_name": "prediction_markets", "title": "Prediction-market data",
        "category": "financial",
        "description": (
            "Live prediction-market data from Polymarket's public Gamma API: "
            "top markets by volume, one market by slug with outcome prices, or "
            "top events. Read-only market intelligence; this node places no "
            "orders and holds no positions."
        ),
        "returns": "markets[]/market/events[] with question, outcomes, prices, volume.",
        "input_schema": _PREDICTION_INPUT_SCHEMA, "output_schema": _PREDICTION_OUTPUT_SCHEMA,
        "input_example": {"op": "markets", "limit": 5},
        "output_example": {"op": "markets", "markets": [{"question": "...", "outcome_prices": ["0.62", "0.38"]}]},
        "annotations": _READ_ANNOTATIONS,
        "validate": _validate_prediction, "chain": _prediction_chain, "is_live": _always_live,
        "check_result": _require_keys("op"),
        "deadline_seconds": 25, "deliver_note": None,
    },
    {
        "id": "image", "path": "/svc/image", "price_usd": 0.10,
        "mcp_name": "image_generate", "title": "Image generation",
        "category": "media",
        "description": (
            "Generate one image from a text prompt using a configured model "
            "(Gemini image generation or OpenAI gpt-image-1), with failover. "
            "Returns the image base64-encoded in the response, which is the "
            "only delivery channel a per-call payer has."
        ),
        "returns": "image_base64, mime_type, model, provider.",
        "input_schema": _IMAGE_INPUT_SCHEMA, "output_schema": _IMAGE_OUTPUT_SCHEMA,
        "input_example": {"prompt": "Isometric illustration of a beehive made of circuit boards"},
        "output_example": {"mime_type": "image/png", "image_base64": "..."},
        "annotations": _GENERATIVE_ANNOTATIONS,
        "validate": _validate_image, "chain": _image_chain, "is_live": _image_live,
        "check_result": _require_keys("image_base64", "mime_type"),
        "deadline_seconds": 90, "deliver_note": None,
    },
    {
        "id": "tts", "path": "/svc/tts", "price_usd": 0.05,
        "mcp_name": "speech_synthesize", "title": "Voice synthesis (text-to-speech)",
        "category": "media",
        "description": (
            "Synthesize speech from up to 1500 characters of text using a "
            "configured voice provider (OpenAI or Gemini TTS), with failover. "
            "Returns playable audio (MP3 or WAV) base64-encoded."
        ),
        "returns": "audio_base64, mime_type, voice, model, provider.",
        "input_schema": _TTS_INPUT_SCHEMA, "output_schema": _TTS_OUTPUT_SCHEMA,
        "input_example": {"text": "Payment received. Your audit is running."},
        "output_example": {"mime_type": "audio/mpeg", "audio_base64": "..."},
        "annotations": _GENERATIVE_ANNOTATIONS,
        "validate": _validate_tts, "chain": _tts_chain, "is_live": _tts_live,
        "check_result": _require_keys("audio_base64", "mime_type"),
        "deadline_seconds": 60, "deliver_note": None,
    },
    {
        "id": "code", "path": "/svc/code", "price_usd": 0.03,
        "mcp_name": "code_execute", "title": "Sandboxed code execution",
        "category": "compute",
        "description": (
            "Run a short Python program in an isolated sandbox: resource "
            "limits, a clean environment, wall-clock kill, and network cut off "
            "where the host supports namespaces (the response says which "
            "isolation ran). Non-zero exits and timeouts are results -- the "
            "sandbox ran; your code did what it did. Offered only on "
            "deployments where the operator has enabled it."
        ),
        "returns": "stdout, stderr, exit_code, timed_out, truncated, sandbox.",
        "input_schema": _CODE_INPUT_SCHEMA, "output_schema": _CODE_OUTPUT_SCHEMA,
        "input_example": {"code": "print(sum(range(10)))"},
        "output_example": {"stdout": "45\n", "stderr": "", "exit_code": 0},
        "annotations": _CODE_ANNOTATIONS,
        "validate": _validate_code, "chain": _code_chain, "is_live": _code_live,
        "check_result": _require_keys("stdout", "stderr"),
        "deadline_seconds": 20, "deliver_note": None,
    },
    {
        "id": "research", "path": "/svc/research", "price_usd": 0.25,
        "mcp_name": "web_research", "title": "Research job (search + read + synthesize)",
        "category": "research",
        "description": (
            "One completed research job: web search, fetch and extract the top "
            "distinct sources, and synthesize a cited answer with an LLM -- "
            "sold as a single call instead of five. Every claim in the answer "
            "cites its numbered source; sources that could not be read are "
            "disclosed, and a job that produces no grounded answer bills "
            "nothing."
        ),
        "returns": "answer (cited), sources[] with url/title, model, providers_used.",
        "input_schema": _RESEARCH_INPUT_SCHEMA, "output_schema": _RESEARCH_OUTPUT_SCHEMA,
        "input_example": {"query": "What is the x402 payment protocol and who runs facilitators?"},
        "output_example": {"answer": "... [1] ... [2]", "sources": [{"url": "https://...", "title": "..."}]},
        "annotations": _GENERATIVE_ANNOTATIONS,
        "validate": _validate_research, "chain": None, "is_live": _research_live,
        "check_result": None,
        "deadline_seconds": 150, "deliver_note": None,
    },
]

_SPEC_BY_ID = {spec["id"]: spec for spec in CATALOG}
_SPEC_BY_PATH = {spec["path"]: spec for spec in CATALOG}
_SPEC_BY_MCP_NAME = {spec["mcp_name"]: spec for spec in CATALOG}


# --------------------------------------------------------------------------
# Public surface consumed by main.py. Everything answers from the LIVE
# catalog, so a capability that lost its configuration disappears from
# every advertised surface at once.
# --------------------------------------------------------------------------


def catalog() -> list:
    """The live catalog: services that could actually run right now."""
    return [spec for spec in CATALOG if _available(spec)]


def price_of_path(path: Optional[str]) -> Optional[float]:
    if not path:
        return None
    spec = _SPEC_BY_PATH.get(path)
    if spec is None or not _available(spec):
        return None
    return spec["price_usd"]


def route_description(path: Optional[str]) -> Optional[str]:
    spec = _SPEC_BY_PATH.get(path) if path else None
    if spec is None or not _available(spec):
        return None
    return spec["description"]


def price_cents_list() -> list:
    return [round(spec["price_usd"] * 100) for spec in catalog()]


def discovery_entries() -> list:
    """Rows for the OpenAPI x-payment-info annotator, shaped like _CATALOG
    rows plus each service's own request example."""
    return [
        {
            "path": spec["path"],
            "price_usd": spec["price_usd"],
            "description": spec["description"],
            "input_example": spec["input_example"],
        }
        for spec in catalog()
    ]


def schema_for_path(path: str):
    """(input_schema, input_example, output_example) for the Bazaar record,
    or None when the path is not a live service."""
    spec = _SPEC_BY_PATH.get(path)
    if spec is None or not _available(spec):
        return None
    return spec["input_schema"], spec["input_example"], spec["output_example"]


def is_service_tool(name: Optional[str]) -> bool:
    if not name:
        return False
    spec = _SPEC_BY_MCP_NAME.get(name)
    return spec is not None and _available(spec)


def validate_tool_args(name: str, args: dict) -> Optional[str]:
    spec = _SPEC_BY_MCP_NAME.get(name)
    if spec is None:
        return f"unknown tool {name}"
    return spec["validate"](args)


def tool_price(name: str) -> Optional[float]:
    spec = _SPEC_BY_MCP_NAME.get(name)
    if spec is None or not _available(spec):
        return None
    return spec["price_usd"]


def mcp_tools() -> list:
    """Live service tools in the same shape main.py's _mcp_tools emits."""
    tools = []
    for spec in catalog():
        tools.append({
            "name": spec["mcp_name"],
            "title": spec["title"],
            "description": (
                f"{spec['description']} ${spec['price_usd']:.2f} per call. "
                f"Returns: {spec['returns']}"
            ),
            "inputSchema": spec["input_schema"],
            "outputSchema": spec["output_schema"],
            "annotations": dict(spec["annotations"], title=spec["title"]),
        })
    return tools


def mcp_tool_prices() -> dict:
    return {spec["mcp_name"]: spec["price_usd"] for spec in catalog()}


def agent_endpoints(base: str, live_methods: list, auth_description: str) -> list:
    """Entries for /.well-known/agent.json, same shape as the audit rows."""
    return [
        {
            "path": spec["path"],
            "method": "POST",
            "payment_required": True,
            "price_usd": spec["price_usd"],
            "input_schema": spec["input_schema"],
            "output_schema": spec["output_schema"],
            "returns": spec["returns"],
            "description": spec["description"],
            "category": spec["category"],
            "auth": auth_description,
            "payment_methods": live_methods,
            "example_request": {
                "url": f"{base}{spec['path']}",
                "method": "POST",
                "headers": {"Content-Type": "application/json", "X-API-Key": "<your key>"},
                "body": spec["input_example"],
            },
        }
        for spec in catalog()
    ]


def validate_route(path: str, args) -> Optional[str]:
    """Why this request must be refused for free, or None when it may be
    paid for and run."""
    spec = _SPEC_BY_PATH.get(path)
    if spec is None:
        return "unknown service"
    if not isinstance(args, dict):
        return "the request body must be a JSON object"
    return spec["validate"](args)


def execute_route(path: str, args: dict):
    """Run one paid service call. Returns (result, call_id); the result
    carries status/service/provider and is ready for _deliver. Raises
    ServiceFailure -- with the per-provider reasons and the ledger row id --
    when nothing could produce a result. Payment is the caller's job, on
    both sides of this call: authorised before, settled after."""
    spec = _SPEC_BY_PATH[path]
    started = time.time()
    deadline = started + spec["deadline_seconds"]
    price_cents = round(spec["price_usd"] * 100)
    try:
        if spec["id"] == "research":
            result, provider, cost = _run_research(args, deadline)
        else:
            result, provider, cost = _run_chain(spec, args, deadline)
    except ServiceFailure as failure:
        latency_ms = int((time.time() - started) * 1000)
        failure.call_id = _record_call(
            spec["id"], None, ok=False, error=failure.detail,
            latency_ms=latency_ms, price_cents=price_cents, est_cost_microusd=0,
        )
        raise
    latency_ms = int((time.time() - started) * 1000)
    call_id = _record_call(
        spec["id"], provider, ok=True, error=None,
        latency_ms=latency_ms, price_cents=price_cents, est_cost_microusd=cost,
    )
    result = {"status": "ok", "service": spec["id"], "provider": provider, **result}
    return result, call_id


def execute_tool(name: str, args: dict):
    return execute_route(_SPEC_BY_MCP_NAME[name]["path"], args)


def tool_call_id_of(name: str) -> Optional[str]:
    spec = _SPEC_BY_MCP_NAME.get(name)
    return spec["path"] if spec else None


def health_snapshot() -> dict:
    """Capability availability and provider circuit state -- names and
    booleans only, never configuration values. Free to read: an agent
    deciding whether to route work here should not have to pay to learn
    the answer."""
    services = {}
    for spec in CATALOG:
        live = _available(spec)
        providers = {}
        if spec["id"] == "research":
            providers = {"composite": {"configured": live}}
        elif spec["chain"] is not None:
            try:
                chain = spec["chain"](spec["input_example"])
            except Exception:
                chain = []
            for provider in chain:
                providers[provider.name] = {
                    "configured": True,
                    "circuit": _breaker.state(provider.name),
                }
        entry = {"available": live, "price_usd": spec["price_usd"], "providers": providers}
        if spec["id"] == "code" and live:
            entry["sandbox"] = service_providers.sandbox_mode()
        services[spec["id"]] = entry
    return {"services": services}
