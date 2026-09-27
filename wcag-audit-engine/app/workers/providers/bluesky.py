"""Bluesky's public AppView (public.api.bsky.app), keyless.

The read-only XRPC methods Bluesky serves to anyone: a profile, an
author's feed, actor search and a post thread. Verified 2026-09-27 from
Cloud Shell and from the box: getProfile, getAuthorFeed, searchActors and
getPostThread answer 200 unauthenticated; searchPosts is 403 for
unauthenticated callers from both, so post search is not offered.

Nothing is cached; the skill stamps `checked_at`. Cost is zero and known.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_BLUESKY_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_BLUESKY_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
BASE = os.environ.get("WORKER_BLUESKY_BASE", "https://public.api.bsky.app/xrpc")
FEED_FILTERS = ("posts_no_replies", "posts_with_replies", "posts_and_author_threads")


async def _get_json(method: str, params: dict) -> tuple:
    """(status, body) for one XRPC query."""
    url = f"{BASE}/{method}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"Bluesky {method} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"Bluesky {method} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"Bluesky {method} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429
                                             else "provider_transient")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"Bluesky {method} did not return JSON.") from None


def _raise_for_status(status: int, data, method: str) -> None:
    if status < 400:
        return
    error = data.get("error") if isinstance(data, dict) else None
    message = (data.get("message") if isinstance(data, dict) else None) or f"HTTP {status}"
    if status == 400:
        # InvalidRequest / "Profile not found" / "Actor not found": the caller named something that is not there.
        raise runtime.InvalidRequest(f"Bluesky {method}: {message}")
    if status in (401, 403):
        raise runtime.PermanentProviderError(f"Bluesky {method} refused an unauthenticated call ({error or status}).")
    if status == 404:
        raise runtime.InvalidRequest(f"Bluesky {method}: not found ({message}).")
    raise runtime.PermanentProviderError(f"Bluesky {method} returned {status}: {message}")


class _BlueskyPublic:
    id = "bluesky-public-appview"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _query(self, method: str, params: dict, expect: str) -> dict:
        status, data = await _get_json(method, params)
        _raise_for_status(status, data, method)
        if not isinstance(data, dict) or expect not in data:
            raise runtime.InvalidProviderResponse(f"Bluesky {method} answered without `{expect}`.")
        return data

    async def profile(self, actor: str) -> runtime.ProviderResult:
        data = await self._query("app.bsky.actor.getProfile", {"actor": actor}, "did")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True, usage="requests=1")

    async def author_feed(self, actor: str, limit: int = 10, feed_filter: str = "posts_no_replies",
                          cursor: Optional[str] = None) -> runtime.ProviderResult:
        params = {"actor": actor, "limit": str(min(max(int(limit), 1), 100)),
                  "filter": feed_filter if feed_filter in FEED_FILTERS else "posts_no_replies"}
        if cursor:
            params["cursor"] = cursor
        data = await self._query("app.bsky.feed.getAuthorFeed", params, "feed")
        if not isinstance(data.get("feed"), list):
            raise runtime.InvalidProviderResponse("Bluesky getAuthorFeed answered with a non-list feed.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 posts={len(data['feed'])}")

    async def search_actors(self, query: str, limit: int = 10) -> runtime.ProviderResult:
        data = await self._query("app.bsky.actor.searchActors",
                                 {"q": query, "limit": str(min(max(int(limit), 1), 100))}, "actors")
        if not isinstance(data.get("actors"), list):
            raise runtime.InvalidProviderResponse("Bluesky searchActors answered with a non-list actors.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 actors={len(data['actors'])}")

    async def thread(self, uri: str, depth: int = 2) -> runtime.ProviderResult:
        data = await self._query("app.bsky.feed.getPostThread",
                                 {"uri": uri, "depth": str(min(max(int(depth), 0), 1000))}, "thread")
        if not isinstance(data.get("thread"), dict):
            raise runtime.InvalidProviderResponse("Bluesky getPostThread answered with a non-object thread.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True, usage="requests=1")


PROVIDERS = [_BlueskyPublic()]
