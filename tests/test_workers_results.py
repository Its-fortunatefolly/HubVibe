"""search.results -- Brave web and news results in any language and country.

Pinned: the real Brave web response captured 2026-09-27 (JP market, ja
language) normalised into results with site name, hostname, age and
extra snippets; the news fallback call; parameter mapping (country,
search_lang/ui_lang, freshness, safesearch); an Answers-plan key (no
result blocks) refused as unavailable, never sold as empty; input refused
before the gate; fail-closed without a key; paid HTTP + MCP; manifests.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "search.results", "/work/search/results", "hubvibe_search_results"

BRAVE_WEB = json.loads(r"""{"type": "search", "query": {"original": "tokyo weather forecast", "country": "jp", "more_results_available": true}, "web": {"type": "search", "results": [{"title": "Tokyo, Tokyo, Japan Weather Forecast | AccuWeather", "url": "https://www.accuweather.com/en/jp/tokyo/226396/weather-forecast/226396", "description": "Tokyo, Tokyo, Japan Weather Forecast, with current conditions, wind, air quality, and what to expect for the next 3 days.", "age": "4 日前", "page_age": "2026-09-23T10:38:54", "language": "en", "family_friendly": true, "meta_url": {"scheme": "https", "netloc": "accuweather.com", "hostname": "www.accuweather.com"}, "profile": {"name": "AccuWeather", "url": "https://www.accuweather.com/en/jp/tokyo/226396/weather-forecast/226396", "long_name": "accuweather.com"}, "extra_snippets": ["Create Your Account Unlock extended daily and hourly forecasts — all with your free account. ... Tokyo, Tokyo Weather Today WinterCast Local {stormName} Tracker Hourly 10-Day Radar MinuteCast® Monthly Air Quality Health & Activities", "For Business For Partners For Advertising AccuWeather APIs AccuWeather Connect Personal Weather Stations"]}, {"title": "Tokyo, Japan 14 day weather forecast", "url": "https://www.timeanddate.com/weather/japan/tokyo/ext", "description": "Forecasted weather conditions the coming 2 weeks for Tokyo", "age": "6 日前", "page_age": "2026-09-22T00:58:03", "language": "en", "family_friendly": true, "meta_url": {"scheme": "https", "netloc": "timeanddate.com", "hostname": "www.timeanddate.com"}, "profile": {"name": "TimeAndDate", "url": "https://www.timeanddate.com/weather/japan/tokyo/ext", "long_name": "timeanddate.com"}, "extra_snippets": ["Currently: 70 °F. Sprinkles. Overcast. (Weather station: Tokyo, Japan)."]}, {"title": "Tokyo - BBC Weather", "url": "https://www.bbc.com/weather/1850147", "description": "14-day weather forecast for Tokyo.", "age": "6 日前", "page_age": "2026-09-22T04:58:03", "language": "en", "family_friendly": true, "meta_url": {"scheme": "https", "netloc": "bbc.com", "hostname": "www.bbc.com"}, "profile": {"name": "BBC", "url": "https://www.bbc.com/weather/1850147", "long_name": "British Broadcasting Corporation"}, "extra_snippets": ["BBC Weather · Search for a location · 14-day forecast · Red Met Office Weather WarningsAmber Met Office Weather WarningsYellow Met Office Weather Warnings · Last updated today at 15:00 · Today · , Light rain showers and light winds · Light Rain Showers ·", "14-day weather forecast for Tokyo."]}]}}""")
BRAVE_NEWS = json.loads(r"""{"type": "news", "results": [{"title": "Typhoon nears Tokyo", "url": "https://example.com/news/1", "description": "A typhoon...", "age": "2 hours ago", "page_age": "2026-09-27T09:00:00", "breaking": false, "meta_url": {"hostname": "example.com"}, "source": "Example News"}]}""")


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
R = W.skills.results
B = W.providers.brave


def _run(coro):
    return asyncio.run(coro)


class _Ctx:
    def __init__(self, web=None, news=None, news_error=None):
        self.web = web if web is not None else {"web": BRAVE_WEB["web"]["results"], "news": [], "more": True, "country": "jp", "altered": None}
        self.news = news if news is not None else {"news": BRAVE_NEWS["results"]}
        self.news_error = news_error
        self.calls = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        outer = self

        class _P:
            id = providers[0].id

            async def web(self, query, count=10, country=None, language=None, freshness=None, safesearch="moderate"):
                outer.calls.append(("web", query, count, country, language, freshness, safesearch))
                return W.runtime.ProviderResult(value=outer.web, cost_micros=5000, cost_measured=True)

            async def news(self, query, count=10, country=None, language=None, freshness=None, safesearch="moderate"):
                outer.calls.append(("news", query, count, country, language, freshness, safesearch))
                if outer.news_error:
                    raise outer.news_error
                return W.runtime.ProviderResult(value=outer.news, cost_micros=5000, cost_measured=True)

        return (await call(_P())).value


def test_input_is_validated_before_the_gate_and_params_map_to_brave():
    ok = R.parse({"query": "  東京  天気予報 ", "country": "jp", "language": "ja", "freshness": "week"})
    assert ok["query"] == "東京 天気予報" and ok["country"] == "JP" and ok["news"] is True and ok["count"] == 10
    params = B._params("x", 5, "jp", "pt-BR", "week", "strict")
    assert params == {"q": "x", "count": "5", "safesearch": "strict", "country": "JP", "search_lang": "pt", "ui_lang": "pt-BR", "freshness": "pw"}
    assert B._params("x", 99, None, "ja", None, "off")["count"] == "20" and "ui_lang" not in B._params("x", 1, None, "ja", None, "off")
    assert B._params("x", 1, "jp", "ja", None, "off")["ui_lang"] == "ja-JP"
    for bad in ({}, {"query": " "}, {"query": "x" * 401}, {"query": "a", "country": "JPN"}, {"query": "a", "count": 0},
                {"query": "a", "count": 21}, {"query": "a", "freshness": "hour"}, {"query": "a", "news": "yes"},
                {"query": "a", "safesearch": "none"}, {"query": "a", "language": "not a tag!"}):
        with pytest.raises(W.runtime.InvalidRequest):
            R.parse(bad)


def test_real_web_results_are_normalised_and_news_comes_from_its_own_call_when_the_web_call_has_none():
    ctx = _Ctx()
    r = _run(R.results(ctx, {"query": "東京 天気予報", "country": "JP", "language": "ja", "count": 3}))
    assert ctx.calls[0] == ("web", "東京 天気予報", 3, "JP", "ja", None, "moderate") and ctx.calls[1][0] == "news"
    assert r["web_count"] == 3 and r["web"][0]["title"].startswith("Tokyo") and r["web"][0]["hostname"] == "www.accuweather.com"
    assert r["web"][0]["site_name"] and r["web"][0]["url"].startswith("https://") and r["web"][0]["page_age"]
    assert r["news_count"] == 1 and r["news"][0] == {"title": "Typhoon nears Tokyo", "url": "https://example.com/news/1", "description": "A typhoon...",
                                                     "age": "2 hours ago", "page_age": "2026-09-27T09:00:00", "source": "Example News", "breaking": False}
    assert r["more_results_available"] is True and r["country"] == "JP" and r["notes"] == [] and r["checked_at"].endswith("Z")
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_news_off_skips_the_second_call_and_a_dead_news_call_is_disclosed():
    ctx = _Ctx()
    r = _run(R.results(ctx, {"query": "x402", "news": False}))
    assert [c[0] for c in ctx.calls] == ["web"] and r["news"] == [] and r["news_count"] == 0
    ctx = _Ctx(news_error=W.runtime.TransientProviderError("Brave news search timed out"))
    r = _run(R.results(ctx, {"query": "x402"}))
    assert r["news"] == [] and r["notes"][0].startswith("News results unavailable")
    r = _run(R.results(_Ctx(web={"web": [], "news": [], "more": False, "country": None, "altered": "x 402"}, news={"news": []}), {"query": "x402"}))
    assert r["web"] == [] and "No result matched the query." in r["notes"] and any("altered" in n for n in r["notes"])
    jsonschema.validate(r, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_the_provider_is_fail_closed_without_a_key(monkeypatch):
    provider = B.PROVIDERS[0]
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    assert provider.available() is False and "not set" in provider.unavailable_reason()
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAtest")
    assert provider.available() is True and W.catalog.BY_NAME[WORKER].available()


def test_an_answers_plan_response_without_result_blocks_is_unavailable_not_empty(monkeypatch):
    async def fake_get(path, params, what):
        return {"type": "search", "query": {"original": "x"}}
    monkeypatch.setattr(B, "_get", fake_get)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAtest")
    with pytest.raises(W.runtime.ProviderUnavailable):
        _run(B.PROVIDERS[0].web("x"))


# --- the route, the tool, the contract ---------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "BSAtest")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_results", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.25 and w.tier == "standard" and w.requires == ["brave"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None
    assert WORKER in W.catalog.NATIVE_LANGUAGE_WORKERS


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"query": "a", "country": "JPN"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.results.results(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"query": "東京 天気予報", "country": "JP", "language": "ja", "count": 3}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 250_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.25 and envelope["result"]["web_count"] == 3
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["news_count"] == 1


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.25}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
