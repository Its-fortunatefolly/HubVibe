"""Brave Search API, keyed: independent web and news results in any
language and country.

Brave runs its own index (not a Google or Bing proxy). The Search plan
bills $5 per 1,000 requests (verified 2026-09-27: web results with title,
url, description, age, page_age, language, extra_snippets; a news endpoint;
50 requests per second). The key lives in BRAVE_SEARCH_API_KEY and the
adapter is fail-closed without it; an "Answers"-plan key returns no result
blocks, which the adapter reports as a plan problem instead of an empty
answer.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_BRAVE_TIMEOUT_SECONDS", "20"))
BASE = os.environ.get("WORKER_BRAVE_API_BASE", "https://api.search.brave.com/res/v1")
USER_AGENT = os.environ.get("WORKER_BRAVE_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
REQUEST_USD = float(os.environ.get("WORKER_BRAVE_REQUEST_USD", "0.005"))
MAX_COUNT = 20
FRESHNESS = {"day": "pd", "week": "pw", "month": "pm", "year": "py", "pd": "pd", "pw": "pw", "pm": "pm", "py": "py"}


def _key() -> str:
    return os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()


def _message(data, status: int) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("detail") or err.get("code") or err)[:300]
    return f"HTTP {status}"


async def _get(path: str, params: dict, what: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(f"{BASE}/{path}", params=params, headers={
                "Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": _key(),
                "User-Agent": USER_AGENT})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"Brave {what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"Brave {what} unreachable: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"Brave {what} did not return JSON (HTTP {response.status_code}).") from None
    status = response.status_code
    if status in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"Brave {what} returned {status}: {_message(data, status)}",
                                             reason="provider_overloaded" if status == 429 else "provider_transient")
    if status in (401, 403):
        raise runtime.ProviderUnavailable(f"Brave refused this deployment's key for {what} ({status}): {_message(data, status)}")
    if status == 422 or status == 400:
        message = _message(data, status)
        if "plan" in message.lower():
            raise runtime.ProviderUnavailable(f"Brave {what} is not in this deployment's plan: {message}")
        raise runtime.InvalidRequest(f"Brave rejected the {what}: {message}")
    if status >= 400:
        raise runtime.PermanentProviderError(f"Brave {what} returned {status}: {_message(data, status)}")
    if not isinstance(data, dict):
        raise runtime.InvalidProviderResponse(f"Brave {what} answered without a JSON object.")
    return data


def _params(query: str, count: int, country: Optional[str], language: Optional[str], freshness: Optional[str],
            safesearch: str) -> dict:
    params = {"q": query, "count": str(min(max(count, 1), MAX_COUNT)), "safesearch": safesearch}
    if country:
        params["country"] = country.upper()
    if language:
        base = language.split("-")[0].lower()
        params["search_lang"] = base
        # ui_lang must be language-COUNTRY (en-US, ja-JP); a bare tag is rejected.
        if "-" in language and len(language.split("-")[1]) == 2:
            params["ui_lang"] = f"{base}-{language.split('-')[1].upper()}"
        elif country:
            params["ui_lang"] = f"{base}-{country.upper()}"
    if freshness:
        params["freshness"] = FRESHNESS.get(freshness, freshness)
    return params


class _Brave:
    id = "brave-search"

    def available(self) -> bool:
        return bool(_key())

    def unavailable_reason(self) -> str:
        return "" if _key() else "BRAVE_SEARCH_API_KEY is not set"

    async def web(self, query: str, count: int = 10, country: Optional[str] = None, language: Optional[str] = None,
                  freshness: Optional[str] = None, safesearch: str = "moderate") -> runtime.ProviderResult:
        params = _params(query, count, country, language, freshness, safesearch)
        params["result_filter"] = "web,news"
        data = await _get("web/search", params, "web search")
        if "web" not in data and "news" not in data and "mixed" not in data:
            raise runtime.ProviderUnavailable(
                "Brave answered without result blocks: this key's plan does not include web results (use the Search plan).")
        web = [r for r in ((data.get("web") or {}).get("results") or []) if isinstance(r, dict)]
        news = [r for r in ((data.get("news") or {}).get("results") or []) if isinstance(r, dict)]
        query_block = data.get("query") or {}
        return runtime.ProviderResult(
            value={"web": web, "news": news, "more": bool(query_block.get("more_results_available")),
                   "country": query_block.get("country"), "altered": query_block.get("altered")},
            cost_micros=int(REQUEST_USD * 1_000_000), cost_measured=True,
            usage=f"web={len(web)} news={len(news)}")

    async def news(self, query: str, count: int = 10, country: Optional[str] = None, language: Optional[str] = None,
                   freshness: Optional[str] = None, safesearch: str = "moderate") -> runtime.ProviderResult:
        data = await _get("news/search", _params(query, count, country, language, freshness, safesearch), "news search")
        rows = [r for r in (data.get("results") or []) if isinstance(r, dict)]
        return runtime.ProviderResult(value={"news": rows}, cost_micros=int(REQUEST_USD * 1_000_000),
                                      cost_measured=True, usage=f"news={len(rows)}")


PROVIDERS = [_Brave()]
