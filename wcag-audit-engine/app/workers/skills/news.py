"""news.search -- current news on any topic, in any language, from the GDELT Project.

A query in any script is matched against the original titles of the
articles GDELT has seen worldwide in the window (it updates every 15
minutes), optionally only those published in one language; a US `symbol`
searches the company's own name from the SEC ticker table. Results are
merged, de-duplicated, newest first, each with its publisher, language and
link, and the GDELT citation GDELT's licence asks for. A search that failed
for this call is listed under `sources_failed`, never hidden.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from ..providers import news, sec_edgar
from .llm import validate_language

MAX_QUERY_CHARS = 300
MAX_LIMIT = 50
MAX_SINCE_HOURS = 720
DEFAULT_SINCE_HOURS = 72
_COMPANY_SUFFIX = re.compile(r"[,.]?\s+(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|group|"
                             r"n\.?v|s\.?a|ag|se|lp|llc)\.?$|\s*/[a-z]{2}/?$", re.I)
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
    language = validate_language(payload)
    region = payload.get("region")
    if region is not None:
        if not isinstance(region, str) or not _REGION.match(region.strip()):
            raise runtime.InvalidRequest("`region`, when given, must be an ISO 3166-1 alpha-2 country code (JP, KR, GB).")
        region = region.strip().upper()
    limit = payload.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise runtime.InvalidRequest(f"`limit` must be a whole number from 1 to {MAX_LIMIT}.")
    since_hours = payload.get("since_hours", DEFAULT_SINCE_HOURS)
    if since_hours is None:
        since_hours = DEFAULT_SINCE_HOURS
    if isinstance(since_hours, bool) or not isinstance(since_hours, int) or not 1 <= since_hours <= MAX_SINCE_HOURS:
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


def company_terms(title: str) -> list:
    name = (title or "").strip()
    for _ in range(3):
        name = _COMPANY_SUFFIX.sub("", name).strip()
    return news.terms_of(name)


async def search(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes, searched, ok, failed, batches = [], [], [], [], []
    primary = (req["language"] or "").split("-")[0].lower() or None
    language_filter = primary if primary in news.SRCLC or primary == "en" else None
    if primary and language_filter is None:
        notes.append(f"GDELT does not tag {req['language']!r} articles; the search covers every language.")
    if req["region"]:
        notes.append("GDELT does not record a publisher's country, so `region` is not a filter; `language` narrows "
                     "the press instead.")

    async def gdelt(label, terms):
        searched.append(label)

        async def call(provider):
            return await provider.search(terms, req["since_hours"], language_filter, max(req["limit"] * 2, 10))
        try:
            value = await ctx.run(label.replace(":", "_"), [news.GDELT], call, per_attempt_seconds=30, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append({"source": label, "reason": exc.detail})
            return
        ok.append(label)
        batches.append(value["articles"])

    async def by_symbol():
        try:
            hit = await sec_edgar.PROVIDERS[0].cik_for(req["symbol"])
        except runtime.WorkerError as exc:
            failed.append({"source": f"gdelt:symbol:{req['symbol']}", "reason": exc.detail})
            return
        terms = company_terms(hit["title"]) if hit else []
        if not terms:
            failed.append({"source": f"gdelt:symbol:{req['symbol']}",
                           "reason": "Symbol news covers US-listed tickers (SEC ticker table); send `query` with the "
                                     "company's name for other listings."})
            return
        notes.append(f"{req['symbol']} searched as {' '.join(terms)!r} ({hit['title']}).")
        await gdelt(f"gdelt:symbol:{req['symbol']}", terms)

    tasks = []
    if req["query"] is not None:
        tasks.append(gdelt("gdelt:query", news.terms_of(req["query"])))
    if req["symbol"] is not None:
        tasks.append(by_symbol())
    await asyncio.gather(*tasks)
    if not ok:
        raise runtime.TransientProviderError(
            "No news search answered: " + "; ".join(f"{f['source']}: {f['reason']}" for f in failed),
            reason="no_sources")
    articles = merge(batches, req["since_hours"], req["limit"])
    if not articles:
        notes.append("GDELT answered but no article title matched in the requested window.")
    return {
        "query": req["query"],
        "symbol": req["symbol"],
        "language": req["language"],
        "language_filter": language_filter,
        "sources_searched": searched,
        "sources_ok": ok,
        "sources_failed": failed,
        "articles": articles,
        "article_count": len(articles),
        "limit": req["limit"],
        "since_hours": req["since_hours"],
        "attribution": dict(news.CITATION),
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"news.search": search}
PRECHECKS = {"news.search": precheck}
