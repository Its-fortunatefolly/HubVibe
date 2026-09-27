"""X (Twitter) API v2, keyed, read-only: recent post counts and a recent
search sample -- consumed ONLY to compute aggregates.

The developer agreement HubVibe accepted (2026-09-27) forbids reselling
anything received from the X API. So this adapter exists to feed
derived, aggregate analysis and nothing else: the skill that uses it never
returns post text, ids, authors or profiles. The provider itself returns
the raw API pages to the skill, which reduces them to numbers.

Token: X_BEARER_TOKEN (the console shows it URL-encoded; store it decoded).
Costs (X pay-per-usage, 2026-09-27): posts read $0.005 each; recent counts
are included with access. Each search page is at most 100 posts.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_X_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_X_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
BASE = os.environ.get("WORKER_X_API_BASE", "https://api.x.com/2")
POST_READ_USD = float(os.environ.get("WORKER_X_POST_READ_USD", "0.005"))
MAX_QUERY_CHARS = 512
GRANULARITIES = ("hour", "day")


def _token() -> str:
    return os.environ.get("X_BEARER_TOKEN", "").strip()


async def _get_json(path: str, params: dict) -> tuple:
    url = f"{BASE}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url, params=params, headers={
                "Authorization": f"Bearer {_token()}", "User-Agent": USER_AGENT, "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"X API {path} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"X API {path} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"X API {path} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429
                                             else "provider_transient")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"X API {path} did not return JSON.") from None


def _message(data, status: int) -> str:
    if isinstance(data, dict):
        for key in ("detail", "title"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()[:300]
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or errors[0].get("detail") or errors[0])[:300]
    return f"HTTP {status}"


def _raise_for_status(status: int, data, what: str) -> None:
    if status < 400:
        return
    message = _message(data, status)
    if status in (401, 403, 402):
        raise runtime.ProviderUnavailable(f"X API refused this deployment's token for {what} ({status}): {message}")
    if status == 400:
        raise runtime.InvalidRequest(f"X rejected the query for {what}: {message}")
    raise runtime.PermanentProviderError(f"X API {what} returned {status}: {message}")


class _XApi:
    id = "x-api-v2"

    def available(self) -> bool:
        return bool(_token())

    def unavailable_reason(self) -> str:
        return "" if self.available() else "X_BEARER_TOKEN is not set on this deployment"

    async def counts_recent(self, query: str, granularity: str = "day",
                            start_time: Optional[str] = None, end_time: Optional[str] = None) -> runtime.ProviderResult:
        """Post counts per bucket over the last 7 days. Included with access
        (no per-post charge)."""
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        params = {"query": query, "granularity": granularity if granularity in GRANULARITIES else "day"}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        status, data = await _get_json("tweets/counts/recent", params)
        _raise_for_status(status, data, "counts")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise runtime.InvalidProviderResponse("X counts answered without a data list.")
        buckets = []
        for row in data["data"]:
            if not isinstance(row, dict):
                continue
            count = row.get("tweet_count")
            if isinstance(count, bool) or not isinstance(count, int):
                continue
            buckets.append({"start": row.get("start"), "end": row.get("end"), "count": count})
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        total = meta.get("total_tweet_count")
        value = {"buckets": buckets,
                 "total": total if isinstance(total, int) and not isinstance(total, bool) else sum(b["count"] for b in buckets)}
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 buckets={len(buckets)}")

    async def search_recent(self, query: str, max_results: int = 10, start_time: Optional[str] = None,
                            end_time: Optional[str] = None, next_token: Optional[str] = None) -> runtime.ProviderResult:
        """One page (10-100 posts) of recent search with the fields the
        aggregates need. Billed by X per post read; recorded as such."""
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        params = {"query": query, "max_results": str(min(max(int(max_results), 10), 100)),
                  "tweet.fields": "public_metrics,created_at,lang,entities,possibly_sensitive"}
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if next_token:
            params["next_token"] = next_token
        status, data = await _get_json("tweets/search/recent", params)
        _raise_for_status(status, data, "search")
        if not isinstance(data, dict):
            raise runtime.InvalidProviderResponse("X search answered without JSON object.")
        posts = [p for p in (data.get("data") or []) if isinstance(p, dict)]
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        value = {"posts": posts, "next_token": meta.get("next_token") if isinstance(meta.get("next_token"), str) else None,
                 "result_count": meta.get("result_count") if isinstance(meta.get("result_count"), int) else len(posts)}
        return runtime.ProviderResult(value=value, cost_micros=int(round(len(posts) * POST_READ_USD * 1_000_000)),
                                      cost_measured=True, usage=f"requests=1 posts_read={len(posts)}")


PROVIDERS = [_XApi()]
