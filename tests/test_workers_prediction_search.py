"""market.prediction topic search uses Polymarket's own search.

It used to filter only the top-N markets by volume, so once sports lines
took over the top of that list, "bitcoin", "Trump" and "fed rates" all came
back empty (seen live 2026-09-28). Pinned on the shape /public-search
returned that day.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "workers"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location("wcag_audit_engine_workers", PKG / "__init__.py",
                                                  submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
PM = W.providers.polymarket

SEARCH = {"events": [
    {"title": "What price will Bitcoin hit in September?", "markets": [
        {"question": "Will Bitcoin reach $100,000 in September?", "slug": "will-bitcoin-reach-100k-in-september-2026",
         "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"0.003\", \"0.997\"]", "volume": "1482403.72",
         "liquidity": "133729.57", "active": True, "closed": False, "endDate": "2026-10-01T04:00:00Z"},
        {"question": "Will Bitcoin reach $80,000 in September?", "slug": "btc-80k-sep", "outcomes": "[\"Yes\", \"No\"]",
         "outcomePrices": "[\"0.2\", \"0.8\"]", "volume": "9000000", "liquidity": "1", "active": True, "closed": False},
        {"question": "Closed one", "slug": "closed", "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"1\", \"0\"]",
         "volume": "99999999", "active": False, "closed": True}]}],
    "pagination": {"hasMore": False}}


def test_a_topic_query_goes_to_polymarket_search_and_returns_open_markets_by_volume(monkeypatch):
    calls = []

    async def fake_get(self, path, params):
        calls.append((path, dict(params)))
        return [SEARCH]
    monkeypatch.setattr(type(PM.PROVIDERS[0]), "_get", fake_get)
    out = asyncio.run(PM.PROVIDERS[0].search("bitcoin", limit=5)).value
    assert calls[0][0] == "/public-search" and calls[0][1]["q"] == "bitcoin" and calls[0][1]["events_status"] == "active"
    assert [m["slug"] for m in out["markets"]] == ["btc-80k-sep", "will-bitcoin-reach-100k-in-september-2026"]
    assert out["count"] == 2


def test_no_query_still_reads_the_top_markets_by_volume(monkeypatch):
    calls = []

    async def fake_get(self, path, params):
        calls.append(path)
        return [SEARCH["events"][0]["markets"][0]]
    monkeypatch.setattr(type(PM.PROVIDERS[0]), "_get", fake_get)
    out = asyncio.run(PM.PROVIDERS[0].search(None, limit=3)).value
    assert calls == ["/markets"] and out["count"] == 1
