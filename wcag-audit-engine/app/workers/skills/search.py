"""Live web search: Brave's independent index, answered from its results."""

from .. import runtime
from ..providers import brave, web_answer
from . import llm as llm_skill

MAX_QUERY_CHARS = 400


async def web_search(ctx, payload: dict) -> dict:
    query = (payload.get("query") or "").strip()
    if not query:
        raise runtime.InvalidRequest("`query` is required.")
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(
            f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")

    language = llm_skill.validate_language(payload)

    async def call(provider):
        return await provider.search(query, language=language)

    value = await ctx.run("search", web_answer.PROVIDERS, call, per_attempt_seconds=45)
    return {
        "query": query,
        "answer": value["answer"],
        "sources": value["sources"],
        "search_queries_used": value.get("queries") or [],
        "model": value["model"],
    }


async def web_sources(ctx, query: str) -> list:
    """Result URLs and titles straight from Brave, for bees that read the
    pages themselves and have no use for a written answer."""
    found = await ctx.run("search", brave.PROVIDERS, lambda p: p.web(query, count=10), per_attempt_seconds=20)
    return [{"url": r["url"], "title": r.get("title")} for r in found["web"] + found["news"] if r.get("url")]


SKILLS = {"search.web": web_search}
PRECHECKS = {"search.web": llm_skill.validate_language}
