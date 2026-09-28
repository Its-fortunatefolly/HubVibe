"""Composite workers: several capabilities, one price, one completed result.

This is the layer that is worth more than the sum of its calls. A buyer could
call extract and then analyze themselves -- two purchases, two round trips,
and they still have to write the prompt and stitch the pieces. A composite
sells the finished product instead.

They are built by CALLING the single-capability skills, never by
reimplementing them: `research.brief` literally invokes `extract.page` and
`llm.analyze`. One implementation per capability, so a fix to extraction
improves every composite that uses it.

All steps share the caller's one deadline through `ctx`, so a composite can
never run past the payment window by doing its work in pieces.
"""

import asyncio
import logging
from urllib.parse import urlparse

from .. import runtime
from . import extract as extract_skill
from . import llm as llm_skill
from . import market as market_skill
from . import news as news_skill
from . import search as search_skill


_log = logging.getLogger("hubvibe.workers.composites")

# Product ids the spot market quotes -> the name the press uses.
_ASSET_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "XRP": "XRP Ripple", "DOGE": "Dogecoin",
                "ADA": "Cardano", "AVAX": "Avalanche", "LINK": "Chainlink", "DOT": "Polkadot", "LTC": "Litecoin",
                "BCH": "Bitcoin Cash", "USDC": "USDC stablecoin", "USDT": "Tether"}
NEWS_LIMIT = 8


async def recent_headlines(ctx, query: str, language=None, since_hours: int = 72) -> tuple:
    """Current headlines on `query` for a composite's evidence: (articles,
    note). Headlines are supplementary evidence, so a feed that fails does
    not fail the job -- it is disclosed in the note instead of hidden."""
    try:
        found = await news_skill.search(ctx, {"query": query, "language": language or "en",
                                              "limit": NEWS_LIMIT, "since_hours": since_hours})
    except runtime.WorkerError as exc:
        return [], f"Headlines unavailable for this call: {exc.detail}"
    except Exception as exc:  # supplementary evidence must never sink the job; say why it is missing
        _log.warning("headlines step failed for %r: %s", query, exc)
        return [], f"Headlines unavailable for this call: {type(exc).__name__}"
    articles = [{"title": a["title"], "url": a["url"], "source_name": a.get("source_name"),
                 "published_at": a.get("published_at")} for a in found["articles"]]
    note = None
    if not articles:
        note = f"No headlines matched {query!r} in the last {since_hours} hours."
    elif found.get("notes"):
        note = " ".join(found["notes"])
    return articles, note


def _headline_lines(articles: list) -> str:
    return "\n".join(f"  - [{a.get('published_at') or 'undated'}] {a['title']} ({a.get('source_name') or 'unknown source'})"
                     for a in articles) or "  (none)"


async def research_brief(ctx, payload: dict) -> dict:
    """Read a page and answer a question about it. Extraction -> inference.

    The composability demonstration in its simplest honest form: the output of
    one worker is the input of the next, inside one paid job.
    """
    url = extract_skill.validate_url(payload.get("url"))
    question = (payload.get("question")
                or "What does this page offer, who is it for, and what is the pricing?")

    page = await extract_skill.extract_page(ctx, {"url": url})
    analysis = await llm_skill.analyze(ctx, {
        "text": page["text"],
        "question": (
            f"{question}\n\n(The material is the page at {page['final_url'] or url}, "
            f"titled {page.get('title') or 'untitled'}.)"),
        "language": payload.get("language"),
    })

    return {
        "url": url,
        "final_url": page.get("final_url"),
        "title": page.get("title"),
        "question": question,
        "brief": analysis["answer"],
        "source": {
            "text_chars": page["text_chars"],
            "truncated": page["truncated"],
            "javascript_rendered": page["javascript_rendered"],
        },
        "model": analysis["model"],
    }


async def page_facts(ctx, payload: dict) -> dict:
    """Read a page and return caller-named fields as JSON. Extraction ->
    structured extraction.

    Sells the shape, not the prose: the caller names the fields and gets
    exactly those keys, which is what makes this usable as a step inside
    somebody else's pipeline.
    """
    url = extract_skill.validate_url(payload.get("url"))
    fields = payload.get("fields")
    if (not isinstance(fields, list) or not fields
            or not all(isinstance(f, str) and f.strip() for f in fields)):
        raise runtime.InvalidRequest("`fields` must be a non-empty list of field names.")

    page = await extract_skill.extract_page(ctx, {"url": url})
    structured = await llm_skill.extract_structured(ctx, {
        "text": page["text"], "fields": fields,
        "language": payload.get("language"),
    })

    return {
        "url": url,
        "final_url": page.get("final_url"),
        "title": page.get("title"),
        "fields": structured["fields"],
        "model": structured["model"],
    }


async def market_intel(ctx, payload: dict) -> dict:
    """Spot price + prediction markets on one subject, analysed together.

    Two independent live sources reconciled into one read. Neither source
    alone answers "what does the market currently think"; that is the product.
    """
    product_id = (payload.get("product_id") or payload.get("symbol") or "BTC-USD").strip().upper()
    query = payload.get("query")
    question = (payload.get("question")
                or "What do these two sources together say about current sentiment?")

    quote = await market_skill.quote(ctx, {"product_id": product_id})
    markets = await market_skill.prediction_markets(ctx, {
        "query": query, "limit": int(payload.get("limit", 10))})
    base = product_id.split("-")[0]
    news, news_note = await recent_headlines(
        ctx, query or _ASSET_NAMES.get(base, base), payload.get("language"))

    material = (
        f"Spot market (Coinbase, live):\n"
        f"  {quote['product_id']} price {quote['price']} "
        f"({quote.get('quote_currency')}), 24h change "
        f"{quote.get('price_change_24h_pct')}%, 24h volume {quote.get('volume_24h')}\n\n"
        f"Prediction markets (Polymarket, live), {markets['count']} matched"
        f"{f' for query {query!r}' if query else ''}:\n"
    )
    for entry in markets["markets"][:10]:
        odds = ", ".join(
            f"{o['outcome']} {o['probability_pct']}%"
            for o in entry.get("implied_probabilities", [])
            if o.get("probability_pct") is not None)
        material += f"  - {entry.get('question')} -> {odds or 'no priced outcomes'}\n"
    material += (f"\nRecent headlines (GDELT, searched now; {news_note or 'newest first'}):\n"
                 f"{_headline_lines(news)}\n")

    analysis = await llm_skill.analyze(ctx, {
        "text": material,
        "question": (
            f"{question} Be explicit about what the numbers do and do not support, and "
            "say what the headlines add or contradict. This is market data, not investment advice."),
        "language": payload.get("language"),
    })

    return {
        "product_id": product_id,
        "spot": quote,
        "prediction_markets": markets["markets"],
        "news": news,
        "news_note": news_note,
        "analysis": analysis["answer"],
        "model": analysis["model"],
        "disclaimer": (
            "Market data and implied probabilities only. Not investment advice "
            "and not a forecast by HubVibe."),
    }


async def _gather_sources(ctx, query: str, max_sources: int) -> tuple:
    """Search, then read up to `max_sources` distinct-host pages.

    Returns (read[], partial[]) and never raises on a source that cannot be
    read -- an unreachable page (paywall, block, timeout) is disclosed to the
    buyer, not treated as a job failure. Only the caller decides whether zero
    readable sources makes the whole job unbillable.
    """
    # Only the result URLs are needed here: no written answer to wait for.
    seen_hosts, queue = set(), []
    for source in await search_skill.web_sources(ctx, query):
        url = source.get("url")
        if not url:
            continue
        host = urlparse(url).netloc
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        queue.append(url)

    async def read_one(url):
        try:
            return await extract_skill.extract_page(ctx, {"url": url})
        except runtime.WorkerError as exc:
            return exc

    # Read the top results in parallel; a page that will not open is replaced
    # by the next result, so a bot-walled site costs one slot, not the job.
    read, partial = [], []
    while queue and len(read) < max_sources:
        batch, queue = queue[:max_sources - len(read)], queue[max_sources - len(read):]
        if ctx.remaining() < 30:
            partial.extend({"url": url, "reason": "ran out of time before this source"} for url in batch)
            break
        for url, page in zip(batch, await asyncio.gather(*(read_one(url) for url in batch))):
            if isinstance(page, runtime.WorkerError):
                partial.append({"url": url, "reason": page.detail})
            else:
                read.append({"url": page.get("final_url") or url, "title": page.get("title"),
                             "text": page["text"][:6000]})
    return read, partial


def _numbered_material(read: list) -> str:
    return "\n\n---\n\n".join(
        f"[{i + 1}] {r['title'] or r['url']} ({r['url']}):\n{r['text']}"
        for i, r in enumerate(read))


async def research_web(ctx, payload: dict) -> dict:
    """Answer a question from live web search, with numbered citations.

    Search -> read up to a few distinct sources -> synthesize with [n]
    citations tying every claim back to where it came from.
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise runtime.InvalidRequest("`question` is required.")
    try:
        max_sources = int(payload.get("max_sources", 3))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`max_sources` must be a whole number.")
    if not 1 <= max_sources <= 4:
        raise runtime.InvalidRequest("`max_sources` must be between 1 and 4.")

    read, partial = await _gather_sources(ctx, question, max_sources)
    if not read:
        raise runtime.TransientProviderError(
            "No search result could be read (every source was unreachable or blocked).",
            reason="no_sources")

    analysis = await llm_skill.analyze(ctx, {
        "text": _numbered_material(read),
        "question": (f"{question}\n\nCite every claim with [n] matching the source "
                    "number above. If the sources do not answer the question, say so."),
        "language": payload.get("language"),
    })
    return {
        "question": question,
        "answer": analysis["answer"],
        "sources": [{"n": i + 1, "url": r["url"], "title": r["title"]}
                   for i, r in enumerate(read)],
        "partial": partial,
        "model": analysis["model"],
    }


async def research_company(ctx, payload: dict) -> dict:
    """Research and verify a company from live web sources: what it does,
    what it sells, and anything notable -- cited, with any thin or
    conflicting evidence disclosed rather than papered over."""
    company = (payload.get("company") or "").strip()
    if not company:
        raise runtime.InvalidRequest("`company` is required.")
    try:
        max_sources = int(payload.get("max_sources", 4))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`max_sources` must be a whole number.")
    if not 1 <= max_sources <= 4:
        raise runtime.InvalidRequest("`max_sources` must be between 1 and 4.")

    read, partial = await _gather_sources(
        ctx, f"{company} company", max_sources)
    if not read:
        raise runtime.TransientProviderError(
            "No source about this company could be read.", reason="no_sources")
    news, news_note = await recent_headlines(ctx, company, payload.get("language"), since_hours=24 * 30)

    analysis = await llm_skill.analyze(ctx, {
        "text": (_numbered_material(read)
                 + f"\n\nRecent headlines about {company} (GDELT, last 30 days, searched now; "
                 f"{news_note or 'newest first'}; not numbered sources, cite them as 'headlines'):\n"
                 f"{_headline_lines(news)}\n"),
        "question": (
            f"Write a research brief on {company}: what it does, its main products or "
            "services, and anything notable from these sources (funding, reputation, "
            "recent news). Cite every claim with [n]. If the sources conflict or are "
            "thin, say so explicitly rather than filling gaps with assumptions."),
        "language": payload.get("language"),
    })
    return {
        "company": company,
        "report": analysis["answer"],
        "sources": [{"n": i + 1, "url": r["url"], "title": r["title"]}
                   for i, r in enumerate(read)],
        "partial": partial,
        "news": news,
        "news_note": news_note,
        "model": analysis["model"],
    }


SKILLS = {
    "research.brief": research_brief,
    "research.page_facts": page_facts,
    "market.intel": market_intel,
    "research.web": research_web,
    "research.company": research_company,
}
# A bad `language` is refused before the payment gate, like any bad input.
PRECHECKS = {name: llm_skill.validate_language for name in SKILLS}
