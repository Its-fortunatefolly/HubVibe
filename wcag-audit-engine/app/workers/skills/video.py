"""video.youtube -- search YouTube or look up videos by id, with live
statistics and duration on every item, in one call.

Give a `query` and the worker runs YouTube's own search (videos only, in
YouTube's ranking, filterable by date, region and language) and then ONE
videos.list call for the ids it returned, so every item carries views,
likes, comments and duration alongside the snippet. Give `video_ids` and
the search is skipped: one videos.list call, the items in the order asked;
ids YouTube does not know are simply absent, never invented.

Exactly one of `query` or `video_ids` is accepted; anything else is refused
before the payment gate. Nothing is cached: `checked_at` (and `as_of`, the
same instant -- YouTube does not stamp its statistics) say when this node
read them. A count YouTube hides is null, never zero.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import youtube

MAX_QUERY_CHARS = 300
MAX_IDS = 25
DEFAULT_RESULTS = 10
SOURCE = "youtube-data-api"

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
_REGION = re.compile(r"^[A-Za-z]{2}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}$")
_SEARCH_ONLY = ("max_results", "order", "published_after", "region_code", "language")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _published_after(raw) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str) or not _RFC3339.match(raw.strip()):
        raise runtime.InvalidRequest(
            "`published_after` must be an RFC 3339 timestamp, e.g. 2026-01-01T00:00:00Z.")
    value = raw.strip()
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise runtime.InvalidRequest(f"`published_after` {value} is not a real date-time.") from None
    return value


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    query, ids = payload.get("query"), payload.get("video_ids")
    if (query is None) == (ids is None):
        raise runtime.InvalidRequest("Give exactly one of `query` (words to search for) or `video_ids` (a list of YouTube ids).")
    req = {"query": None, "video_ids": None, "max_results": DEFAULT_RESULTS, "order": "relevance",
           "published_after": None, "region_code": None, "language": None}
    if ids is not None:
        extra = [k for k in _SEARCH_ONLY if payload.get(k) is not None]
        if extra:
            raise runtime.InvalidRequest(f"{', '.join('`' + k + '`' for k in extra)} apply only with `query`.")
        if (not isinstance(ids, list) or not ids or len(ids) > MAX_IDS
                or not all(isinstance(v, str) and youtube.VIDEO_ID.match(v) for v in ids)):
            raise runtime.InvalidRequest(
                f"`video_ids` must be a list of 1 to {MAX_IDS} YouTube video ids (11 characters of "
                "letters, digits, - and _, e.g. dQw4w9WgXcQ).")
        req["video_ids"] = list(dict.fromkeys(ids))
        return req
    if not isinstance(query, str) or not query.strip():
        raise runtime.InvalidRequest("`query` must be a non-empty string: words to search YouTube for.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")
    req["query"] = query
    max_results = payload.get("max_results", DEFAULT_RESULTS)
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= youtube.MAX_RESULTS:
        raise runtime.InvalidRequest(f"`max_results` must be a whole number from 1 to {youtube.MAX_RESULTS}.")
    req["max_results"] = max_results
    order = payload.get("order", "relevance")
    if not isinstance(order, str) or order not in youtube.ORDERS:
        raise runtime.InvalidRequest(f"`order` must be one of {list(youtube.ORDERS)}.")
    req["order"] = order
    req["published_after"] = _published_after(payload.get("published_after"))
    region = payload.get("region_code")
    if region is not None:
        if not isinstance(region, str) or not _REGION.match(region.strip()):
            raise runtime.InvalidRequest("`region_code` must be an ISO 3166-1 alpha-2 country code, e.g. US, JP.")
        req["region_code"] = region.strip().upper()
    language = payload.get("language")
    if language is not None:
        if not isinstance(language, str) or not _LANGUAGE.match(language.strip()):
            raise runtime.InvalidRequest("`language` must be a 2-3 letter ISO 639 language code, e.g. en, ja, pt.")
        req["language"] = language.strip().lower()
    return req


def precheck(payload: dict) -> None:
    parse(payload)


async def _videos(ctx, ids: list) -> dict:
    async def call(provider):
        return await provider.videos(ids)

    return await ctx.run("videos", youtube.PROVIDERS, call, per_attempt_seconds=15, max_attempts=2)


async def search(ctx, payload: dict) -> dict:
    req = parse(payload)
    now = _now()
    if req["query"] is not None:
        async def call(provider):
            return await provider.search(req["query"], max_results=req["max_results"], order=req["order"],
                                         published_after=req["published_after"], region_code=req["region_code"],
                                         relevance_language=req["language"])

        found = await ctx.run("search", youtube.PROVIDERS, call, per_attempt_seconds=15, max_attempts=2)
        ids = found["video_ids"]
        total = found["total_results"]
        details = {}
        if ids:
            details = {item["video_id"]: item for item in (await _videos(ctx, ids))["items"]}
        # Search order is the order; a hit videos.list no longer returns is
        # still listed from its search snippet, with null statistics.
        items = [details.get(vid) or youtube.video_record(vid, found["snippets"].get(vid)) for vid in ids]
    else:
        ids = req["video_ids"]
        answer = await _videos(ctx, ids)
        details = {item["video_id"]: item for item in answer["items"]}
        items = [details[vid] for vid in ids if vid in details]
        total = answer["total_results"]
    return {
        "query": req["query"],
        "video_ids": req["video_ids"],
        "total_results": total,
        "items": items,
        "item_count": len(items),
        "source": SOURCE,
        "as_of": now,
        "checked_at": now,
    }


SKILLS = {"video.youtube": search}
PRECHECKS = {"video.youtube": precheck}
