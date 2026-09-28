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


async def prediction_markets(ctx, payload: dict, allow_empty: bool = False) -> dict:
    """Live prediction markets and their implied probabilities.

    Returns the probability as a percentage rather than the raw outcome price,
    because that is the number a buying agent reasons with.

    An empty list is never sold: a topic no open market matches is a free
    refusal, and an empty list with no topic means Polymarket answered with
    nothing (a provider failure, also unbilled). The one repeat buyer paid
    $0.05 four times on 2026-09-25/26 for empty lists; this is why.
    `allow_empty` is for composites (market.intel) that sell other data
    beside the markets.
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
    if not value["count"] and not allow_empty:
        if query:
            raise runtime.InvalidRequest(
                f"No open Polymarket market matches {query!r}. Try the market's main words "
                "(for example 'fed rate cut', 'bitcoin', 'election'), or send no query for the "
                "biggest markets. Nothing was charged.")
        raise runtime.TransientProviderError("Polymarket returned no open markets.")
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


async def rates(ctx, payload: dict) -> dict:
    """Coinbase's exchange-rate table for one base currency."""
    currency = (payload.get("currency") or "").strip().upper()
    if not re.match(r"^[A-Z0-9]{2,12}$", currency):
        raise runtime.InvalidRequest("`currency` must be a currency code, e.g. USD or ETH.")

    async def call(provider):
        return await provider.rates(currency)

    value = await ctx.run("rates", coinbase_market.RATES_PROVIDERS, call, per_attempt_seconds=20)
    return {"currency": value["currency"], "rates": value["rates"],
           "source": "coinbase-exchange-rates"}


async def ticker(ctx, payload: dict) -> dict:
    """Live bid/ask/volume for one Coinbase product, from the Exchange
    market-data host (independent of the market.quote data source)."""
    product_id = (payload.get("product_id") or payload.get("symbol") or "").strip().upper()
    if not _PRODUCT.match(product_id):
        raise runtime.InvalidRequest(
            "`product_id` must look like BTC-USD (base-quote, uppercase).")

    async def call(provider):
        return await provider.ticker(product_id)

    value = await ctx.run("ticker", coinbase_market.TICKER_PROVIDERS, call, per_attempt_seconds=20)
    return {"product_id": value["product_id"], "price": value["price"], "bid": value["bid"],
           "ask": value["ask"], "volume": value["volume"], "time": value["time"],
           "source": "coinbase-exchange"}


_SLUG = re.compile(r"^[a-z0-9-]{1,200}$")


async def prediction_market_by_slug(ctx, payload: dict) -> dict:
    """One named Polymarket market, by its slug."""
    slug = (payload.get("slug") or "").strip().lower()
    if not _SLUG.match(slug):
        raise runtime.InvalidRequest(
            "`slug` must be a Polymarket market slug (lowercase, letters/digits/hyphens).")

    async def call(provider):
        return await provider.by_slug(slug)

    value = await ctx.run("market", polymarket.PROVIDERS, call, per_attempt_seconds=20)
    return {"slug": value["slug"], "market": value["market"], "source": "polymarket-gamma"}


async def prediction_events(ctx, payload: dict) -> dict:
    """Live Polymarket events (a market grouping), highest volume first."""
    try:
        limit = int(payload.get("limit", 10))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`limit` must be a whole number.")
    if not 1 <= limit <= 50:
        raise runtime.InvalidRequest("`limit` must be between 1 and 50.")

    async def call(provider):
        return await provider.events(limit=limit)

    value = await ctx.run("events", polymarket.PROVIDERS, call, per_attempt_seconds=20)
    if not value["count"]:
        raise runtime.TransientProviderError("Polymarket returned no open events.")
    return {"events": value["events"], "count": value["count"], "source": "polymarket-gamma"}


SKILLS = {
    "market.quote": quote, "market.prediction": prediction_markets,
    "market.rates": rates, "market.ticker": ticker,
    "prediction.market": prediction_market_by_slug, "prediction.events": prediction_events,
}
