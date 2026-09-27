"""search.results -- independent web and news results for a query, in any
language and country, from Brave's own index.

Where search.web returns a grounded written answer, this bee returns the
results themselves: title, URL, description, age and extra snippets for
the web, and headline, source and age for news, so an agent can pick what
to read next (fetch.raw, extract.page, opendata.table...). `language`
selects the search language and interface; `country` the market;
`freshness` limits results to the last day, week, month or year.
"""

import re
from datetime import datetime, timezone

from .. import runtime
from ..providers import brave
from .llm import validate_language

MAX_QUERY_CHARS = 400
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
FRESHNESS = ("day", "week", "month", "year")
SAFESEARCH = ("off", "moderate", "strict")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise runtime.InvalidRequest("`query` is required: words to search for, in any language.")
    query = " ".join(query.split())
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")
    country = payload.get("country")
    if country is not None:
        if not isinstance(country, str) or not _COUNTRY.match(country.strip()):
            raise runtime.InvalidRequest("`country`, when given, must be an ISO 3166-1 alpha-2 code (US, JP, DE).")
        country = country.strip().upper()
    count = payload.get("count", 10)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= brave.MAX_COUNT:
        raise runtime.InvalidRequest(f"`count` must be a whole number from 1 to {brave.MAX_COUNT}.")
    freshness = payload.get("freshness")
    if freshness is not None and freshness not in FRESHNESS:
        raise runtime.InvalidRequest(f"`freshness`, when given, must be one of {list(FRESHNESS)}.")
    news = payload.get("news", True)
    if not isinstance(news, bool):
        raise runtime.InvalidRequest("`news` must be true or false.")
    safesearch = payload.get("safesearch", "moderate")
    if safesearch not in SAFESEARCH:
        raise runtime.InvalidRequest(f"`safesearch` must be one of {list(SAFESEARCH)}.")
    return {"query": query, "country": country, "language": validate_language(payload), "count": count,
            "freshness": freshness, "news": news, "safesearch": safesearch}


def precheck(payload: dict) -> None:
    parse(payload)


def _web(r: dict) -> dict:
    meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
    profile = r.get("profile") if isinstance(r.get("profile"), dict) else {}
    snippets = [s for s in (r.get("extra_snippets") or []) if isinstance(s, str)][:5]
    return {
        "title": r.get("title"),
        "url": r.get("url"),
        "description": r.get("description"),
        "age": r.get("age"),
        "page_age": r.get("page_age"),
        "language": r.get("language"),
        "site_name": profile.get("name") or profile.get("long_name"),
        "hostname": meta.get("hostname"),
        "extra_snippets": snippets,
    }


def _news(r: dict) -> dict:
    meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
    return {
        "title": r.get("title"),
        "url": r.get("url"),
        "description": r.get("description"),
        "age": r.get("age"),
        "page_age": r.get("page_age"),
        "source": (r.get("source") if isinstance(r.get("source"), str) else None) or meta.get("hostname"),
        "breaking": bool(r.get("breaking")),
    }


async def results(ctx, payload: dict) -> dict:
    req = parse(payload)
    provider = brave.PROVIDERS[0]
    notes = []

    async def web(p):
        return await p.web(req["query"], req["count"], req["country"], req["language"], req["freshness"], req["safesearch"])
    got = await ctx.run("web", [provider], web, per_attempt_seconds=20, max_attempts=2)
    news_rows = got["news"]
    if req["news"] and not news_rows:
        async def news(p):
            return await p.news(req["query"], min(req["count"], 10), req["country"], req["language"], req["freshness"], req["safesearch"])
        try:
            news_rows = (await ctx.run("news", [provider], news, per_attempt_seconds=20, max_attempts=1))["news"]
        except runtime.WorkerError as exc:
            notes.append(f"News results unavailable for this call: {exc.detail}")
    web_out = [_web(r) for r in got["web"]][:req["count"]]
    news_out = [_news(r) for r in news_rows][:req["count"]] if req["news"] else []
    if not web_out and not news_out:
        notes.append("No result matched the query.")
    if got.get("altered"):
        notes.append(f"The engine altered the query to: {got['altered']}")
    return {
        "query": req["query"],
        "country": req["country"] or (got.get("country") or "").upper() or None,
        "language": req["language"],
        "freshness": req["freshness"],
        "web": web_out,
        "web_count": len(web_out),
        "news": news_out,
        "news_count": len(news_out),
        "more_results_available": bool(got["more"]),
        "source": "brave",
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"search.results": results}
PRECHECKS = {"search.results": precheck}
