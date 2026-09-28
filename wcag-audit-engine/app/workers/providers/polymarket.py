"""Polymarket prediction-market data via the public Gamma API.

Connected, not rebuilt. Verified keyless 2026-09-15: a live market with
outcome prices came back with no credential.

Read-only market DATA. This worker does not trade, hold positions, or take
any action in a market; it reports what a market currently implies.
"""

import json
import re
import os
from typing import Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_BASE = os.environ.get("WORKER_POLYMARKET_BASE", "https://gamma-api.polymarket.com")
_TIMEOUT = float(os.environ.get("WORKER_MARKET_TIMEOUT_SECONDS", "20"))


def _as_list(raw):
    """Gamma returns these as JSON-encoded STRINGS, not arrays -- passing the
    raw value through would hand the buyer a string where the schema promises
    a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [raw]
    return []


# Words that carry no topic on their own, so a market is not matched on them.
_STOPWORDS = {
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "by", "can", "could", "did", "do", "does", "for",
    "from", "has", "have", "how", "if", "in", "into", "is", "it", "its", "market", "markets", "more", "most",
    "odds", "of", "on", "or", "over", "polymarket", "prediction", "predictions", "than", "that", "the", "this",
    "to", "under", "was", "what", "when", "where", "which", "who", "why", "will", "with", "would",
}
_TOKEN = re.compile(r"[0-9a-z]+")


def _normal(text: str) -> str:
    # "$5,000" and "5000" are the same number; hyphenated slugs are words.
    return re.sub(r"(?<=\d),(?=\d)", "", (text or "").lower()).replace("-", " ")


def keywords(query: str) -> list:
    """The query's topic words, in order, without filler."""
    out = []
    for word in _TOKEN.findall(_normal(query)):
        if word not in _STOPWORDS and (len(word) > 2 or word.isdigit()) and word not in out:
            out.append(word)
    return out


# Words too common in market questions to identify a topic by themselves.
_GENERIC = {
    "above", "after", "before", "below", "best", "between", "cut", "cuts", "day", "end", "first", "happen",
    "hit", "last", "month", "new", "next", "price", "prices", "rate", "rates", "reach", "say", "says", "stock",
    "stocks", "today", "tomorrow", "top", "week", "win", "winner", "winners", "wins", "year", "years",
}
# Tickers buyers type for the names markets use.
_ALIASES = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana", "doge": "dogecoin", "xrp": "ripple",
            "fed": "federal", "gop": "republican", "potus": "president", "nyc": "new york"}


_COINS = {"btc": "Bitcoin", "eth": "Ethereum", "sol": "Solana", "doge": "Dogecoin", "xrp": "XRP"}


def expand(query: str) -> str:
    """Tickers as the names Polymarket's questions use ("BTC hit 150k" ->
    "Bitcoin hit 150k"), so its search ranks the price markets first."""
    return re.sub(r"\b(btc|eth|sol|doge)\b", lambda m: _COINS[m.group(1).lower()], query, flags=re.I)


def _has(word: str, have: set, text: str) -> bool:
    if word in have or (len(word) > 4 and word.endswith("s") and word[:-1] in have):
        return True
    if len(word) >= 5 and any(h.startswith(word) for h in have):
        return True
    alias = _ALIASES.get(word)
    return bool(alias) and (alias in have or alias in text)


def relevant(words: list, text: str, lenient: bool = False) -> bool:
    """True when the market's own text holds enough of the query's words:
    all of one or two, and more than half of three or more. A word also
    counts in its singular form (elections -> election) and a ticker as its
    name (btc -> bitcoin). `lenient` asks only for every distinctive word
    (not generic, not a number): "bitcoin price end of year" -> bitcoin."""
    if not words:
        return True
    text = _normal(text)
    have = set(_TOKEN.findall(text))
    if lenient:
        strong = [w for w in words if w not in _GENERIC and not any(c.isdigit() for c in w)]
        return bool(strong) and all(_has(w, have, text) for w in strong)
    found = sum(1 for w in words if _has(w, have, text))
    return found >= (len(words) if len(words) <= 2 else len(words) // 2 + 1)


class _Polymarket:
    id = "polymarket"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _get(self, path: str, params: dict) -> list:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(f"{_BASE}{path}", params=params,
                                            headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Polymarket timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Polymarket unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"Polymarket returned {response.status_code}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Polymarket rejected the read ({response.status_code})")
        data = response.json()
        return data if isinstance(data, list) else [data]

    @staticmethod
    def _num(value):
        """Polymarket serialises volume and liquidity as decimal strings
        ('20.0396'); the published contract promises a number or null."""
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _shape(self, market: dict) -> dict:
        outcomes = _as_list(market.get("outcomes"))
        prices = _as_list(market.get("outcomePrices"))
        implied = []
        for index, outcome in enumerate(outcomes):
            probability = None
            if index < len(prices):
                try:
                    probability = round(float(prices[index]) * 100, 2)
                except (TypeError, ValueError):
                    probability = None
            implied.append({"outcome": outcome, "probability_pct": probability})
        return {
            "id": market.get("id"),
            "question": market.get("question"),
            "slug": market.get("slug"),
            "active": market.get("active"),
            "closed": market.get("closed"),
            "end_date": market.get("endDate"),
            "volume": self._num(market.get("volume")),
            "liquidity": self._num(market.get("liquidity")),
            "implied_probabilities": implied,
        }

    async def search(self, query: Optional[str] = None, limit: int = 10,
                     active_only: bool = True) -> runtime.ProviderResult:
        limit = max(1, min(int(limit), 50))
        if query:
            # Polymarket's own search, which matches the whole catalogue. (Filtering
            # only the top markets by volume answered almost every topic with
            # nothing once sports lines took over the top of that list.)
            params = {"q": expand(query), "limit_per_type": 20, "search_profiles": "false", "search_tags": "false"}
            if active_only:
                params["events_status"] = "active"
            found = await self._get("/public-search", params)
            events = (found[0] or {}).get("events") or [] if found and isinstance(found[0], dict) else []
            # Polymarket's search is fuzzy: nonsense text still brings back
            # something ("xyzzy plumbus nonsense" -> a Consensys IPO market,
            # "nvidia earnings" -> Micron's, seen live 2026-09-28). Keep only
            # markets whose own words carry the query's key words.
            words = keywords(expand(query))
            candidates = [(m, " ".join(str(x or "") for x in (m.get("question"), m.get("slug"), e.get("title"),
                                                              e.get("slug"))))
                          for e in events if isinstance(e, dict) for m in (e.get("markets") or [])
                          if isinstance(m, dict) and (not active_only or (m.get("active") and not m.get("closed")))]
            markets = [m for m, text in candidates if relevant(words, text)]
            if not markets:
                markets = [m for m, text in candidates if relevant(words, text, lenient=True)]
            shaped = [self._shape(m) for m in markets]
            shaped.sort(key=lambda m: m.get("volume") or 0, reverse=True)
            shaped = shaped[:limit]
        else:
            # `volumeNum`, not `volume`: on /markets `volume` sorts as TEXT, so
            # "top by volume" came back as $100 weather bets and $1,000 soccer
            # lines (seen live 2026-09-28). /events sorts `volume` numerically.
            params = {"limit": limit, "order": "volumeNum", "ascending": "false"}
            if active_only:
                params.update({"active": "true", "closed": "false"})
            markets = await self._get("/markets", params)
            shaped = [self._shape(m) for m in markets if isinstance(m, dict)]
            shaped.sort(key=lambda m: m.get("volume") or 0, reverse=True)
        return runtime.ProviderResult(
            value={"query": query, "markets": shaped, "count": len(shaped)},
            cost_micros=0, cost_measured=True, usage=f"markets={len(shaped)}")

    async def by_slug(self, slug: str) -> runtime.ProviderResult:
        # `closed` must be sent explicitly: without it the list endpoint can
        # return a stale closed market for a slug that has since been reused,
        # so the caller is billed for the wrong market's prices.
        markets = await self._get("/markets", {"slug": slug, "closed": "false"})
        shaped = [self._shape(m) for m in markets if isinstance(m, dict)]
        if not shaped:
            raise runtime.InvalidRequest(f"No market found for slug '{slug}'.")
        return runtime.ProviderResult(
            value={"slug": slug, "market": shaped[0]},
            cost_micros=0, cost_measured=True, usage=f"slug={slug}")

    async def events(self, limit: int = 10, active_only: bool = True) -> runtime.ProviderResult:
        params = {"limit": max(1, min(int(limit), 50)), "order": "volume", "ascending": "false"}
        if active_only:
            params.update({"active": "true", "closed": "false"})
        data = await self._get("/events", params)
        events = [
            {"id": e.get("id"), "title": e.get("title"), "slug": e.get("slug"),
             "volume": self._num(e.get("volume")), "end_date": e.get("endDate"),
             "market_count": len(e.get("markets") or [])}
            for e in data if isinstance(e, dict)
        ]
        return runtime.ProviderResult(
            value={"events": events, "count": len(events)},
            cost_micros=0, cost_measured=True, usage=f"events={len(events)}")


PROVIDERS = [_Polymarket()]
PROVIDER = PROVIDERS[0]
