"""YouTube Data API v3, keyed: video search and per-video details.

Two REST calls behind one adapter: `search.list` (a query -> video ids in
YouTube's own ranking, with the total match count) and `videos.list` (up to
50 ids -> snippet, statistics and contentDetails in one request). Each is a
fresh request; nothing is cached.

The key is WORKER_YOUTUBE_API_KEY. Without it the adapter reports
unavailable and the worker is never advertised (fail-closed, like every
keyed provider here). The API has no monetary price -- Google meters it in
quota units (search.list 100, videos.list 1, from the published quota
table) -- so each result carries cost_micros=0, cost_measured=True and the
units spent in `usage`.

NEVER A GUESS: a count YouTube does not send (likeCount and commentCount are
absent when the uploader hides them) is null, never zero.
"""

import os
import re
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_YOUTUBE_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_YOUTUBE_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
BASE = os.environ.get("WORKER_YOUTUBE_BASE", "https://www.googleapis.com/youtube/v3")

MAX_RESULTS = 25
ORDERS = ("relevance", "date", "viewCount", "rating", "title")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
DESCRIPTION_CHARS = 500
# Google's published quota costs per call.
SEARCH_QUOTA_UNITS = 100
VIDEOS_QUOTA_UNITS = 1
WATCH_URL = "https://www.youtube.com/watch?v="

# Error reasons Google attaches (errors[].reason / details[].reason) that mean
# this DEPLOYMENT cannot call the API right now -- never the caller's fault.
_QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
                  "RATE_LIMIT_EXCEEDED")
_KEY_REASONS = ("keyInvalid", "API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED", "API_KEY_HTTP_REFERRER_BLOCKED",
                "API_KEY_IP_ADDRESS_BLOCKED", "accessNotConfigured", "SERVICE_DISABLED", "forbidden")

_DURATION = re.compile(r"^P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _api_key() -> str:
    return os.environ.get("WORKER_YOUTUBE_API_KEY", "").strip()


def parse_duration(text) -> Optional[int]:
    """ISO 8601 duration as YouTube writes it -> whole seconds.

    PT1H2M3S -> 3723, PT45S -> 45, P0D -> 0 (a live or upcoming stream),
    P1DT2H -> 93600. Anything else (missing, malformed, empty 'PT') -> None.
    """
    if not isinstance(text, str):
        return None
    match = _DURATION.match(text.strip())
    if not match or not any(g is not None for g in match.groups()):
        return None
    weeks, days, hours, minutes, seconds = (int(g) if g is not None else 0 for g in match.groups())
    return weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60 + seconds


def _count(value) -> Optional[int]:
    """YouTube statistics are decimal STRINGS ('52004'); absent when hidden."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _str(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _thumbnail(thumbnails) -> Optional[str]:
    if not isinstance(thumbnails, dict):
        return None
    for size in ("high", "medium", "default", "standard", "maxres"):
        entry = thumbnails.get(size)
        if isinstance(entry, dict) and _str(entry.get("url")):
            return entry["url"].strip()
    return None


def _live(snippet: dict) -> Optional[bool]:
    state = snippet.get("liveBroadcastContent")
    if not isinstance(state, str) or not state:
        return None
    return state != "none"


def video_record(video_id: str, snippet, statistics=None, content_details=None) -> dict:
    """One video in the worker's item shape. Statistics and contentDetails
    are optional: a search hit for which videos.list returned nothing (made
    private between the two calls) is still listed, with nulls where the
    numbers would be."""
    snippet = snippet if isinstance(snippet, dict) else {}
    statistics = statistics if isinstance(statistics, dict) else {}
    content_details = content_details if isinstance(content_details, dict) else {}
    description = _str(snippet.get("description"))
    return {
        "video_id": video_id,
        "url": WATCH_URL + video_id,
        "title": _str(snippet.get("title")),
        "description": description[:DESCRIPTION_CHARS] if description else None,
        "channel_id": _str(snippet.get("channelId")),
        "channel_title": _str(snippet.get("channelTitle")),
        "published_at": _str(snippet.get("publishedAt")),
        "duration_seconds": parse_duration(content_details.get("duration")),
        "view_count": _count(statistics.get("viewCount")),
        "like_count": _count(statistics.get("likeCount")),
        "comment_count": _count(statistics.get("commentCount")),
        "live": _live(snippet),
        "thumbnail_url": _thumbnail(snippet.get("thumbnails")),
    }


def _reasons(data) -> list:
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return []
    found = []
    for entry in list(error.get("errors") or []) + list(error.get("details") or []):
        if isinstance(entry, dict) and _str(entry.get("reason")):
            found.append(entry["reason"].strip())
    return found


def _message(data, status: int) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and _str(error.get("message")):
        return error["message"].strip()[:300]
    return f"HTTP {status}"


def _raise_for_status(status: int, data, call: str) -> None:
    """Map a YouTube error body onto the runtime's failure classes."""
    if status < 400:
        return
    reasons = _reasons(data)
    message = _message(data, status)
    quota = next((r for r in reasons if r in _QUOTA_REASONS), None)
    key = next((r for r in reasons if r in _KEY_REASONS), None)
    if quota:
        raise runtime.ProviderUnavailable(
            f"YouTube Data API quota exhausted on this deployment ({quota}): {message}")
    if key or status in (401, 403):
        which = key or (reasons[0] if reasons else f"HTTP {status}")
        raise runtime.ProviderUnavailable(f"YouTube Data API refused the key ({which}): {message}")
    if status == 400:
        raise runtime.InvalidRequest(f"YouTube rejected the {call} request: {message}")
    raise runtime.PermanentProviderError(f"YouTube Data API {call} returned {status}: {message}")


async def _get_json(path: str, params: dict) -> tuple:
    """(status, body) for one GET. The key travels in `params` and is never
    echoed into an error message."""
    url = f"{BASE}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"YouTube Data API {path} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"YouTube Data API {path} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"YouTube Data API {path} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429
                                             else "provider_transient")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"YouTube Data API {path} did not return JSON.") from None


def _total(data) -> Optional[int]:
    page = data.get("pageInfo") if isinstance(data, dict) else None
    total = page.get("totalResults") if isinstance(page, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


class _YouTubeData:
    id = "youtube-data-api"

    def available(self) -> bool:
        return bool(_api_key())

    def unavailable_reason(self) -> str:
        return "" if self.available() else "WORKER_YOUTUBE_API_KEY is not set on this deployment"

    async def search(self, query: str, max_results: int = 10, order: str = "relevance",
                     published_after: Optional[str] = None, region_code: Optional[str] = None,
                     relevance_language: Optional[str] = None) -> runtime.ProviderResult:
        """search.list, videos only: ids in YouTube's order, each with its
        search snippet, plus the total match count."""
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        params = {"part": "snippet", "q": query, "type": "video",
                  "maxResults": str(min(max(int(max_results), 1), MAX_RESULTS)),
                  "order": order if order in ORDERS else "relevance", "key": _api_key()}
        if published_after:
            params["publishedAfter"] = published_after
        if region_code:
            params["regionCode"] = region_code
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        status, data = await _get_json("search", params)
        _raise_for_status(status, data, "search")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise runtime.InvalidProviderResponse("YouTube search answered without an items list.")
        ids, snippets = [], {}
        for item in data["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), dict):
                continue
            video_id = item["id"].get("videoId")
            if not isinstance(video_id, str) or not VIDEO_ID.match(video_id) or video_id in snippets:
                continue
            ids.append(video_id)
            snippets[video_id] = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        value = {"query": query, "total_results": _total(data), "video_ids": ids, "snippets": snippets,
                 "region_code": _str(data.get("regionCode"))}
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"quota_units={SEARCH_QUOTA_UNITS} results={len(ids)}")

    async def videos(self, ids: list) -> runtime.ProviderResult:
        """videos.list for up to 50 ids in ONE call. Ids YouTube does not
        know are simply absent from `items` (a 404 or an empty list is an
        empty delivery, not a failure)."""
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        wanted = [v for v in ids if isinstance(v, str) and VIDEO_ID.match(v)]
        if not wanted:
            return runtime.ProviderResult(value={"total_results": 0, "items": []}, cost_micros=0,
                                          cost_measured=True, usage="quota_units=0 results=0")
        params = {"part": "snippet,statistics,contentDetails", "id": ",".join(wanted[:50]), "key": _api_key()}
        status, data = await _get_json("videos", params)
        if status == 404:
            return runtime.ProviderResult(value={"total_results": 0, "items": []}, cost_micros=0,
                                          cost_measured=True, usage=f"quota_units={VIDEOS_QUOTA_UNITS} results=0")
        _raise_for_status(status, data, "videos")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise runtime.InvalidProviderResponse("YouTube videos answered without an items list.")
        items = []
        for item in data["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not VIDEO_ID.match(item["id"]):
                continue
            items.append(video_record(item["id"], item.get("snippet"), item.get("statistics"),
                                      item.get("contentDetails")))
        value = {"total_results": _total(data), "items": items}
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"quota_units={VIDEOS_QUOTA_UNITS} results={len(items)}")


PROVIDERS = [_YouTubeData()]
