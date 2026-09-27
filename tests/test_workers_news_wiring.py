"""Headlines pumped into the composites: market.intel and research.company
read current news for their subject and hand it to the analysis as
evidence, disclosed in the result. A feed that fails never sinks the job.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
C = W.skills.composites

HEADLINES = [{"title": "Bitcoin steadies as ETF inflows resume", "url": "https://news.google.com/rss/articles/a",
              "source_name": "Reuters", "source_url": "https://www.reuters.com", "published_at": "2026-09-27T09:00:00Z",
              "summary": None, "feed": "google_news"}]


def _check_news_contract(worker, result):
    schema = W.catalog.contract.OUTPUT_SCHEMAS[worker]
    assert "news" in schema["required"] and "news_note" in schema["required"]
    jsonschema.validate(result["news"], schema["properties"]["news"])
    jsonschema.validate(result["news_note"], schema["properties"]["news_note"])


def _fakes(monkeypatch, captured, news_result=None, news_error=None):
    async def fake_quote(ctx, payload):
        return {"product_id": payload["product_id"], "price": 84473.0, "quote_currency": "USD",
                "price_change_24h_pct": 1.2, "volume_24h": 1000.0}

    async def fake_markets(ctx, payload):
        return {"count": 1, "markets": [{"question": "BTC above 100k by Dec 31?",
                                         "implied_probabilities": [{"outcome": "Yes", "probability_pct": 40.0}]}]}

    async def fake_analyze(ctx, payload):
        captured["text"] = payload["text"]
        captured["language"] = payload.get("language")
        return {"answer": "analysis", "model": "fake"}

    async def fake_news(ctx, payload):
        captured["news_payload"] = payload
        if news_error is not None:
            raise news_error
        return news_result

    async def fake_sources(ctx, query, max_sources):
        return ([{"url": "https://a.example", "title": "A", "text": "Acme makes widgets"}], [])

    monkeypatch.setattr(W.skills.market, "quote", fake_quote)
    monkeypatch.setattr(W.skills.market, "prediction_markets", fake_markets)
    monkeypatch.setattr(W.skills.llm, "analyze", fake_analyze)
    monkeypatch.setattr(W.skills.news, "search", fake_news)
    monkeypatch.setattr(C, "_gather_sources", fake_sources)


def test_market_intel_reads_headlines_for_the_asset_and_hands_them_to_the_analysis(monkeypatch):
    captured = {}
    _fakes(monkeypatch, captured, news_result={"articles": HEADLINES, "notes": []})
    result = asyncio.run(C.market_intel(None, {"product_id": "BTC-USD", "language": "ja"}))
    assert captured["news_payload"] == {"query": "Bitcoin", "language": "ja", "limit": C.NEWS_LIMIT, "since_hours": 72}
    assert result["news"] == [{"title": HEADLINES[0]["title"], "url": HEADLINES[0]["url"], "source_name": "Reuters",
                               "published_at": "2026-09-27T09:00:00Z"}]
    assert result["news_note"] is None and "Recent headlines" in captured["text"] and "Reuters" in captured["text"]
    _check_news_contract("market.intel", result)


def test_market_intel_uses_the_query_as_the_news_topic_and_survives_a_dead_feed(monkeypatch):
    captured = {}
    _fakes(monkeypatch, captured, news_error=W.runtime.TransientProviderError("Google News (US:en) timed out"))
    result = asyncio.run(C.market_intel(None, {"product_id": "ETH-USD", "query": "Ethereum ETF"}))
    assert captured["news_payload"]["query"] == "Ethereum ETF"
    assert result["news"] == [] and result["news_note"].startswith("Headlines unavailable for this call")
    assert "Headlines unavailable" in captured["text"]
    _check_news_contract("market.intel", result)


def test_market_intel_survives_a_programming_error_in_the_feed_and_says_so(monkeypatch):
    captured = {}
    _fakes(monkeypatch, captured, news_error=TypeError("boom"))
    result = asyncio.run(C.market_intel(None, {"product_id": "SOL-USD"}))
    assert result["news"] == [] and result["news_note"] == "Headlines unavailable for this call: TypeError"


def test_research_company_reads_a_month_of_headlines_and_keeps_them_apart_from_cited_sources(monkeypatch):
    captured = {}
    _fakes(monkeypatch, captured, news_result={"articles": HEADLINES, "notes": ["Edition US:xx returned nothing; en/US used."]})
    result = asyncio.run(C.research_company(None, {"company": "Acme", "language": "de"}))
    assert captured["news_payload"] == {"query": "Acme", "language": "de", "limit": C.NEWS_LIMIT, "since_hours": 720}
    assert result["news"][0]["title"] == HEADLINES[0]["title"] and result["news_note"].startswith("Edition")
    assert "not numbered sources" in captured["text"] and result["sources"][0]["n"] == 1
    _check_news_contract("research.company", result)


def test_no_matching_headlines_is_disclosed_not_hidden(monkeypatch):
    captured = {}
    _fakes(monkeypatch, captured, news_result={"articles": [], "notes": []})
    result = asyncio.run(C.market_intel(None, {"product_id": "DOGE-USD"}))
    assert result["news"] == [] and result["news_note"] == "No headlines matched 'Dogecoin' in the last 72 hours."
