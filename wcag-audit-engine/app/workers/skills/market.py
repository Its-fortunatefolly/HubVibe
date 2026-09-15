"""Market-data workers: spot quotes and prediction-market probabilities."""

import re

from .. import runtime
from ..providers import coinbase_market, polymarket

_PRODUCT = re.compile(r"^[A-Z0-9]{1,10}-[A-Z0-9]{2,10}$")


async def quote(ctx, payload: dict) -> dict:
    """Current price and 24h movement for one Coinbase product."""
    product_id = (payload.get("product_id") or payload.get("symbol") or "").strip().upper()
    if not _PRODUCT.match(product_id):
        raise runtime.InvalidRequest(
            "`product_id` must look like BTC-USD (base-quote, uppercase).")

    async def call(provider):
        return await provider.product(product_id)

    value = await ctx.run("quote", coinbase_market.PROVIDERS, call, per_attempt_seconds=20)
    if value.get("price") in (None, ""):
        raise runtime.InvalidProviderResponse(f"No price returned for {product_id}.")
    return {
        "product_id": value["product_id"],
        "price": value["price"],
        "price_change_24h_pct": value.get("price_change_24h_pct"),
        "volume_24h": value.get("volume_24h"),
        "base_currency": value.get("base_currency"),
        "quote_currency": value.get("quote_currency"),
        "status": value.get("status"),
        "source": "coinbase-advanced-trade-public",
    }


async def prediction_markets(ctx, payload: dict) -> dict:
    """Live prediction markets and their implied probabilities.

    Returns the probability as a percentage rather than the raw outcome price,
    because that is the number a buying agent reasons with.
    """
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise runtime.InvalidRequest("`query`, when given, must be a string.")
    try:
        limit = int(payload.get("limit", 10))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`limit` must be a whole number.")
    if not 1 <= limit <= 50:
        raise runtime.InvalidRequest("`limit` must be between 1 and 50.")

    async def call(provider):
        return await provider.search(query=query, limit=limit)

    value = await ctx.run("markets", polymarket.PROVIDERS, call, per_attempt_seconds=20)
    return {
        "query": query,
        "markets": value["markets"],
        "count": value["count"],
        "source": "polymarket-gamma",
        "note": (
            "Implied probabilities are what the market is currently pricing, "
            "not a forecast by HubVibe."
        ),
    }


SKILLS = {"market.quote": quote, "market.prediction": prediction_markets}
