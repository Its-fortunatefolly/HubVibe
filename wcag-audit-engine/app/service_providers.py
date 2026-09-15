"""Provider adapters for the machine-service catalog in services.py.

Each adapter is one upstream capability -- an LLM API, a search API, a
public JSON-RPC node, a subprocess sandbox -- normalised to the one shape
its service returns, so the service layer (and everything above it: the
402s, the MCP tools, the manifest) never has to know which vendor answered.

Ground rules, inherited from the audit engine these sit beside:

- httpx only. Every adapter is a plain HTTP call with an explicit timeout;
  no vendor SDK gets to bring its own retry policy, thread pool, or pinned
  Starlette into the deployed image (the `mcp` package already forced
  integrations/mcp_server.py out of the service for exactly that reason).
- Secrets come from the environment at call time, never at import: a key
  rotated on a running box is a key the next request uses. Nothing here
  logs, echoes, or returns a credential.
- An adapter either returns a dict that satisfies its service's contract or
  raises ProviderError saying why. It never fabricates a result: the caller
  above turns "no provider could answer" into an unbilled 502, and the one
  thing this file must never do is turn that into something that looks
  like an answer.
- Outbound fetches of CALLER-CHOSEN URLs go through audits.fetch_once,
  which re-checks every redirect hop against the SSRF gate. The fixed
  vendor hosts (api.anthropic.com etc.) are not caller-chosen and do not.
- Costs are tracked, not invented: where an API reports usage (all three
  LLM vendors do) the estimate is usage x the vendor's published list
  price; where it does not, the estimate is the vendor's published
  per-call price, labeled as an estimate. See services.py for how these
  feed the margin ledger.
"""

import base64
import io
import json
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from typing import Optional

import httpx

try:
    from . import audits
except ImportError:
    # Loaded by file path rather than as part of the `app` package -- reuse
    # the exact module main.py/audits.py registered so there is one SSRF
    # gate and one browser pool per process, never a second copy.
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path

    _AUDITS_NAME = "wcag_audit_engine_audits"
    audits = _sys.modules.get(_AUDITS_NAME)
    if audits is None:
        _spec = importlib.util.spec_from_file_location(
            _AUDITS_NAME, _Path(__file__).resolve().parent / "audits.py"
        )
        audits = importlib.util.module_from_spec(_spec)
        _sys.modules[_AUDITS_NAME] = audits
        _spec.loader.exec_module(audits)

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """One provider failed to answer. `transient` says whether retrying the
    SAME provider could plausibly succeed (timeouts, 429s, 5xx) or is a
    waste of a paid caller's time (bad key, unsupported input). The message
    is safe to show the payer: it must name the failure, never a secret."""

    def __init__(self, reason: str, transient: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.transient = transient


def _post_json(url: str, *, headers: dict, body: dict, timeout: float) -> dict:
    """POST JSON, return parsed JSON, raise ProviderError on anything else.

    Status handling is the transient/permanent split every adapter needs:
    408/429/5xx are the upstream having a moment (retryable), any other 4xx
    is this request being wrong for that upstream (not retryable). The
    response body's own error message is passed through -- truncated -- so a
    payer's 502 says what the vendor said, not just "provider failed".
    """
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError("timed out", transient=True)
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport error: {type(exc).__name__}", transient=True)
    return _decode_json_response(response)


def _get_json(url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None,
              timeout: float = 15.0) -> dict:
    try:
        response = httpx.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError("timed out", transient=True)
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport error: {type(exc).__name__}", transient=True)
    return _decode_json_response(response)


def _decode_json_response(response) -> dict:
    if response.status_code in (408, 429) or response.status_code >= 500:
        raise ProviderError(
            f"upstream answered HTTP {response.status_code}: {_short_body(response)}",
            transient=True,
        )
    if response.status_code >= 400:
        raise ProviderError(
            f"upstream refused the request (HTTP {response.status_code}): {_short_body(response)}"
        )
    try:
        parsed = response.json()
    except Exception:
        raise ProviderError("upstream returned a body that is not JSON", transient=True)
    if not isinstance(parsed, (dict, list)):
        raise ProviderError("upstream returned JSON of an unexpected shape", transient=True)
    return parsed


def _short_body(response, limit: int = 220) -> str:
    """The upstream's own words, bounded. Vendor error bodies name the
    problem ('invalid x-api-key', 'model not found') and that is exactly
    what a paying agent needs on its 502 -- but never more than a line."""
    try:
        text = response.text or ""
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:limit]


# --------------------------------------------------------------------------
# LLM inference: Anthropic (Claude), Google (Gemini), OpenAI.
#
# Flat per-call pricing only works when the worst case is bounded, so every
# model that may run at this price is on an allowlist of cheap-tier models
# and the token caps live in services.py's validation. A caller who wants a
# frontier model is asking for a different product at a different price --
# refusing here is what keeps the advertised price honest.
# --------------------------------------------------------------------------

_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash-lite"
_OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def _allowed_models(env_var: str, default_models: tuple) -> tuple:
    raw = os.environ.get(env_var, "")
    extra = tuple(m.strip() for m in raw.split(",") if m.strip())
    return default_models + tuple(m for m in extra if m not in default_models)


def anthropic_models() -> tuple:
    return _allowed_models("SVC_ANTHROPIC_MODELS", (_ANTHROPIC_DEFAULT_MODEL,))


def gemini_models() -> tuple:
    return _allowed_models(
        "SVC_GEMINI_MODELS", (_GEMINI_DEFAULT_MODEL, "gemini-2.5-flash")
    )


def openai_models() -> tuple:
    return _allowed_models("SVC_OPENAI_MODELS", (_OPENAI_DEFAULT_MODEL, "gpt-5-mini"))


# Published list prices, USD per million tokens (input, output), used to turn
# the usage counts the vendors report into a cost estimate for the margin
# ledger. These are estimates from list prices as of 2026-09 -- the usage
# counts are measured, the rates are not -- and unknown models fall back to
# the dearest rate here so a margin is never overstated.
_LLM_RATES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5-mini": (0.25, 2.00),
}
_LLM_FALLBACK_RATE = (1.00, 5.00)


def llm_cost_microusd(model: str, input_tokens: int, output_tokens: int) -> int:
    rate = _LLM_FALLBACK_RATE
    for prefix, pair in _LLM_RATES_PER_MTOK.items():
        if model.startswith(prefix):
            rate = pair
            break
    dollars = (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000
    return int(dollars * 1_000_000)


def call_anthropic(args: dict, timeout: float) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ProviderError("ANTHROPIC_API_KEY is not configured")
    model = args.get("model") or _ANTHROPIC_DEFAULT_MODEL
    body = {
        "model": model,
        "max_tokens": args["max_tokens"],
        "messages": [{"role": "user", "content": args["prompt"]}],
    }
    if args.get("system"):
        body["system"] = args["system"]
    if args.get("temperature") is not None:
        body["temperature"] = args["temperature"]
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        body=body,
        timeout=timeout,
    )
    parts = data.get("content") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text")
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "text": text,
        "model": data.get("model") or model,
        "finish_reason": data.get("stop_reason"),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost_microusd": llm_cost_microusd(model, input_tokens, output_tokens),
    }


def _gemini_generate(model: str, body: dict, timeout: float) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY is not configured")
    # The key rides a header, not the query string, so it can never end up
    # in an access log or a traceback that prints the URL.
    return _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key},
        body=body,
        timeout=timeout,
    )


def call_gemini(args: dict, timeout: float) -> dict:
    model = args.get("model") or _GEMINI_DEFAULT_MODEL
    body = {
        "contents": [{"role": "user", "parts": [{"text": args["prompt"]}]}],
        "generationConfig": {"maxOutputTokens": args["max_tokens"]},
    }
    if args.get("system"):
        body["systemInstruction"] = {"parts": [{"text": args["system"]}]}
    if args.get("temperature") is not None:
        body["generationConfig"]["temperature"] = args["temperature"]
    data = _gemini_generate(model, body, timeout)
    candidates = data.get("candidates") or []
    if not candidates:
        # Gemini reports safety blocks as an empty candidate list with
        # promptFeedback -- a permanent outcome for this input, not an outage.
        feedback = (data.get("promptFeedback") or {}).get("blockReason")
        raise ProviderError(f"no candidates returned{f' (blocked: {feedback})' if feedback else ''}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    usage = data.get("usageMetadata") or {}
    input_tokens = int(usage.get("promptTokenCount") or 0)
    output_tokens = int(usage.get("candidatesTokenCount") or 0)
    return {
        "text": text,
        "model": data.get("modelVersion") or model,
        "finish_reason": candidates[0].get("finishReason"),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost_microusd": llm_cost_microusd(model, input_tokens, output_tokens),
    }


def call_openai(args: dict, timeout: float) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not configured")
    model = args.get("model") or _OPENAI_DEFAULT_MODEL
    messages = []
    if args.get("system"):
        messages.append({"role": "system", "content": args["system"]})
    messages.append({"role": "user", "content": args["prompt"]})
    body = {"model": model, "messages": messages, "max_completion_tokens": args["max_tokens"]}
    if args.get("temperature") is not None:
        body["temperature"] = args["temperature"]
    data = _post_json(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        body=body,
        timeout=timeout,
    )
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("no choices returned")
    usage = data.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "text": (choices[0].get("message") or {}).get("content") or "",
        "model": data.get("model") or model,
        "finish_reason": choices[0].get("finish_reason"),
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "cost_microusd": llm_cost_microusd(model, input_tokens, output_tokens),
    }


# --------------------------------------------------------------------------
# Web search: Brave, Serper. Both are keyed; with neither key set the
# search service is simply absent from every advertised surface.
# --------------------------------------------------------------------------

_BRAVE_COST_MICROUSD = 5_000   # $5 / 1000 queries, Brave's published base tier
_SERPER_COST_MICROUSD = 1_000  # $50 / 50k queries, Serper's published rate


def call_brave_search(args: dict, timeout: float) -> dict:
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        raise ProviderError("BRAVE_SEARCH_API_KEY is not configured")
    data = _get_json(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": args["query"], "count": args["count"]},
        timeout=timeout,
    )
    raw = ((data.get("web") or {}).get("results")) or []
    results = [
        {
            "title": r.get("title") or "",
            "url": r.get("url") or "",
            "snippet": r.get("description") or "",
        }
        for r in raw[: args["count"]]
        if isinstance(r, dict) and r.get("url")
    ]
    return {"query": args["query"], "results": results, "cost_microusd": _BRAVE_COST_MICROUSD}


def call_serper_search(args: dict, timeout: float) -> dict:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise ProviderError("SERPER_API_KEY is not configured")
    data = _post_json(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": key},
        body={"q": args["query"], "num": args["count"]},
        timeout=timeout,
    )
    raw = data.get("organic") or []
    results = [
        {
            "title": r.get("title") or "",
            "url": r.get("link") or "",
            "snippet": r.get("snippet") or "",
        }
        for r in raw[: args["count"]]
        if isinstance(r, dict) and r.get("link")
    ]
    return {"query": args["query"], "results": results, "cost_microusd": _SERPER_COST_MICROUSD}


# --------------------------------------------------------------------------
# Web fetch / extraction. Self-contained: the "provider" is this node's own
# guarded HTTP client, which is why these two are live on every deployment.
# Every hop of every fetch goes through audits.blocked_target_reason via
# audits.fetch_once -- the same gate, the same rule, one implementation.
# --------------------------------------------------------------------------

_FETCH_TEXT_CAP = 500_000       # characters of body returned to the caller
_EXTRACT_TEXT_CAP = 200_000     # characters of extracted plain text
_EXTRACT_LINK_CAP = 200

_TEXTUAL_TYPES = ("text/", "application/json", "application/xml", "application/xhtml",
                  "application/javascript", "application/ld+json", "application/rss",
                  "application/atom")


def _guarded_fetch(url: str, timeout_note: str = "fetch") -> "httpx.Response":
    try:
        response = audits.fetch_once(url)
    except audits.TargetNotFetchable as exc:
        raise ProviderError(str(exc))
    except httpx.TimeoutException:
        raise ProviderError(f"{timeout_note} timed out", transient=True)
    except httpx.HTTPError as exc:
        raise ProviderError(f"{timeout_note} failed: {type(exc).__name__}", transient=True)
    return response


def call_web_fetch(args: dict, timeout: float) -> dict:
    response = _guarded_fetch(args["url"])
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    textual = any(content_type.startswith(t) for t in _TEXTUAL_TYPES) or not content_type
    text = None
    truncated = False
    if textual:
        body = response.text or ""
        truncated = len(body) > _FETCH_TEXT_CAP
        text = body[:_FETCH_TEXT_CAP]
    return {
        "url": args["url"],
        "final_url": str(response.url),
        "status": response.status_code,
        "content_type": content_type or None,
        "bytes": len(response.content or b""),
        "text": text,
        "truncated": truncated,
        "headers": {k.lower(): v for k, v in response.headers.items()},
    }


class _ExtractionParser(HTMLParser):
    """Title, metadata, visible text and links -- nothing rendered, nothing
    guessed. Script/style/template contents are dropped because they are
    code, not content, and a research agent quoting a minified bundle as
    'page text' is worse than useless."""

    _SKIP = {"script", "style", "template", "noscript", "svg", "head"}
    _BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "blockquote", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict = {}
        self.lang: Optional[str] = None
        self.canonical: Optional[str] = None
        self.links: list = []
        self._chunks: list = []
        self._chars = 0
        self._skip_depth = 0
        self._in_title = False
        self._link_href: Optional[str] = None
        self._link_text: list = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP and tag != "head":
            self._skip_depth += 1
            return
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "html" and attrs.get("lang"):
            self.lang = attrs["lang"]
        elif tag == "meta":
            name = attrs.get("name") or attrs.get("property")
            if name and attrs.get("content") is not None:
                self.meta.setdefault(name.lower(), attrs["content"])
        elif tag == "link" and (attrs.get("rel") or "").lower() == "canonical":
            self.canonical = attrs.get("href")
        elif tag == "a" and attrs.get("href") and len(self.links) < _EXTRACT_LINK_CAP:
            self._link_href = attrs["href"]
            self._link_text = []
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and tag != "head":
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_href is not None:
            text = " ".join("".join(self._link_text).split())[:200]
            self.links.append({"href": self._link_href, "text": text})
            self._link_href = None
            self._link_text = []
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        if self._link_href is not None:
            self._link_text.append(data)
        if self._chars < _EXTRACT_TEXT_CAP:
            self._chunks.append(data)
            self._chars += len(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)[:_EXTRACT_TEXT_CAP]


def extract_from_html(html: str, url: str, final_url: str) -> dict:
    parser = _ExtractionParser()
    try:
        parser.feed(html)
    except Exception:
        # A page broken enough to crash the parser still has whatever was
        # collected before the break; deliver that rather than nothing.
        pass
    text = parser.text()
    return {
        "url": url,
        "final_url": final_url,
        "title": " ".join(parser.title.split()) or None,
        "description": parser.meta.get("description") or parser.meta.get("og:description"),
        "lang": parser.lang,
        "canonical": parser.canonical,
        "text": text,
        "word_count": len(text.split()),
        "truncated": len(html) > _EXTRACT_TEXT_CAP,
        "links": parser.links,
    }


def call_web_extract(args: dict, timeout: float) -> dict:
    response = _guarded_fetch(args["url"], timeout_note="extract fetch")
    if response.status_code >= 400:
        raise ProviderError(
            f"target answered HTTP {response.status_code}, so there is no page to extract"
        )
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not any(
        content_type.startswith(t) for t in ("text/html", "application/xhtml", "text/plain")
    ):
        raise ProviderError(f"target is {content_type.split(';')[0]}, not an HTML page")
    return extract_from_html(response.text or "", args["url"], str(response.url))


# --------------------------------------------------------------------------
# Blockchain JSON-RPC (Base by default). Read-only allowlist: this node
# proxies QUESTIONS to the chain, it does not broadcast transactions --
# eth_sendRawTransaction is exactly the method an abuser would want a paid
# anonymous relay for, and nothing about the audit business needs it.
# --------------------------------------------------------------------------

RPC_ALLOWED_METHODS = frozenset({
    "eth_blockNumber", "eth_chainId", "eth_gasPrice", "eth_maxPriorityFeePerGas",
    "eth_feeHistory", "eth_getBalance", "eth_getTransactionCount",
    "eth_getBlockByNumber", "eth_getBlockByHash", "eth_getTransactionByHash",
    "eth_getTransactionReceipt", "eth_getBlockTransactionCountByNumber",
    "eth_getCode", "eth_call", "eth_estimateGas", "eth_getLogs",
    "eth_getStorageAt", "net_version", "web3_clientVersion",
})

_RPC_DEFAULT_URLS = "https://mainnet.base.org,https://base-rpc.publicnode.com"
_RPC_LOG_RANGE_CAP = 10_000


def rpc_upstreams() -> list:
    raw = os.environ.get("SVC_RPC_URLS", _RPC_DEFAULT_URLS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def rpc_network_name() -> str:
    return os.environ.get("SVC_RPC_NETWORK", "base")


def check_rpc_request(method: str, params: list) -> Optional[str]:
    """Why this RPC call is refused before any payment is read, or None."""
    if method not in RPC_ALLOWED_METHODS:
        return (
            f"method '{method}' is not offered; this service answers read-only "
            "queries only (see the input schema for the allowlist)"
        )
    if method == "eth_getLogs" and params and isinstance(params[0], dict):
        try:
            from_block = params[0].get("fromBlock")
            to_block = params[0].get("toBlock")
            if (isinstance(from_block, str) and from_block.startswith("0x")
                    and isinstance(to_block, str) and to_block.startswith("0x")):
                if int(to_block, 16) - int(from_block, 16) > _RPC_LOG_RANGE_CAP:
                    return f"eth_getLogs block range exceeds {_RPC_LOG_RANGE_CAP} blocks"
        except ValueError:
            return "eth_getLogs fromBlock/toBlock are not parseable hex quantities"
    return None


def call_chain_rpc(args: dict, timeout: float, upstream: str) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": args["method"], "params": args.get("params") or []}
    data = _post_json(
        upstream,
        headers={"User-Agent": audits.USER_AGENT},
        body=body,
        timeout=timeout,
    )
    if not isinstance(data, dict) or ("result" not in data and "error" not in data):
        raise ProviderError("upstream returned a non-JSON-RPC response", transient=True)
    out = {"network": rpc_network_name(), "method": args["method"]}
    if "error" in data and data["error"] is not None:
        error = data["error"]
        # An upstream that answers "limit exceeded" is the upstream being
        # unavailable to us, not the chain answering the question -- fall
        # over to the next node rather than billing for a rate-limit notice.
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        if re.search(r"rate.?limit|limit exceeded|too many request", message, re.I):
            raise ProviderError(f"upstream rate-limited: {message[:120]}", transient=True)
        # Any other JSON-RPC error object is the chain's authoritative answer
        # to this exact call (revert reasons included) and is delivered as one.
        out["error"] = error
    else:
        out["result"] = data.get("result")
    return out


# --------------------------------------------------------------------------
# Market data: Coinbase's public, keyless data APIs. Primary is the v2 data
# API; the Exchange market-data API is an independent backend at a different
# host, which is what makes it a real fallback rather than the same outage
# twice.
# --------------------------------------------------------------------------

_PAIR_RE = re.compile(r"^[A-Z0-9]{2,12}-[A-Z0-9]{2,12}$")
_CURRENCY_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def call_coinbase_market(args: dict, timeout: float) -> dict:
    op = args["op"]
    if op == "spot":
        data = _get_json(
            f"https://api.coinbase.com/v2/prices/{args['pair']}/spot", timeout=timeout
        )
        payload = data.get("data") or {}
        if not payload.get("amount"):
            raise ProviderError("no spot price in response", transient=True)
        return {
            "op": "spot",
            "pair": args["pair"],
            "amount": payload["amount"],
            "currency": payload.get("currency"),
            "source": "coinbase",
        }
    if op == "rates":
        data = _get_json(
            "https://api.coinbase.com/v2/exchange-rates",
            params={"currency": args["currency"]},
            timeout=timeout,
        )
        payload = data.get("data") or {}
        if not payload.get("rates"):
            raise ProviderError("no rates in response", transient=True)
        return {
            "op": "rates",
            "currency": payload.get("currency") or args["currency"],
            "rates": payload["rates"],
            "source": "coinbase",
        }
    if op == "ticker":
        data = _get_json(
            f"https://api.exchange.coinbase.com/products/{args['pair']}/ticker",
            headers={"User-Agent": audits.USER_AGENT},
            timeout=timeout,
        )
        if not data.get("price"):
            raise ProviderError("no ticker price in response", transient=True)
        return {
            "op": "ticker",
            "pair": args["pair"],
            "price": data.get("price"),
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "volume": data.get("volume"),
            "time": data.get("time"),
            "source": "coinbase-exchange",
        }
    raise ProviderError(f"unsupported op '{op}'")


def call_coinbase_exchange_spot_fallback(args: dict, timeout: float) -> dict:
    """Spot via the Exchange ticker -- only wired as the fallback for
    op=spot, where the two APIs answer the same question."""
    if args["op"] != "spot":
        raise ProviderError("fallback only answers op=spot")
    data = _get_json(
        f"https://api.exchange.coinbase.com/products/{args['pair']}/ticker",
        headers={"User-Agent": audits.USER_AGENT},
        timeout=timeout,
    )
    if not data.get("price"):
        raise ProviderError("no ticker price in response", transient=True)
    return {
        "op": "spot",
        "pair": args["pair"],
        "amount": data["price"],
        "currency": args["pair"].split("-")[-1],
        "source": "coinbase-exchange",
    }


# --------------------------------------------------------------------------
# Prediction-market data: Polymarket's public Gamma API. Read-only market
# data -- questions, outcome prices, volumes -- not order placement.
# --------------------------------------------------------------------------

_GAMMA_BASE = "https://gamma-api.polymarket.com"


def _parse_gamma_json_field(value):
    """Gamma encodes list fields (outcomes, outcomePrices) as JSON strings
    inside JSON. Parse defensively; hand back the raw value if it will not
    parse rather than dropping data."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _normalize_gamma_market(market: dict) -> dict:
    return {
        "id": market.get("id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "outcomes": _parse_gamma_json_field(market.get("outcomes")),
        "outcome_prices": _parse_gamma_json_field(market.get("outcomePrices")),
        "volume": market.get("volume"),
        "liquidity": market.get("liquidity"),
        "end_date": market.get("endDate"),
        "active": market.get("active"),
        "closed": market.get("closed"),
    }


def call_polymarket(args: dict, timeout: float) -> dict:
    op = args["op"]
    headers = {"User-Agent": audits.USER_AGENT}
    if op == "markets":
        params = {
            "limit": args["limit"],
            "active": str(bool(args.get("active", True))).lower(),
            "closed": str(bool(args.get("closed", False))).lower(),
            "order": "volumeNum",
            "ascending": "false",
        }
        data = _get_json(f"{_GAMMA_BASE}/markets", headers=headers, params=params, timeout=timeout)
        if not isinstance(data, list):
            raise ProviderError("unexpected markets response shape", transient=True)
        return {"op": "markets", "markets": [_normalize_gamma_market(m) for m in data],
                "source": "polymarket-gamma"}
    if op == "market":
        data = _get_json(f"{_GAMMA_BASE}/markets", headers=headers,
                         params={"slug": args["slug"]}, timeout=timeout)
        if not isinstance(data, list):
            raise ProviderError("unexpected market response shape", transient=True)
        if not data:
            raise ProviderError(f"no market found for slug '{args['slug']}'")
        return {"op": "market", "market": _normalize_gamma_market(data[0]),
                "source": "polymarket-gamma"}
    if op == "events":
        params = {
            "limit": args["limit"],
            "active": str(bool(args.get("active", True))).lower(),
            "closed": str(bool(args.get("closed", False))).lower(),
            "order": "volume",
            "ascending": "false",
        }
        data = _get_json(f"{_GAMMA_BASE}/events", headers=headers, params=params, timeout=timeout)
        if not isinstance(data, list):
            raise ProviderError("unexpected events response shape", transient=True)
        events = [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "slug": e.get("slug"),
                "volume": e.get("volume"),
                "end_date": e.get("endDate"),
                "market_count": len(e.get("markets") or []),
            }
            for e in data
            if isinstance(e, dict)
        ]
        return {"op": "events", "events": events, "source": "polymarket-gamma"}
    raise ProviderError(f"unsupported op '{op}'")


# --------------------------------------------------------------------------
# Image generation: Gemini's image model, OpenAI's gpt-image-1. The result
# is base64 in JSON because the response is the only delivery channel a
# per-call payer has -- there is no bucket to upload to on their behalf.
# --------------------------------------------------------------------------

_GEMINI_IMAGE_COST_MICROUSD = 39_000   # ~1290 image-output tokens at $30/MTok, list price
_OPENAI_IMAGE_COST_MICROUSD = 42_000   # gpt-image-1 medium 1024x1024, list price


def gemini_image_model() -> str:
    return os.environ.get("SVC_GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def call_gemini_image(args: dict, timeout: float) -> dict:
    model = gemini_image_model()
    data = _gemini_generate(
        model,
        {"contents": [{"role": "user", "parts": [{"text": args["prompt"]}]}]},
        timeout,
    )
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return {
                    "prompt": args["prompt"],
                    "image_base64": inline["data"],
                    "mime_type": inline.get("mimeType") or inline.get("mime_type") or "image/png",
                    "model": model,
                    "cost_microusd": _GEMINI_IMAGE_COST_MICROUSD,
                }
    raise ProviderError("no image in response")


def call_openai_image(args: dict, timeout: float) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not configured")
    model = os.environ.get("SVC_OPENAI_IMAGE_MODEL", "gpt-image-1")
    data = _post_json(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {key}"},
        body={"model": model, "prompt": args["prompt"], "size": "1024x1024", "n": 1},
        timeout=timeout,
    )
    items = data.get("data") or []
    if not items or not items[0].get("b64_json"):
        raise ProviderError("no image in response")
    return {
        "prompt": args["prompt"],
        "image_base64": items[0]["b64_json"],
        "mime_type": "image/png",
        "model": model,
        "cost_microusd": _OPENAI_IMAGE_COST_MICROUSD,
    }


# --------------------------------------------------------------------------
# Voice (text-to-speech): OpenAI TTS, Gemini TTS. Gemini returns raw 24 kHz
# PCM which nothing plays as-is, so it is wrapped into a WAV container here
# -- the caller asked for speech, not for a sample-format puzzle.
# --------------------------------------------------------------------------

_OPENAI_TTS_COST_PER_CHAR_MICROUSD = 15   # $15 / 1M characters, list price
_GEMINI_TTS_COST_MICROUSD = 20_000        # rough per-call estimate at list price


def call_openai_tts(args: dict, timeout: float) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not configured")
    model = os.environ.get("SVC_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    voice = args.get("voice") or "alloy"
    try:
        response = httpx.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": args["text"], "voice": voice,
                  "response_format": "mp3"},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        raise ProviderError("timed out", transient=True)
    except httpx.HTTPError as exc:
        raise ProviderError(f"transport error: {type(exc).__name__}", transient=True)
    if response.status_code in (408, 429) or response.status_code >= 500:
        raise ProviderError(f"upstream answered HTTP {response.status_code}", transient=True)
    if response.status_code >= 400:
        raise ProviderError(
            f"upstream refused the request (HTTP {response.status_code}): {_short_body(response)}"
        )
    audio = response.content or b""
    if not audio:
        raise ProviderError("empty audio response", transient=True)
    return {
        "text_length": len(args["text"]),
        "voice": voice,
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "mime_type": "audio/mpeg",
        "model": model,
        "cost_microusd": _OPENAI_TTS_COST_PER_CHAR_MICROUSD * len(args["text"]),
    }


def _pcm_to_wav(pcm: bytes, rate: int = 24_000, channels: int = 1, sample_width: int = 2) -> bytes:
    buffer = io.BytesIO()
    data_size = len(pcm)
    buffer.write(b"RIFF")
    buffer.write(struct.pack("<I", 36 + data_size))
    buffer.write(b"WAVEfmt ")
    byte_rate = rate * channels * sample_width
    buffer.write(struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate,
                             channels * sample_width, sample_width * 8))
    buffer.write(b"data")
    buffer.write(struct.pack("<I", data_size))
    buffer.write(pcm)
    return buffer.getvalue()


def call_gemini_tts(args: dict, timeout: float) -> dict:
    model = os.environ.get("SVC_GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    voice = args.get("voice") or "Kore"
    body = {
        "contents": [{"role": "user", "parts": [{"text": args["text"]}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    data = _gemini_generate(model, body, timeout)
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                mime = (inline.get("mimeType") or inline.get("mime_type") or "").lower()
                raw = base64.b64decode(inline["data"])
                if mime.startswith("audio/l16") or mime.startswith("audio/pcm") or not mime:
                    rate = 24_000
                    match = re.search(r"rate=(\d+)", mime)
                    if match:
                        rate = int(match.group(1))
                    raw = _pcm_to_wav(raw, rate=rate)
                    mime = "audio/wav"
                return {
                    "text_length": len(args["text"]),
                    "voice": voice,
                    "audio_base64": base64.b64encode(raw).decode("ascii"),
                    "mime_type": mime,
                    "model": model,
                    "cost_microusd": _GEMINI_TTS_COST_MICROUSD,
                }
    raise ProviderError("no audio in response")


# --------------------------------------------------------------------------
# Code execution. A real subprocess with real limits, not an eval() with a
# blocklist -- blocklists lose. Ships OFF (SVC_CODE_EXEC=1 enables it)
# because running strangers' code next to a wallet-holding service is a
# decision only the operator may make, knowingly.
#
# The sandbox layers, best first, degrading only with disclosure:
#   1. `unshare -rn`: a new user+network namespace when the kernel and
#      container policy allow it -- code runs with NO network at all.
#   2. setuid to nobody when running as root: the child cannot read this
#      process's /proc/<pid>/environ, which is where the x402/Stripe
#      secrets would otherwise leak. Without root and without unshare,
#      the sandbox refuses to run rather than quietly running weaker.
# Plus, always: rlimits (CPU, memory, file size, fds, procs), a clean
# environment (no inherited secrets), an empty temp working directory,
# wall-clock kill, and output caps.
# --------------------------------------------------------------------------

_CODE_OUTPUT_CAP = 64_000
_NOBODY_UID = 65534
_NOBODY_GID = 65534

_sandbox_probe: Optional[tuple] = None  # (mode or "none", python path)


def code_exec_enabled() -> bool:
    return os.environ.get("SVC_CODE_EXEC") == "1"


def _probe_unshare() -> bool:
    try:
        probe = subprocess.run(
            ["unshare", "-rn", "true"], capture_output=True, timeout=10
        )
        return probe.returncode == 0
    except Exception:
        return False


def _sandbox_python_candidates() -> list:
    """Interpreters the sandboxed child might be able to exec. The service
    can itself run from a venv inside a directory the dropped-privilege
    child cannot traverse, in which case sys.executable is the one python
    on the box the sandbox can NOT use -- so system interpreters are real
    candidates, not fallbacks of last resort."""
    candidates = [sys.executable]
    base = getattr(sys, "base_exec_prefix", None)
    if base:
        candidates.append(os.path.join(base, "bin", "python3"))
    candidates += ["/usr/local/bin/python3", "/usr/bin/python3"]
    seen: list = []
    for path in candidates:
        if path and path not in seen and os.path.isfile(path):
            seen.append(path)
    return seen


def _probe_sandbox() -> tuple:
    """The availability answer, established by actually running the sandbox.

    A mode inferred from 'unshare exists' or 'we are root' advertised a
    capability that then failed at exec time (a venv interpreter behind a
    0700 directory turns every run into 'Permission denied' -- observed,
    not hypothetical). So the probe runs print() end-to-end through the
    exact isolation a paid call would use, per candidate interpreter, and
    only a combination that produced output is ever advertised."""
    modes = []
    if _probe_unshare():
        modes.append("netns")
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        modes.append("setuid")
    for mode in modes:
        for python in _sandbox_python_candidates():
            try:
                result = _run_sandboxed_once(
                    mode, python, "print('sandbox-probe-ok')", stdin="",
                    wall_seconds=10, workdir_mode=0o777,
                )
            except Exception:
                continue
            if result[2] == 0 and "sandbox-probe-ok" in result[0]:
                return (mode, python)
    return ("none", "")


def sandbox_mode() -> Optional[str]:
    """Which isolation this host can actually provide: 'netns' (user+net
    namespace -- no network), 'setuid' (privilege drop, network reachable),
    or None (neither works end-to-end -- the service refuses to run code).
    Probed once, by running real code through the real sandbox."""
    global _sandbox_probe
    if _sandbox_probe is None:
        _sandbox_probe = _probe_sandbox()
    return None if _sandbox_probe[0] == "none" else _sandbox_probe[0]


def _sandbox_preexec():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except (ValueError, OSError):
        pass
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Order matters: groups, gid, then uid -- after setuid there is no
        # privilege left to drop the rest with.
        try:
            os.setgroups([])
        except OSError:
            pass
        os.setgid(_NOBODY_GID)
        os.setuid(_NOBODY_UID)


def _run_sandboxed_once(mode: str, python: str, code: str, stdin: str,
                        wall_seconds: float, workdir_mode: int):
    """One sandboxed execution. Returns (stdout_bytes_decoded, stderr,
    exit_code, timed_out, raw_lengths) -- shared by the probe and the paid
    path so the probe can never pass a configuration the real run fails."""
    with tempfile.TemporaryDirectory(prefix="hubvibe-code-") as workdir:
        # The child drops to nobody in EVERY mode where this process is root
        # (netns included -- the namespace maps whatever uid execs it), and a
        # cwd it cannot read turns every run into a permission error that
        # masquerades as the caller's code failing.
        os.chmod(workdir, workdir_mode)
        command = [python, "-I", "-c", code]
        if mode == "netns":
            command = ["unshare", "-rn"] + command
        try:
            completed = subprocess.run(
                command,
                input=stdin.encode("utf-8"),
                capture_output=True,
                timeout=wall_seconds,
                cwd=workdir,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": workdir,
                     "LANG": "C.UTF-8", "TMPDIR": workdir},
                preexec_fn=_sandbox_preexec,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"")
            return (
                stdout[:_CODE_OUTPUT_CAP].decode("utf-8", "replace"),
                f"killed: exceeded the {wall_seconds:g}s wall-clock limit",
                None, True, len(stdout),
            )
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    return (
        stdout[:_CODE_OUTPUT_CAP].decode("utf-8", "replace"),
        stderr[:_CODE_OUTPUT_CAP].decode("utf-8", "replace"),
        completed.returncode, False, max(len(stdout), len(stderr)),
    )


def run_code_sandboxed(args: dict, timeout: float) -> dict:
    mode = sandbox_mode()
    if mode is None:
        raise ProviderError(
            "this host cannot isolate untrusted code end-to-end, so code "
            "execution is refused"
        )
    python = _sandbox_probe[1]
    wall_seconds = min(float(args.get("timeout_seconds") or 5), timeout)
    try:
        stdout, stderr, exit_code, timed_out, raw_len = _run_sandboxed_once(
            mode, python, args["code"], args.get("stdin") or "",
            wall_seconds, workdir_mode=0o777,
        )
    except OSError as exc:
        raise ProviderError(f"sandbox could not start: {exc}", transient=True)
    # A timeout or a non-zero exit is the CALLER'S code doing what it did,
    # delivered as a result -- the sandbox ran, which is what is being sold.
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "truncated": raw_len > _CODE_OUTPUT_CAP,
        "sandbox": mode,
    }
