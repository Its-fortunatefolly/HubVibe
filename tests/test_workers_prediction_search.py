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
        calls.append((path, dict(params)))
        return [SEARCH["events"][0]["markets"][0], SEARCH["events"][0]["markets"][1]]
    monkeypatch.setattr(type(PM.PROVIDERS[0]), "_get", fake_get)
    out = asyncio.run(PM.PROVIDERS[0].search(None, limit=3)).value
    assert [c[0] for c in calls] == ["/markets"] and out["count"] == 2
    # `volume` sorts as text on /markets ($100 weather bets first, seen live
    # 2026-09-28); `volumeNum` is the numeric field.
    assert calls[0][1]["order"] == "volumeNum" and calls[0][1]["ascending"] == "false"
    assert [m["slug"] for m in out["markets"]] == ["btc-80k-sep", "will-bitcoin-reach-100k-in-september-2026"]


# --- never sold empty, never sold unrelated (2026-09-28) -----------------------
#
# The one repeat buyer paid $0.05 four times on 2026-09-25/26 and got zero
# markets each time; and Polymarket's fuzzy search answers nonsense with
# something ("xyzzy plumbus nonsense" -> a Consensys IPO market, seen live).

import pytest  # noqa: E402

NOISE = {"events": [
    {"title": "Consensys IPO by ___ ?", "slug": "consensys-ipo", "markets": [
        {"question": "Will Consensys IPO by September 30 2026?", "slug": "consensys-ipo-sep", "outcomes": "[\"Yes\", \"No\"]",
         "outcomePrices": "[\"0.1\", \"0.9\"]", "volume": "5000", "active": True, "closed": False}]},
    SEARCH["events"][0]]}


class _Ctx:
    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        return (await call(providers[0])).value


def _serve(monkeypatch, pages):
    seen = []

    async def fake_get(self, path, params):
        seen.append((path, dict(params)))
        return pages(path, params)
    monkeypatch.setattr(type(PM.PROVIDERS[0]), "_get", fake_get)
    return seen


def test_keywords_and_relevance_rules():
    assert PM.keywords("Will the price of Ethereum be above $5,000?") == ["price", "ethereum", "above", "5000"]
    assert PM.relevant(["fed", "rate", "cut"], "Will no Fed rate cuts happen in 2026?")
    assert not PM.relevant(["xyzzy", "plumbus", "nonsense"], "Will Consensys IPO by September 30 2026?")
    assert not PM.relevant(["2026", "world", "cup", "win"], "Will Ciryl Gane be the UFC Heavyweight Champion on December 31, 2026?")
    assert PM.relevant(["bitcoin", "price", "end", "year"], "Will Bitcoin dip to $45,000 by December 31, 2026?", lenient=True)
    assert not PM.relevant(["2026", "world", "cup", "win"], "UFC Heavyweight Champion 2026", lenient=True)
    assert PM.expand("Will BTC hit 150k") == "Will Bitcoin hit 150k" and PM.expand("subtle") == "subtle"


def test_unrelated_markets_are_dropped_and_tickers_become_names(monkeypatch):
    seen = _serve(monkeypatch, lambda path, params: [NOISE])
    out = asyncio.run(PM.PROVIDERS[0].search("BTC", limit=5)).value
    assert seen[0][1]["q"] == "Bitcoin"
    assert [m["slug"] for m in out["markets"]] == ["btc-80k-sep", "will-bitcoin-reach-100k-in-september-2026"]


def test_a_topic_nothing_matches_is_refused_free_not_sold_empty(monkeypatch):
    _serve(monkeypatch, lambda path, params: [NOISE])
    with pytest.raises(W.runtime.InvalidRequest) as refused:
        asyncio.run(W.skills.market.prediction_markets(_Ctx(), {"query": "xyzzy plumbus nonsense"}))
    assert "Nothing was charged" in str(refused.value)
    # market.intel sells spot price and news beside the markets: an empty list is fine there.
    out = asyncio.run(W.skills.market.prediction_markets(_Ctx(), {"query": "xyzzy plumbus nonsense"}, allow_empty=True))
    assert out["count"] == 0


def test_an_empty_answer_with_no_topic_is_a_provider_failure_not_a_sale(monkeypatch):
    _serve(monkeypatch, lambda path, params: [])
    with pytest.raises(W.runtime.TransientProviderError):
        asyncio.run(W.skills.market.prediction_markets(_Ctx(), {}))
    with pytest.raises(W.runtime.TransientProviderError):
        asyncio.run(W.skills.market.prediction_events(_Ctx(), {}))


def test_over_http_a_topic_nothing_matches_is_a_free_400(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd")
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    main_path = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("wcag_audit_main_prediction_empty", main_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workers = module.workers
    workers.ledger.reset_for_tests()
    workers.runtime.reset_breakers()
    workers.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)

    async def fake_get(self, path, params):
        return [NOISE]
    monkeypatch.setattr(type(workers.providers.polymarket.PROVIDERS[0]), "_get", fake_get)
    response = TestClient(module.app).post("/work/market/prediction", headers={"X-API-Key": "test-key"},
                                           json={"query": "xyzzy plumbus nonsense"})
    assert response.status_code == 400, response.text
    assert response.json()["billed"] is False and "Nothing was charged" in response.json()["detail"]
    workers.ledger.reset_for_tests()
