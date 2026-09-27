"""social.x_pulse -- what X (Twitter) is saying about a topic, as numbers
only: post volume over the last days, engagement totals and averages,
language and hashtag distribution, link and media share.

AGGREGATES ONLY, BY AGREEMENT. HubVibe's X developer agreement forbids
reselling anything received from the X API, so this worker never returns
a post, an id, a handle or a profile: it reads a bounded sample of recent
posts and reduces them to figures, and reads the recent-counts endpoint
for volume. The sample size is the caller's choice (0 = counts only) and
is what X bills for, so it is disclosed in `sample` and priced into the
route.

ALWAYS CURRENT: every call is fresh; `as_of` is the end of the window
(now), `checked_at` when the node read it. NEVER A GUESS: a metric X does
not report is null.
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from ..providers import x_api

MAX_QUERY_CHARS = 400
MAX_DAYS = 7
MAX_SAMPLE = 100
DEFAULT_SAMPLE = 40
GRANULARITIES = ("hour", "day")
SOURCE = "x-api-v2"
_LANG = re.compile(r"^[A-Za-z]{2}$")
# Query operators that would turn this into a per-account read rather than
# a topic aggregate are still allowed (from:, to:) -- the OUTPUT stays
# aggregate regardless. What is refused is an empty or over-long query.


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise runtime.InvalidRequest("`query` is required: words or X search operators (e.g. \"open source\" lang:en).")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")
    days = payload.get("days", MAX_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise runtime.InvalidRequest(f"`days` must be a whole number from 1 to {MAX_DAYS} (X's recent window).")
    granularity = payload.get("granularity", "day")
    if not isinstance(granularity, str) or granularity not in GRANULARITIES:
        raise runtime.InvalidRequest(f"`granularity` must be one of {list(GRANULARITIES)}.")
    sample = payload.get("sample", DEFAULT_SAMPLE)
    if isinstance(sample, bool) or not isinstance(sample, int) or not 0 <= sample <= MAX_SAMPLE:
        raise runtime.InvalidRequest(f"`sample` must be a whole number from 0 (counts only) to {MAX_SAMPLE}.")
    lang = payload.get("lang")
    if lang is not None:
        if not isinstance(lang, str) or not _LANG.match(lang.strip()):
            raise runtime.InvalidRequest("`lang`, when given, must be a two-letter language code such as en or ja.")
        lang = lang.strip().lower()
    include_retweets = payload.get("include_retweets", False)
    if not isinstance(include_retweets, bool):
        raise runtime.InvalidRequest("`include_retweets`, when given, must be true or false.")
    effective = query
    if lang and "lang:" not in query:
        effective += f" lang:{lang}"
    if not include_retweets and "is:retweet" not in query:
        effective += " -is:retweet"
    if len(effective) > x_api.MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"The effective query is over X's {x_api.MAX_QUERY_CHARS}-character limit.")
    return {"query": query, "effective_query": effective, "days": days, "granularity": granularity,
            "sample": sample, "lang": lang, "include_retweets": include_retweets}


def precheck(payload: dict) -> None:
    parse(payload)


def _metric(post: dict, key: str) -> Optional[int]:
    metrics = post.get("public_metrics") if isinstance(post.get("public_metrics"), dict) else {}
    value = metrics.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def aggregate(posts: list) -> dict:
    """Reduce raw posts to figures. Nothing identifying survives this."""
    n = len(posts)
    totals = {k: 0 for k in ("like", "retweet", "reply", "quote", "impression", "bookmark")}
    seen = {k: 0 for k in totals}
    max_likes, max_impressions = None, None
    languages, hashtags = Counter(), Counter()
    with_links = with_media = with_hashtags = sensitive = 0
    for post in posts:
        for k in totals:
            v = _metric(post, f"{k}_count")
            if v is not None:
                totals[k] += v
                seen[k] += 1
        likes = _metric(post, "like_count")
        if likes is not None:
            max_likes = likes if max_likes is None else max(max_likes, likes)
        imps = _metric(post, "impression_count")
        if imps is not None:
            max_impressions = imps if max_impressions is None else max(max_impressions, imps)
        lang = post.get("lang")
        if isinstance(lang, str) and lang:
            languages[lang] += 1
        entities = post.get("entities") if isinstance(post.get("entities"), dict) else {}
        tags = [t.get("tag") for t in (entities.get("hashtags") or []) if isinstance(t, dict) and isinstance(t.get("tag"), str)]
        if tags:
            with_hashtags += 1
            for t in set(x.lower() for x in tags):
                hashtags[t] += 1
        urls = [u for u in (entities.get("urls") or []) if isinstance(u, dict)]
        if any(not str(u.get("expanded_url") or u.get("url") or "").startswith(("https://twitter.com", "https://x.com", "https://t.co")) for u in urls):
            with_links += 1
        if any(str(u.get("expanded_url") or "").startswith(("https://twitter.com", "https://x.com")) and "/photo/" in str(u.get("expanded_url") or "") or "/video/" in str(u.get("expanded_url") or "") for u in urls):
            with_media += 1
        if post.get("possibly_sensitive") is True:
            sensitive += 1
    interactions = totals["like"] + totals["retweet"] + totals["reply"] + totals["quote"]

    def mean(total, count):
        return (total / count) if count else None

    def pct(count):
        return round(100.0 * count / n, 1) if n else None

    return {
        "posts_read": n,
        "likes_total": totals["like"] if seen["like"] else None,
        "reposts_total": totals["retweet"] if seen["retweet"] else None,
        "replies_total": totals["reply"] if seen["reply"] else None,
        "quotes_total": totals["quote"] if seen["quote"] else None,
        "impressions_total": totals["impression"] if seen["impression"] else None,
        "bookmarks_total": totals["bookmark"] if seen["bookmark"] else None,
        "mean_likes": mean(totals["like"], seen["like"]),
        "mean_impressions": mean(totals["impression"], seen["impression"]),
        "max_likes": max_likes,
        "max_impressions": max_impressions,
        "engagement_rate_pct": round(100.0 * interactions / totals["impression"], 3) if totals["impression"] else None,
        "languages": dict(languages.most_common()),
        "top_hashtags": [{"tag": t, "posts": c} for t, c in hashtags.most_common(10)],
        "with_hashtags_pct": pct(with_hashtags),
        "with_links_pct": pct(with_links),
        "with_media_pct": pct(with_media),
        "possibly_sensitive_pct": pct(sensitive),
    }


async def pulse(ctx, payload: dict) -> dict:
    req = parse(payload)
    end = _now()
    start = end - timedelta(days=req["days"])
    # X requires end_time at least 10 seconds before now for recent search.
    end_param = _iso(end - timedelta(seconds=15))
    start_param = _iso(start)

    async def counts(provider):
        return await provider.counts_recent(req["effective_query"], granularity=req["granularity"],
                                            start_time=start_param, end_time=end_param)

    volume = await ctx.run("counts", x_api.PROVIDERS, counts, per_attempt_seconds=15, max_attempts=2)

    posts, pages, next_token = [], 0, None
    while req["sample"] > 0 and len(posts) < req["sample"] and pages < 3:
        want = min(100, max(10, req["sample"] - len(posts)))
        token = next_token

        async def page(provider, want=want, token=token):
            return await provider.search_recent(req["effective_query"], max_results=want, start_time=start_param,
                                                end_time=end_param, next_token=token)

        result = await ctx.run(f"search_page_{pages + 1}", x_api.PROVIDERS, page, per_attempt_seconds=15, max_attempts=2)
        pages += 1
        posts.extend(result["posts"])
        next_token = result.get("next_token")
        if not next_token or not result["posts"]:
            break
    posts = posts[:req["sample"]]

    checked_at = _iso(_now())
    return {
        "query": req["query"],
        "effective_query": req["effective_query"],
        "window": {"start": start_param, "end": end_param, "days": req["days"]},
        "granularity": req["granularity"],
        "volume": {"total": volume["total"], "buckets": volume["buckets"], "bucket_count": len(volume["buckets"])},
        "sample": {"requested": req["sample"], "read": len(posts), "pages": pages,
                   "x_cost_usd": round(len(posts) * x_api.POST_READ_USD, 4)},
        "engagement": aggregate(posts) if posts else None,
        "source": SOURCE,
        "as_of": end_param,
        "checked_at": checked_at,
    }


SKILLS = {"social.x_pulse": pulse}
PRECHECKS = {"social.x_pulse": precheck}
