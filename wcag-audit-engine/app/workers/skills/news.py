"""news.search -- current headlines on any topic, in any language, from the
publishers of the reader's own country, plus a ticker's own news feed.

A query in any script goes to the Google News edition for the language and
region asked for (71 editions verified; others tried and disclosed), and a
`symbol` adds Yahoo Finance's per-ticker headlines. Results are merged,
de-duplicated, filtered to the asked window and ordered newest first. A
feed that failed for this call is listed under `sources_failed`, never
hidden. Nothing is cached; `checked_at` says when the feeds were read.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from ..providers import news
from .llm import validate_language

MAX_QUERY_CHARS = 300
MAX_LIMIT = 50
MAX_SINCE_HOURS = 720
_SYMBOL = re.compile(r"^[A-Za-z0-9.\-=^]{1,12}$")
_REGION = re.compile(r"^[A-Za-z]{2}$")
_WS = re.compile(r"\s+")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    query = payload.get("query")
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise runtime.InvalidRequest("`query`, when given, must be words to search for, in any language.")
        query = _WS.sub(" ", query.strip())
        if len(query) > MAX_QUERY_CHARS:
            raise runtime.InvalidRequest(f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")
    symbol = payload.get("symbol")
    if symbol is not None:
        if not isinstance(symbol, str) or not _SYMBOL.match(symbol.strip()):
            raise runtime.InvalidRequest("`symbol`, when given, must be a ticker such as AAPL, 7203.T or 005930.KS.")
        symbol = symbol.strip().upper()
    if query is None and symbol is None:
        raise runtime.InvalidRequest("Give `query` (a topic, any language) and/or `symbol` (a ticker).")
    language = validate_language(payload) or "en"
    region = payload.get("region")
    if region is not None:
        if not isinstance(region, str) or not _REGION.match(region.strip()):
            raise runtime.InvalidRequest("`region`, when given, must be an ISO 3166-1 alpha-2 country code (JP, KR, GB).")
        region = region.strip().upper()
    limit = payload.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise runtime.InvalidRequest(f"`limit` must be a whole number from 1 to {MAX_LIMIT}.")
    since_hours = payload.get("since_hours")
    if since_hours is not None and (isinstance(since_hours, bool) or not isinstance(since_hours, int)
                                    or not 1 <= since_hours <= MAX_SINCE_HOURS):
        raise runtime.InvalidRequest(f"`since_hours`, when given, must be a whole number from 1 to {MAX_SINCE_HOURS}.")
    return {"query": query, "symbol": symbol, "language": language, "region": region, "limit": limit,
            "since_hours": since_hours}


def precheck(payload: dict) -> None:
    parse(payload)


def _key(article: dict) -> str:
    return _WS.sub(" ", (article.get("title") or "").lower()).strip()


def merge(batches: list, since_hours: Optional[int], limit: int, now: Optional[datetime] = None) -> list:
    """Merge feeds: drop duplicates by title, keep the asked window, newest first."""
    now = now or _now_dt()
    cutoff = now - timedelta(hours=since_hours) if since_hours else None
    seen, out = set(), []
    for batch in batches:
        for a in batch:
            k = _key(a)
            if not k or k in seen:
                continue
            if cutoff is not None:
                stamp = a.get("published_at")
                if not stamp:
                    continue
                try:
                    if datetime.fromisoformat(stamp.replace("Z", "+00:00")) < cutoff:
                        continue
                except ValueError:
                    continue
            seen.add(k)
            out.append(a)
    out.sort(key=lambda a: a.get("published_at") or "", reverse=True)
    return out[:limit]


async def search(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes, searched, ok, failed, batches = [], [], [], [], []
    edition = None

    async def google():
        nonlocal edition
        hl, gl, ceid = news.edition_for(req["language"], req["region"])
        searched.append(f"google_news:{ceid}")

        async def call(provider):
            return await provider.search(req["query"], req["language"], req["region"])
        try:
            value = await ctx.run("google_news", [news.GOOGLE], call, per_attempt_seconds=20, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append({"source": f"google_news:{ceid}", "reason": exc.detail})
            return
        edition = value["edition"]
        if not value["articles"] and not edition["verified"]:
            async def fallback(provider):
                return await provider.search(req["query"], "en", "US")
            try:
                value = await ctx.run("google_news_fallback", [news.GOOGLE], fallback, per_attempt_seconds=20, max_attempts=1)
                notes.append(f"Edition {ceid} returned nothing for this query; results are from the en/US edition.")
                edition = value["edition"]
            except runtime.WorkerError as exc:
                failed.append({"source": "google_news:US:en", "reason": exc.detail})
                return
        ok.append(f"google_news:{edition['ceid']}")
        batches.append(value["articles"])

    async def yahoo():
        searched.append(f"yahoo_finance:{req['symbol']}")

        async def call(provider):
            return await provider.headlines(req["symbol"])
        try:
            value = await ctx.run("yahoo_finance", [news.YAHOO], call, per_attempt_seconds=20, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append({"source": f"yahoo_finance:{req['symbol']}", "reason": exc.detail})
            return
        ok.append(f"yahoo_finance:{req['symbol']}")
        batches.append(value["articles"])

    tasks = []
    if req["query"] is not None:
        tasks.append(google())
    if req["symbol"] is not None:
        tasks.append(yahoo())
    await asyncio.gather(*tasks)
    if not ok:
        raise runtime.TransientProviderError(
            "No news source answered: " + "; ".join(f"{f['source']}: {f['reason']}" for f in failed),
            reason="no_sources")
    articles = merge(batches, req["since_hours"], req["limit"])
    if not articles:
        notes.append("The sources answered but no article matched the query in the requested window.")
    return {
        "query": req["query"],
        "symbol": req["symbol"],
        "language": req["language"],
        "edition": edition,
        "sources_searched": searched,
        "sources_ok": ok,
        "sources_failed": failed,
        "articles": articles,
        "article_count": len(articles),
        "limit": req["limit"],
        "since_hours": req["since_hours"],
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"news.search": search}
PRECHECKS = {"news.search": precheck}
