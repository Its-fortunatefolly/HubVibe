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

from .. import runtime
from . import extract as extract_skill
from . import llm as llm_skill
from . import market as market_skill


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
        "text": page["text"], "fields": fields})

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

    analysis = await llm_skill.analyze(ctx, {
        "text": material,
        "question": (
            f"{question} Be explicit about what the numbers do and do not support. "
            "This is market data, not investment advice."),
    })

    return {
        "product_id": product_id,
        "spot": quote,
        "prediction_markets": markets["markets"],
        "analysis": analysis["answer"],
        "model": analysis["model"],
        "disclaimer": (
            "Market data and implied probabilities only. Not investment advice "
            "and not a forecast by HubVibe."),
    }


SKILLS = {
    "research.brief": research_brief,
    "research.page_facts": page_facts,
    "market.intel": market_intel,
}
