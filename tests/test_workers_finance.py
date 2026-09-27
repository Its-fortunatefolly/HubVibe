"""market.stock, market.fundamentals, finance.analytics.

The mathematics is pinned against closed forms and hand-computed series
(Black-Scholes-Merton reference case, put-call parity, drawdown on a known
path, quantile interpolation, Wilder RSI on a textbook series, EMA seeding);
the two market providers are parsed from captured responses of the real
endpoints (2026-09-26/27); fundamentals resolve alternates to the concept
with the most recent period and restatements to the latest filing; bad
input is refused before the gate; each route sells the same row over HTTP
and MCP; the static manifests match; and every result stamps checked_at.
"""

import asyncio
import importlib.util
import json
import math
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKERS = {"market.stock": ("/work/market/stock", "hubvibe_market_stock", 0.05),
           "market.fundamentals": ("/work/market/fundamentals", "hubvibe_market_fundamentals", 0.05),
           "finance.analytics": ("/work/finance/analytics", "hubvibe_finance_analytics", 0.50)}

# Captured 2026-09-26 from api.nasdaq.com (quote + historical) and query1.finance.yahoo.com.
NASDAQ_QUOTE = {"data": {"symbol": "AAPL", "companyName": "Apple Inc. Common Stock", "stockType": "Common Stock",
                         "exchange": "NASDAQ-GS", "isNasdaqListed": True, "isNasdaq100": True, "isHeld": False,
                         "assetClass": "STOCKS", "marketStatus": "Closed",
                         "primaryData": {"lastSalePrice": "$341.07", "netChange": "+5.15", "percentageChange": "+1.53%",
                                         "deltaIndicator": "up", "lastTradeTimestamp": "Sep 26, 2026 4:00 PM ET",
                                         "isRealTime": False, "bidPrice": "N/A", "askPrice": "N/A", "bidSize": "N/A",
                                         "askSize": "N/A", "volume": "30,002,768", "currency": None},
                         "secondaryData": None,
                         "keyStats": {"fiftyTwoWeekHighLow": {"label": "52 Week Range:", "value": "243.42 - 345.34"},
                                      "dayrange": {"label": "High/Low:", "value": "334.53 - 341.67"}}},
                "message": None, "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}
NASDAQ_HISTORY = {"data": {"symbol": "AAPL", "totalRecords": 2, "tradesTable": {
    "asOf": None, "headers": {"date": "Date", "close": "Close/Last", "volume": "Volume", "open": "Open", "high": "High", "low": "Low"},
    "rows": [{"date": "09/25/2026", "close": "$341.07", "volume": "30,002,510", "open": "$336.04", "high": "$341.67", "low": "$334.53"},
             {"date": "09/24/2026", "close": "$335.92", "volume": "24,733,100", "open": "$336.72", "high": "$338.91", "low": "$334.30"}]}},
    "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None}}
NASDAQ_UNKNOWN = {"data": None, "message": None,
                  "status": {"rCode": 400, "bCodeMessage": [{"code": 1001, "errorMessage": "Symbol not exists."}], "developerMessage": None}}
YAHOO_CHART = {"chart": {"result": [{"meta": {"currency": "USD", "symbol": "SPY", "exchangeName": "PCX", "fullExchangeName": "NYSEArca",
                                              "instrumentType": "ETF", "regularMarketPrice": 612.5, "regularMarketTime": 1790366401,
                                              "chartPreviousClose": 610.0, "previousClose": None, "longName": "SPDR S&P 500 ETF Trust",
                                              "shortName": "SPDR S&P 500", "regularMarketDayHigh": 613.2, "regularMarketDayLow": 609.1,
                                              "regularMarketVolume": 51000000},
                                     "timestamp": [1790193601, 1790280001, 1790366401],
                                     "indicators": {"quote": [{"open": [608.0, 609.5, 610.2], "high": [610.0, 611.0, 613.2],
                                                               "low": [606.5, 608.0, 609.1], "close": [609.0, 610.0, 612.5],
                                                               "volume": [40000000, 42000000, 51000000]}],
                                                    "adjclose": [{"adjclose": [609.0, 610.0, 612.5]}]}}],
                         "error": None}}
YAHOO_UNKNOWN = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}
COMPANY_FACTS = {"cik": 320193, "entityName": "Apple Inc.", "facts": {
    "dei": {"EntityCommonStockSharesOutstanding": {"label": "Entity Common Stock, Shares Outstanding", "units": {"shares": [
        {"end": "2025-10-17", "val": 14776353000, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31"}]}}},
    "us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            {"start": "2017-10-01", "end": "2018-09-29", "val": 265595000000, "fy": 2018, "fp": "FY", "form": "10-K", "filed": "2018-11-05"}]}},
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Revenue from Contract with Customer", "units": {"USD": [
            {"start": "2024-09-29", "end": "2025-09-27", "val": 416161000000, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31", "frame": "CY2025"},
            {"start": "2023-10-01", "end": "2024-09-28", "val": 391035000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-11-01"},
            # the same period reported twice: an original and a restated figure; the later filing wins
            {"start": "2023-10-01", "end": "2024-09-28", "val": 391000000000, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2024-10-30"},
            {"start": "2026-03-29", "end": "2026-06-27", "val": 109417000000, "fy": 2026, "fp": "Q3", "form": "10-Q", "filed": "2026-07-31", "frame": "CY2026Q2"}]}},
        "NetIncomeLoss": {"label": "Net Income (Loss)", "units": {"USD": [
            {"start": "2024-09-29", "end": "2025-09-27", "val": 112010000000, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2025-10-31"}]}},
    }}}


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


def _fin():
    return W.skills.finance


class _Ctx:
    """Serves each step from a fixture provider; records what ran."""

    def __init__(self, quotes=None, facts=None):
        self.quotes = quotes or {}
        self.facts = facts
        self.steps, self.providers_used, self.attempts = [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _Provider:
            id = "fixture"

            def available(self):
                return True

            async def quote(self, symbol, history_range=None):
                value = outer.quotes.get(symbol)
                if value is None:
                    raise W.runtime.InvalidRequest(f"`symbol` {symbol} is not known to the fixture.")
                return W.runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True)

            async def company_facts(self, cik):
                return W.runtime.ProviderResult(value=outer.facts, cost_micros=0, cost_measured=True)

        result = await call(_Provider())
        self.providers_used.append("fixture")
        return result.value


# --- the mathematics --------------------------------------------------------------

def test_black_scholes_matches_the_reference_case_and_put_call_parity():
    """Hull's standard example: S=100, K=100, r=5%, sigma=20%, T=1."""
    F = _fin()
    call = F.black_scholes("call", 100, 100, 0.05, 0.20, 1.0)
    put = F.black_scholes("put", 100, 100, 0.05, 0.20, 1.0)
    assert call["price"] == pytest.approx(10.450583572, abs=1e-9)
    assert put["price"] == pytest.approx(5.573526022, abs=1e-9)
    assert call["delta"] == pytest.approx(0.636830651, abs=1e-9)
    assert put["delta"] == pytest.approx(call["delta"] - 1.0, abs=1e-12)
    assert call["gamma"] == pytest.approx(0.018762017, abs=1e-9)
    assert call["vega"] == pytest.approx(37.524035, abs=1e-6)
    assert call["theta"] == pytest.approx(-6.414028, abs=1e-6)
    assert call["rho"] == pytest.approx(53.232482, abs=1e-6)
    assert call["d1"] == pytest.approx(0.35, abs=1e-12) and call["d2"] == pytest.approx(0.15, abs=1e-12)
    # put-call parity: C - P = S - K e^{-rT}
    assert (call["price"] - put["price"]) == pytest.approx(100 - 100 * math.exp(-0.05), abs=1e-12)
    # a continuous dividend yield lowers the call and raises the put
    with_q = F.black_scholes("call", 100, 100, 0.05, 0.20, 1.0, dividend_yield=0.03)
    assert with_q["price"] < call["price"]
    assert F.black_scholes("put", 100, 100, 0.05, 0.20, 1.0, dividend_yield=0.03)["price"] > put["price"]


def test_the_normal_quantile_is_the_standard_librarys():
    F = _fin()
    assert F.norm_ppf(0.05) == pytest.approx(-1.6448536269514722, abs=1e-12)
    assert F.norm_ppf(0.5) == 0.0
    assert F.norm_cdf(1.959963984540054) == pytest.approx(0.975, abs=1e-12)
    with pytest.raises(ValueError):
        F.norm_ppf(0.0)


def test_drawdown_on_a_known_path():
    F = _fin()
    dd = F.max_drawdown([100, 120, 90, 130, 80, 100, 135])
    assert dd["max_drawdown"] == pytest.approx(-50 / 130, abs=1e-15)
    assert dd == {"max_drawdown": dd["max_drawdown"], "peak_index": 3, "trough_index": 4,
                  "recovery_index": 6, "duration_periods": 1}
    flat = F.max_drawdown([100, 101, 102])
    assert flat["max_drawdown"] == 0.0 and flat["recovery_index"] is None
    never = F.max_drawdown([100, 50, 60])
    assert never["max_drawdown"] == -0.5 and never["recovery_index"] is None


def test_returns_volatility_sharpe_and_var_on_a_hand_computed_series():
    F = _fin()
    prices = [100.0, 110.0, 99.0, 108.9, 98.01]
    rets = F.simple_returns(prices)
    assert rets == pytest.approx([0.1, -0.1, 0.1, -0.1], abs=1e-12)
    assert F.log_returns(prices)[0] == pytest.approx(math.log(1.1), abs=1e-15)
    req = F.parse_analytics({"prices": prices, "periods_per_year": 4, "risk_free_rate": 0.0, "alpha": 0.25,
                             "metrics": ["returns", "volatility", "sharpe", "sortino", "var", "drawdown"]})
    out = F.compute(prices, req)["metrics"]
    # mean 0, sample std of [.1,-.1,.1,-.1] = sqrt(0.04/3)
    assert out["returns"]["mean_period_return"] == pytest.approx(0.0, abs=1e-15)
    assert out["returns"]["total_return"] == pytest.approx(98.01 / 100 - 1, abs=1e-12)
    assert out["returns"]["cagr"] == pytest.approx((0.9801) ** (1 / 1.0) - 1, abs=1e-12)  # 4 returns / 4 ppy = 1 year
    assert out["volatility"]["period_std"] == pytest.approx(math.sqrt(0.04 / 3), abs=1e-15)
    assert out["volatility"]["annualized"] == pytest.approx(math.sqrt(0.04 / 3) * 2, abs=1e-15)
    assert out["sharpe"] == pytest.approx(0.0, abs=1e-15)
    # historical VaR at alpha .25 with 4 returns: position (n-1)q = 0.75 -> -0.1 + 0.75*0 = -0.1 ... sorted [-.1,-.1,.1,.1]
    assert out["var"]["historical_var"] == pytest.approx(0.1, abs=1e-15)
    assert out["var"]["historical_cvar"] == pytest.approx(0.1, abs=1e-15)
    assert out["var"]["parametric_var"] == pytest.approx(-(0.0 + F.norm_ppf(0.25) * math.sqrt(0.04 / 3)), abs=1e-15)
    # peak 110 (index 1) to trough 98.01 (index 4)
    assert out["drawdown"]["max_drawdown"] == pytest.approx(98.01 / 110 - 1, abs=1e-12)
    assert out["drawdown"]["peak_index"] == 1 and out["drawdown"]["trough_index"] == 4


def test_quantile_interpolates_like_numpys_default():
    q = _fin()._quantile
    assert q([1, 2, 3, 4], 0.5) == 2.5
    assert q([1, 2, 3, 4], 0.25) == 1.75
    assert q([1, 2, 3, 4], 0.0) == 1 and q([1, 2, 3, 4], 1.0) == 4
    assert q([7], 0.3) == 7


def test_wilder_rsi_ema_seed_and_bollinger_bands():
    F = _fin()
    # A textbook RSI(14) series: 14 gains of 1 then a loss of 1 -> avg gain (13/14), avg loss (1/14) -> RS 13 -> RSI 92.857...
    prices = [100 + i for i in range(15)] + [113]
    assert F.rsi(prices, 14) == pytest.approx(100 - 100 / (1 + 13), abs=1e-12)
    assert F.rsi([100 + i for i in range(15)], 14) == 100.0  # no losses
    assert F.rsi([1, 2, 3], 14) is None
    # EMA seeded with the SMA of the first window, then alpha = 2/(w+1)
    assert F.ema([1, 2, 3, 4], 3) == pytest.approx(2.0 * 0.5 + 4 * 0.5, abs=1e-15)
    assert F.sma([1, 2, 3, 4], 2) == 3.5 and F.sma([1, 2], 3) is None
    b = F.bollinger([2, 4, 4, 4, 5, 5, 7, 9], 8, 2.0)  # population std of this set is exactly 2
    assert b["middle"] == 5.0 and b["upper"] == 9.0 and b["lower"] == 1.0
    assert b["percent_b"] == pytest.approx(1.0, abs=1e-15) and b["bandwidth"] == pytest.approx(8 / 5, abs=1e-15)


def test_beta_alpha_and_correlation_against_a_benchmark():
    F = _fin()
    bench = [100, 102, 101, 104, 103, 106]
    # twice the benchmark's moves -> beta 2, correlation 1
    prices = [100]
    for a, b in zip(bench, bench[1:]):
        prices.append(prices[-1] * (1 + 2 * (b / a - 1)))
    req = F.parse_analytics({"prices": prices, "benchmark_prices": bench, "metrics": ["beta"], "periods_per_year": 12})
    beta = F.compute(prices, req, bench)["metrics"]["beta"]
    assert beta["beta"] == pytest.approx(2.0, abs=1e-12)
    assert beta["correlation"] == pytest.approx(1.0, abs=1e-12)
    assert beta["alpha_annualized"] == pytest.approx(0.0, abs=1e-9)
    assert beta["n"] == 5


def test_kelly_fraction():
    k = _fin().kelly(0.6, 1.0)
    assert k["fraction"] == pytest.approx(0.2, abs=1e-15) and k["half_kelly"] == pytest.approx(0.1, abs=1e-15) and k["bet"]
    assert _fin().kelly(0.4, 1.0)["bet"] is False


def test_the_same_input_always_gives_the_same_numbers():
    F = _fin()
    body = W.catalog.example_for(W.catalog.BY_NAME["finance.analytics"])
    a = asyncio.run(F.analytics(_Ctx(), body))
    b = asyncio.run(F.analytics(_Ctx(), body))
    for key in ("returns", "volatility", "sharpe", "sortino", "drawdown", "var", "moving_averages", "rsi",
                "bollinger", "black_scholes", "kelly"):
        assert a[key] == b[key], key
    assert a["metrics_computed"] == ["returns", "volatility", "sharpe", "sortino", "drawdown", "var",
                                     "moving_averages", "rsi", "bollinger", "black_scholes", "kelly"]
    assert a["black_scholes"]["inputs"]["spot"] == body["prices"][-1]  # spot defaulted from the series
    assert a["checked_at"].endswith("Z") and a["as_of"] is None  # bare prices carry no date
    assert W.catalog.contract.check(W.catalog.BY_NAME["finance.analytics"].output_schema, a) is None


def test_analytics_over_a_symbol_uses_the_live_series_and_stamps_as_of():
    F = _fin()
    history = [{"date": f"2026-09-{d:02d}", "close": c} for d, c in zip(range(1, 11), [10, 11, 10.5, 12, 11.5, 13, 12.5, 14, 13.5, 15])]
    quote = {"symbol": "ZZZ", "price": 15.0, "as_of": "2026-09-10T20:00:00Z", "history": history, "history_range": "1mo"}
    ctx = _Ctx(quotes={"ZZZ": quote, "BBB": dict(quote, symbol="BBB")})
    r = asyncio.run(F.analytics(ctx, {"symbol": "zzz", "range": "1mo", "benchmark_symbol": "BBB", "metrics": ["returns", "beta"]}))
    assert ctx.steps == ["prices", "benchmark"]
    assert r["source"]["type"] == "symbol" and r["source"]["symbol"] == "ZZZ" and r["source"]["benchmark_symbol"] == "BBB"
    assert r["n"] == 10 and r["first_date"] == "2026-09-01" and r["last_date"] == "2026-09-10"
    assert r["as_of"] == "2026-09-10T20:00:00Z"
    assert r["beta"]["beta"] == pytest.approx(1.0, abs=1e-12)
    assert r["volatility"] is None and "beta" in r["metrics_computed"]


def test_bad_analytics_input_is_refused_before_any_fetch():
    F = _fin()
    for bad in ({}, {"prices": [1, 2], "symbol": "AAPL"}, {"prices": [1]}, {"prices": [1, -2]},
                {"prices": [1, 2], "metrics": ["nope"]}, {"prices": [1, 2], "alpha": 0.7},
                {"prices": [1, 2], "metrics": ["black_scholes"]},
                {"prices": [1, 2], "option": {"type": "swap", "strike": 1, "time_to_expiry_years": 1}},
                {"prices": [1, 2], "metrics": ["beta"]}, {"prices": [1, 2], "windows": {"rsi": 1}},
                {"symbol": "AAPL", "range": "9y"}, {"symbol": "not a symbol"}):
        with pytest.raises(W.runtime.InvalidRequest):
            F.precheck_analytics(bad)
    ok = F.parse_analytics({"prices": [{"date": "2026-01-01", "close": 1}, {"date": "2026-01-02", "close": 2}]})
    assert ok["prices"] == [1.0, 2.0] and ok["dates"] == ["2026-01-01", "2026-01-02"]


# --- market.stock: the two providers, parsed from captured responses ------------

def _stub_get(monkeypatch, module, responses):
    """responses: {url substring: (status, json)}"""
    async def fake(url, params=None):
        for key, value in responses.items():
            if key in url:
                return value
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(module, "_get_json", fake)


def test_nasdaq_quote_and_history_are_parsed_from_the_captured_shape(monkeypatch):
    eq = W.providers.equities
    _stub_get(monkeypatch, eq, {"/info": (200, NASDAQ_QUOTE), "/historical": (200, NASDAQ_HISTORY)})
    value = asyncio.run(eq.PROVIDERS[0].quote("AAPL", history_range="1mo")).value
    assert value["price"] == 341.07 and value["change"] == 5.15 and value["change_pct"] == 1.53
    assert value["volume"] == 30002768 and value["currency"] == "USD" and value["exchange"] == "NASDAQ-GS"
    assert value["day_low"] == 334.53 and value["day_high"] == 341.67
    assert value["as_of"] == "2026-09-26T16:00:00 America/New_York" and value["delayed_minutes"] == 15
    assert value["market_state"] == "Closed"
    assert [h["date"] for h in value["history"]] == ["2026-09-24", "2026-09-25"]  # oldest first
    assert value["history"][1] == {"date": "2026-09-25", "open": 336.04, "high": 341.67, "low": 334.53,
                                   "close": 341.07, "volume": 30002510}


def test_nasdaq_unknown_symbol_defers_to_the_fallback_provider(monkeypatch):
    eq = W.providers.equities
    _stub_get(monkeypatch, eq, {"/info": (200, NASDAQ_UNKNOWN)})
    with pytest.raises(W.runtime.PermanentProviderError):
        asyncio.run(eq.PROVIDERS[0].quote("SPY"))


def test_yahoo_chart_is_parsed_and_its_unknown_symbol_is_the_callers_error(monkeypatch):
    eq = W.providers.equities
    _stub_get(monkeypatch, eq, {"/chart/SPY": (200, YAHOO_CHART), "/chart/ZZZZQ": (404, YAHOO_UNKNOWN)})
    value = asyncio.run(eq.PROVIDERS[1].quote("SPY", history_range="5d")).value
    assert value["price"] == 612.5 and value["previous_close"] == 610.0
    assert value["change"] == pytest.approx(2.5, abs=1e-9) and value["change_pct"] == pytest.approx(2.5 / 610 * 100, abs=1e-4)
    assert value["name"] == "SPDR S&P 500 ETF Trust" and value["asset_class"] == "etf" and value["currency"] == "USD"
    assert value["as_of"] == "2026-09-25T20:00:01Z" and value["delayed_minutes"] is None
    assert [h["close"] for h in value["history"]] == [609.0, 610.0, 612.5]
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(eq.PROVIDERS[1].quote("ZZZZQ"))


def test_nasdaq_timestamp_parsing():
    parse = W.providers.equities._nasdaq_as_of
    assert parse("Sep 24, 2026") == "2026-09-24"
    assert parse("Sep 26, 2026 4:00 PM ET") == "2026-09-26T16:00:00 America/New_York"
    assert parse("") is None and parse(None) is None


def test_market_stock_skill_shapes_the_result(monkeypatch):
    quote = {"symbol": "AAPL", "name": "Apple Inc.", "price": 341.07, "as_of": "2026-09-25", "delayed_minutes": 15,
             "history": [{"date": "2026-09-25", "close": 341.07}], "history_range": "1mo", "currency": "USD"}
    ctx = _Ctx(quotes={"AAPL": quote})
    r = asyncio.run(_fin().stock(ctx, {"symbol": "aapl", "range": "1mo"}))
    assert r["symbol"] == "AAPL" and r["price"] == 341.07 and r["history_rows"] == 1 and r["source"] == "fixture"
    assert r["checked_at"].endswith("Z")
    assert W.catalog.contract.check(W.catalog.BY_NAME["market.stock"].output_schema, r) is None
    for bad in ({}, {"symbol": "A" * 13}, {"symbol": "AAPL", "range": "7y"}, {"symbol": "AAPL", "include_history": "no"}):
        with pytest.raises(W.runtime.InvalidRequest):
            _fin().precheck_stock(bad)


# --- market.fundamentals ------------------------------------------------------------

def test_fundamentals_prefer_the_current_alternate_and_the_latest_filing(monkeypatch):
    F = _fin()

    async def fake_cik(self, symbol):
        return {"cik": 320193, "title": "Apple Inc."} if symbol == "AAPL" else None
    monkeypatch.setattr(W.providers.sec_edgar._SecEdgar, "cik_for", fake_cik)
    ctx = _Ctx(facts=COMPANY_FACTS)
    r = asyncio.run(F.fundamentals(ctx, {"symbol": "AAPL", "periods": 2, "concepts": ["Revenues", "NetIncomeLoss", "EntityCommonStockSharesOutstanding", "Liabilities"]}))
    assert ctx.steps == ["company_facts"]
    rev = r["concepts"]["Revenues"]
    assert rev["concept"] == "RevenueFromContractWithCustomerExcludingAssessedTax"  # not the 2018 `Revenues`
    assert [v["end"] for v in rev["values"]] == ["2026-06-27", "2025-09-27"]  # newest first, 2 periods
    assert r["concepts"]["EntityCommonStockSharesOutstanding"]["taxonomy"] == "dei"
    assert r["concepts_missing"] == ["Liabilities"]
    assert r["entity_name"] == "Apple Inc." and r["cik"] == 320193 and r["as_of"] == "2026-07-31"
    assert r["source"] == "sec-edgar-xbrl-companyfacts" and r["checked_at"].endswith("Z")
    assert W.catalog.contract.check(W.catalog.BY_NAME["market.fundamentals"].output_schema, r) is None
    # annual only, 3 periods: the restated FY2024 figure (later filing) is the one returned
    r = asyncio.run(F.fundamentals(_Ctx(facts=COMPANY_FACTS), {"symbol": "AAPL", "periods": 3, "forms": ["10-k"], "concepts": ["Revenues"]}))
    values = r["concepts"]["Revenues"]["values"]
    assert [(v["end"], v["value"]) for v in values] == [("2025-09-27", 416161000000), ("2024-09-28", 391035000000)]
    assert r["forms"] == ["10-K"]


def test_fundamentals_input_is_validated_before_the_gate():
    F = _fin()
    for bad in ({}, {"symbol": "A" * 13}, {"cik": "abc"}, {"cik": 0}, {"symbol": "AAPL", "concepts": []},
                {"symbol": "AAPL", "periods": 0}, {"symbol": "AAPL", "forms": "10-K"}):
        with pytest.raises(W.runtime.InvalidRequest):
            F.precheck_fundamentals(bad)
    assert F._parse_fundamentals({"cik": "0000320193"})["cik"] == 320193


def test_an_unlisted_ticker_is_the_callers_error(monkeypatch):
    async def fake_cik(self, symbol):
        return None
    monkeypatch.setattr(W.providers.sec_edgar._SecEdgar, "cik_for", fake_cik)
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(_fin().fundamentals(_Ctx(facts=COMPANY_FACTS), {"symbol": "ZZZZQ"}))


# --- the routes, the tools, the contract -----------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_finance", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
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


def test_the_three_rows_are_priced_and_keyless_and_always_current():
    for name, (path, tool, price) in WORKERS.items():
        w = W.catalog.BY_NAME[name]
        assert w.path == path and w.price_usd == price
        assert w.available(), f"{name} must be deliverable with no credential"
        schema = W.catalog.contract.OUTPUT_SCHEMAS[name]
        assert "checked_at" in schema["required"] and schema["properties"]["checked_at"]["format"] == "date-time"
        assert "as_of" in schema["properties"], name
        assert W.skills.PRECHECKS.get(w.skill) is not None, name
        jsonschema.validate(W.catalog.example_for(w), w.input_schema)


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    for path, body in (("/work/market/stock", {"symbol": "AAPL", "range": "9y"}),
                       ("/work/market/fundamentals", {"periods": 3}),
                       ("/work/finance/analytics", {"prices": [1]})):
        response = client.post(path, headers={"X-API-Key": "test-key"}, json=body)
        assert response.status_code == 400, (path, response.text)
        assert response.json()["reason"] == "invalid_request" and response.json()["billed"] is False


def test_unpaid_calls_are_402s_at_the_catalog_prices(client):
    for name, (path, tool, price) in WORKERS.items():
        response = client.post(path, json=W.catalog.example_for(W.catalog.BY_NAME[name]))
        assert response.status_code == 402, path
        accepts = response.json()["accepts"][0]
        assert int(accepts.get("maxAmountRequired") or accepts.get("amount")) == round(price * 1_000_000)


def test_a_paid_analytics_call_over_http_and_mcp_delivers_the_envelope(client):
    body = W.catalog.example_for(W.catalog.BY_NAME["finance.analytics"])
    response = client.post("/work/finance/analytics", headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == "finance.analytics" and envelope["price_usd"] == 0.50
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME["finance.analytics"]))
    assert envelope["result"]["black_scholes"]["type"] == "call"
    assert envelope["provenance"]["attempts"] == 0  # pure arithmetic: no provider ran
    receipt = client.get(envelope["receipt_url"]).json()
    assert receipt["request"]["worker"] == "finance.analytics" and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "hubvibe_finance_analytics", "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["result"]["kelly"]["fraction"] == pytest.approx(0.55 - 0.45 / 1.5, abs=1e-12)


def test_the_tools_are_listed_and_the_static_manifests_match(app_module, client):
    live = {t["name"]: t for t in app_module._mcp_tools()}
    manifest = {t["name"]: t for t in client.get("/mcp.json").json()["tools"]}
    for name, (path, tool, price) in WORKERS.items():
        worker = W.catalog.BY_NAME[name]
        assert live[tool]["title"] == worker.title and f"${price:.2f} per call" in live[tool]["description"]
        assert live[tool]["outputSchema"] == W.catalog.response_schema(worker)
        assert manifest[tool]["httpEndpoint"] == {"method": "POST", "path": path, "price_usd": price}
        for static in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
            on_disk = next(t for t in json.loads(static.read_text())["tools"] if t["name"] == tool)
            for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
                assert on_disk[field] == live[tool][field], f"{static.name} {tool} {field} is stale"
