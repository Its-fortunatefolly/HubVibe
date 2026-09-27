"""Live web search, grounded via Gemini's Google Search tool."""

from .. import runtime
from ..providers import search_grounding
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

    value = await ctx.run("search", search_grounding.PROVIDERS, call, per_attempt_seconds=45)
    return {
        "query": query,
        "answer": value["answer"],
        "sources": value["sources"],
        "search_queries_used": value.get("queries") or [],
        "model": value["model"],
    }


SKILLS = {"search.web": web_search}
PRECHECKS = {"search.web": llm_skill.validate_language}
