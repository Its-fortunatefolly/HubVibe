"""Markets and trading mathematics: three workers.

  market.stock        -- a live equity quote and daily history (keyless
                         Nasdaq data API, Yahoo chart fallback)
  market.fundamentals -- the filer's own reported numbers from SEC EDGAR
                         XBRL company facts (official, keyless)
  finance.analytics   -- deterministic trading mathematics over a price
                         series: returns, volatility, Sharpe and Sortino,
                         drawdown, VaR/CVaR, beta/alpha/correlation, moving
                         averages, RSI, Bollinger bands, Black-Scholes with
                         Greeks, Kelly. Pure arithmetic with exactly rounded
                         sums (math.fsum); no LLM anywhere in the path.

ALWAYS CURRENT: every market read is a fresh request and carries `as_of`
(the source's own timestamp) and `checked_at` (when this node read it).
THE MATHEMATICS IS THE PRODUCT: every formula is named in `method`, every
number is a function of the inputs alone, and the reference values the
tests pin (Black-Scholes closed forms, hand-computed series) are the
contract.
"""

import math
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Optional

from .. import runtime
from ..providers import equities, sec_edgar

MAX_PRICES = 100_000
MIN_PRICES = 2
METRICS = ("returns", "volatility", "sharpe", "sortino", "drawdown", "var", "beta",
           "moving_averages", "rsi", "bollinger", "black_scholes", "kelly")
DEFAULT_METRICS = ("returns", "volatility", "sharpe", "sortino", "drawdown", "var",
                   "moving_averages", "rsi", "bollinger")

# The XBRL concepts a buyer most often wants, with the alternates filers use.
DEFAULT_CONCEPTS = ["Revenues", "NetIncomeLoss", "EarningsPerShareDiluted", "OperatingIncomeLoss",
                    "Assets", "Liabilities", "StockholdersEquity",
                    "CashAndCashEquivalentsAtCarryingValue", "EntityCommonStockSharesOutstanding"]
CONCEPT_ALTERNATES = {
    "Revenues": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "StockholdersEquity": ["StockholdersEquity",
                           "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}
MAX_CONCEPTS = 25
MAX_PERIODS = 40


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- market.stock ---------------------------------------------------------------

def _parse_stock(payload: dict) -> dict:
    symbol = equities.validate_symbol(payload.get("symbol"))
    include_history = payload.get("include_history", True)
    if not isinstance(include_history, bool):
        raise runtime.InvalidRequest("`include_history`, when given, must be true or false.")
    history_range = equities.validate_range(payload.get("range")) if include_history else None
    return {"symbol": symbol, "history_range": history_range}


def precheck_stock(payload: dict) -> None:
    _parse_stock(payload)


async def _quote(ctx, symbol: str, history_range: Optional[str], step: str = "quote") -> dict:
    async def call(provider):
        return await provider.quote(symbol, history_range=history_range)

    # A stalled primary must not spend the fallback's time: one short retry,
    # then the next provider.
    return await ctx.run(step, equities.PROVIDERS, call, per_attempt_seconds=12, max_attempts=2)


async def stock(ctx, payload: dict) -> dict:
    req = _parse_stock(payload)
    value = await _quote(ctx, req["symbol"], req["history_range"])
    source = (ctx.providers_used[-1] if getattr(ctx, "providers_used", None) else None)
    return {
        "symbol": value["symbol"],
        "name": value.get("name"),
        "exchange": value.get("exchange"),
        "asset_class": value.get("asset_class"),
        "currency": value.get("currency"),
        "price": value["price"],
        "change": value.get("change"),
        "change_pct": value.get("change_pct"),
        "previous_close": value.get("previous_close"),
        "day_high": value.get("day_high"),
        "day_low": value.get("day_low"),
        "volume": value.get("volume"),
        "market_state": value.get("market_state"),
        "as_of": value.get("as_of"),
        "delayed_minutes": value.get("delayed_minutes"),
        "history_range": value.get("history_range"),
        "history": value.get("history") or [],
        "history_rows": len(value.get("history") or []),
        "source": source,
        "checked_at": _now(),
    }


# --- market.fundamentals --------------------------------------------------------

def _parse_fundamentals(payload: dict) -> dict:
    symbol = payload.get("symbol")
    cik = payload.get("cik")
    if symbol is None and cik is None:
        raise runtime.InvalidRequest("Give `symbol` (a US ticker) or `cik` (the SEC's Central Index Key).")
    if symbol is not None:
        symbol = equities.validate_symbol(symbol)
    if cik is not None:
        if isinstance(cik, bool) or not isinstance(cik, (int, str)):
            raise runtime.InvalidRequest("`cik` must be a whole number or its digit string.")
        try:
            cik = int(str(cik).strip())
        except ValueError:
            raise runtime.InvalidRequest("`cik` must be a whole number or its digit string.") from None
        if not 1 <= cik <= 9_999_999_999:
            raise runtime.InvalidRequest("`cik` is out of range.")
    concepts = payload.get("concepts")
    if concepts is None:
        concepts = list(DEFAULT_CONCEPTS)
    elif (not isinstance(concepts, list) or not concepts or len(concepts) > MAX_CONCEPTS
          or not all(isinstance(c, str) and c.strip() and len(c) <= 120 for c in concepts)):
        raise runtime.InvalidRequest(f"`concepts` must be 1-{MAX_CONCEPTS} XBRL concept names (strings).")
    else:
        concepts = [c.strip() for c in concepts]
    periods = payload.get("periods", 8)
    if isinstance(periods, bool) or not isinstance(periods, int) or not 1 <= periods <= MAX_PERIODS:
        raise runtime.InvalidRequest(f"`periods` must be a whole number from 1 to {MAX_PERIODS}.")
    forms = payload.get("forms")
    if forms is not None and (not isinstance(forms, list) or not forms
                              or not all(isinstance(f, str) and f.strip() for f in forms)):
        raise runtime.InvalidRequest("`forms`, when given, must be a list such as [\"10-K\", \"10-Q\"].")
    return {"symbol": symbol, "cik": cik, "concepts": concepts, "periods": periods,
            "forms": [f.strip().upper() for f in forms] if forms else None}


def precheck_fundamentals(payload: dict) -> None:
    _parse_fundamentals(payload)


def _latest_facts(fact: dict, periods: int, forms: Optional[list]) -> tuple:
    """The most recent `periods` distinct reporting periods of one concept,
    newest first, from whichever unit the filer reports it in."""
    units = fact.get("units") or {}
    if not units:
        return [], None
    unit = sorted(units.keys(), key=lambda u: (u != "USD", u))[0]
    rows = [r for r in units[unit] if isinstance(r, dict) and r.get("end") and r.get("val") is not None]
    if forms:
        rows = [r for r in rows if str(r.get("form") or "").upper() in forms]
    # One value per (start, end): the latest filing that reports it wins,
    # which is the restated figure when there was a restatement.
    rows.sort(key=lambda r: (r.get("end"), r.get("start") or "", r.get("filed") or ""))
    by_period = {}
    for r in rows:
        by_period[(r.get("start"), r.get("end"))] = r
    picked = sorted(by_period.values(), key=lambda r: (r.get("end"), r.get("start") or ""), reverse=True)[:periods]
    out = [{"end": r.get("end"), "start": r.get("start"), "value": r.get("val"), "unit": unit,
            "fiscal_year": r.get("fy"), "fiscal_period": r.get("fp"), "form": r.get("form"),
            "filed": r.get("filed"), "frame": r.get("frame")} for r in picked]
    return out, unit


async def fundamentals(ctx, payload: dict) -> dict:
    req = _parse_fundamentals(payload)
    provider = sec_edgar.PROVIDERS[0]
    cik, title = req["cik"], None
    if cik is None:
        listing = await provider.cik_for(req["symbol"])
        if listing is None:
            raise runtime.InvalidRequest(
                f"`symbol` {req['symbol']} is not in the SEC's ticker table; give `cik` instead.")
        cik, title = listing["cik"], listing.get("title")

    async def call(p):
        return await p.company_facts(cik)

    data = await ctx.run("company_facts", sec_edgar.PROVIDERS, call, per_attempt_seconds=45)
    taxonomies = data.get("facts") or {}
    facts, missing, latest_filed = {}, [], None
    for concept in req["concepts"]:
        # Filers move between alternate concepts over the years (Apple's
        # `Revenues` stops in 2018; `RevenueFromContractWithCustomer...`
        # carries on). The alternate with the most recent period wins.
        candidates = []
        for name in CONCEPT_ALTERNATES.get(concept, [concept]):
            for taxonomy in ("us-gaap", "ifrs-full", "dei"):
                fact = (taxonomies.get(taxonomy) or {}).get(name)
                if fact:
                    rows, unit = _latest_facts(fact, req["periods"], req["forms"])
                    if rows:
                        candidates.append({"concept": name, "taxonomy": taxonomy, "label": fact.get("label"),
                                           "unit": unit, "values": rows})
        found = max(candidates, key=lambda c: c["values"][0]["end"]) if candidates else None
        if found is None:
            missing.append(concept)
            continue
        facts[concept] = found
        for r in found["values"]:
            if r.get("filed") and (latest_filed is None or r["filed"] > latest_filed):
                latest_filed = r["filed"]
    return {
        "symbol": req["symbol"],
        "cik": int(cik),
        "entity_name": data.get("entityName") or title,
        "concepts": facts,
        "concepts_missing": missing,
        "periods": req["periods"],
        "forms": req["forms"],
        "as_of": latest_filed,
        "source": "sec-edgar-xbrl-companyfacts",
        "checked_at": _now(),
    }


# --- finance.analytics: the mathematics -----------------------------------------

def _mean(values: list) -> float:
    return math.fsum(values) / len(values)


def _var(values: list, ddof: int = 1) -> Optional[float]:
    n = len(values)
    if n - ddof <= 0:
        return None
    m = _mean(values)
    return math.fsum((v - m) ** 2 for v in values) / (n - ddof)


def _std(values: list, ddof: int = 1) -> Optional[float]:
    v = _var(values, ddof)
    return math.sqrt(v) if v is not None else None


def _cov(xs: list, ys: list) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = _mean(xs), _mean(ys)
    return math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)


def _quantile(sorted_values: list, q: float) -> float:
    """Linear interpolation between order statistics (NumPy's default,
    R type 7): position (n-1)q."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


_STD_NORMAL = NormalDist()


def norm_cdf(x: float) -> float:
    return _STD_NORMAL.cdf(x)


def norm_pdf(x: float) -> float:
    return _STD_NORMAL.pdf(x)


def norm_ppf(p: float) -> float:
    """Inverse normal CDF: the standard library's implementation (Wichura's
    AS241, accurate to about 1e-16)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    return _STD_NORMAL.inv_cdf(p)


def simple_returns(prices: list) -> list:
    return [(b / a) - 1.0 for a, b in zip(prices, prices[1:])]


def log_returns(prices: list) -> list:
    return [math.log(b / a) for a, b in zip(prices, prices[1:])]


def max_drawdown(prices: list) -> dict:
    """Largest peak-to-trough fall of the running maximum, as a fraction of
    the peak, with the indices; recovery_index is the first later index at
    or above the peak, null when it never recovered."""
    peak_i, peak = 0, prices[0]
    worst, worst_peak_i, worst_trough_i = 0.0, 0, 0
    for i, p in enumerate(prices):
        if p > peak:
            peak, peak_i = p, i
        dd = (p - peak) / peak if peak else 0.0
        if dd < worst:
            worst, worst_peak_i, worst_trough_i = dd, peak_i, i
    recovery = None
    if worst < 0:
        target = prices[worst_peak_i]
        for j in range(worst_trough_i + 1, len(prices)):
            if prices[j] >= target:
                recovery = j
                break
    return {"max_drawdown": worst, "peak_index": worst_peak_i, "trough_index": worst_trough_i,
            "recovery_index": recovery,
            "duration_periods": (worst_trough_i - worst_peak_i) if worst < 0 else 0}


def sma(prices: list, window: int) -> Optional[float]:
    if window > len(prices):
        return None
    return math.fsum(prices[-window:]) / window


def ema(prices: list, window: int) -> Optional[float]:
    """Exponential moving average seeded with the SMA of the first `window`
    prices, smoothing 2/(window+1), the standard charting definition."""
    if window > len(prices):
        return None
    alpha = 2.0 / (window + 1)
    value = math.fsum(prices[:window]) / window
    for p in prices[window:]:
        value = alpha * p + (1 - alpha) * value
    return value


def rsi(prices: list, window: int = 14) -> Optional[float]:
    """Wilder's RSI: average gain and loss over the first `window` changes,
    then Wilder smoothing ((prev*(w-1) + current)/w) for the rest."""
    if len(prices) < window + 1:
        return None
    changes = [b - a for a, b in zip(prices, prices[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = math.fsum(gains[:window]) / window
    avg_loss = math.fsum(losses[:window]) / window
    for g, l in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + l) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def bollinger(prices: list, window: int = 20, k: float = 2.0) -> Optional[dict]:
    """Middle = SMA(window); bands = middle +/- k * population standard
    deviation of the same window (the charting convention)."""
    if window > len(prices):
        return None
    tail = prices[-window:]
    middle = math.fsum(tail) / window
    sd = math.sqrt(math.fsum((p - middle) ** 2 for p in tail) / window)
    upper, lower = middle + k * sd, middle - k * sd
    return {"window": window, "k": k, "middle": middle, "upper": upper, "lower": lower,
            "bandwidth": (upper - lower) / middle if middle else None,
            "percent_b": ((prices[-1] - lower) / (upper - lower)) if upper != lower else None}


def black_scholes(kind: str, spot: float, strike: float, rate: float, volatility: float,
                  years: float, dividend_yield: float = 0.0) -> dict:
    """Black-Scholes-Merton for a European option on an asset with a
    continuous dividend yield q. Greeks per unit: delta per 1.0 of spot,
    gamma per 1.0^2, vega per 1.0 of volatility (divide by 100 for a
    percentage point), theta per year (divide by 365 for a day), rho per
    1.0 of rate."""
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * volatility ** 2) * years) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t
    disc_r = math.exp(-rate * years)
    disc_q = math.exp(-dividend_yield * years)
    pdf_d1 = norm_pdf(d1)
    if kind == "call":
        price = spot * disc_q * norm_cdf(d1) - strike * disc_r * norm_cdf(d2)
        delta = disc_q * norm_cdf(d1)
        theta = (-spot * disc_q * pdf_d1 * volatility / (2 * sqrt_t)
                 - rate * strike * disc_r * norm_cdf(d2) + dividend_yield * spot * disc_q * norm_cdf(d1))
        rho = strike * years * disc_r * norm_cdf(d2)
    else:
        price = strike * disc_r * norm_cdf(-d2) - spot * disc_q * norm_cdf(-d1)
        delta = -disc_q * norm_cdf(-d1)
        theta = (-spot * disc_q * pdf_d1 * volatility / (2 * sqrt_t)
                 + rate * strike * disc_r * norm_cdf(-d2) - dividend_yield * spot * disc_q * norm_cdf(-d1))
        rho = -strike * years * disc_r * norm_cdf(-d2)
    gamma = disc_q * pdf_d1 / (spot * volatility * sqrt_t)
    vega = spot * disc_q * pdf_d1 * sqrt_t
    return {"type": kind, "price": price, "delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta, "rho": rho, "d1": d1, "d2": d2,
            "inputs": {"spot": spot, "strike": strike, "rate": rate, "volatility": volatility,
                       "time_to_expiry_years": years, "dividend_yield": dividend_yield}}


def kelly(win_probability: float, win_loss_ratio: float) -> dict:
    """Kelly fraction f* = p - (1-p)/b for a bet paying b:1; negative means
    do not bet. half_kelly is the common practical fraction."""
    f = win_probability - (1.0 - win_probability) / win_loss_ratio
    return {"win_probability": win_probability, "win_loss_ratio": win_loss_ratio,
            "fraction": f, "half_kelly": f / 2.0, "bet": f > 0}


# --- finance.analytics: the worker -----------------------------------------------

def _number(value, where: str, minimum=None, maximum=None, exclusive_min=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise runtime.InvalidRequest(f"`{where}` must be a finite number.")
    v = float(value)
    if minimum is not None and (v <= minimum if exclusive_min else v < minimum):
        raise runtime.InvalidRequest(f"`{where}` must be {'greater than' if exclusive_min else 'at least'} {minimum}.")
    if maximum is not None and v > maximum:
        raise runtime.InvalidRequest(f"`{where}` must be at most {maximum}.")
    return v


def _price_series(raw, where: str) -> tuple:
    """Prices as floats plus their dates (None when given bare). Accepts a
    list of numbers or of {date, close} objects, oldest first."""
    if not isinstance(raw, list) or len(raw) < MIN_PRICES:
        raise runtime.InvalidRequest(f"`{where}` must be a list of at least {MIN_PRICES} prices, oldest first.")
    if len(raw) > MAX_PRICES:
        raise runtime.InvalidRequest(f"`{where}` has {len(raw)} prices, over the {MAX_PRICES} limit.")
    prices, dates = [], []
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            value = item.get("close", item.get("price"))
            dates.append(item.get("date") if isinstance(item.get("date"), str) else None)
        else:
            value = item
            dates.append(None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise runtime.InvalidRequest(f"`{where}[{i}]` must be a positive finite price.")
        prices.append(float(value))
    return prices, (dates if any(dates) else None)


def parse_analytics(payload: dict) -> dict:
    """The request, validated; raises InvalidRequest before any payment."""
    has_prices = payload.get("prices") is not None
    has_symbol = payload.get("symbol") is not None
    if has_prices == has_symbol:
        raise runtime.InvalidRequest("Give exactly one of `prices` (inline series) or `symbol` (fetched live).")
    req = {"prices": None, "dates": None, "symbol": None, "range": None,
           "benchmark_prices": None, "benchmark_symbol": None}
    if has_prices:
        req["prices"], req["dates"] = _price_series(payload["prices"], "prices")
    else:
        req["symbol"] = equities.validate_symbol(payload["symbol"])
        req["range"] = equities.validate_range(payload.get("range") or "1y")
    if payload.get("benchmark_prices") is not None and payload.get("benchmark_symbol") is not None:
        raise runtime.InvalidRequest("Give at most one of `benchmark_prices` and `benchmark_symbol`.")
    if payload.get("benchmark_prices") is not None:
        req["benchmark_prices"], _ = _price_series(payload["benchmark_prices"], "benchmark_prices")
    if payload.get("benchmark_symbol") is not None:
        req["benchmark_symbol"] = equities.validate_symbol(payload["benchmark_symbol"])
    metrics = payload.get("metrics")
    if metrics is None:
        metrics = list(DEFAULT_METRICS)
        if req["benchmark_prices"] is not None or req["benchmark_symbol"] is not None:
            metrics.append("beta")
        if payload.get("option") is not None:
            metrics.append("black_scholes")
        if payload.get("kelly") is not None:
            metrics.append("kelly")
    elif (not isinstance(metrics, list) or not metrics
          or not all(isinstance(m, str) and m in METRICS for m in metrics)):
        raise runtime.InvalidRequest(f"`metrics` must be a non-empty list drawn from {list(METRICS)}.")
    req["metrics"] = list(dict.fromkeys(metrics))
    req["periods_per_year"] = int(_number(payload.get("periods_per_year", 252), "periods_per_year", 1, 100000))
    req["risk_free_rate"] = _number(payload.get("risk_free_rate", 0.0), "risk_free_rate", -1.0, 10.0)
    req["alpha"] = _number(payload.get("alpha", 0.05), "alpha", 0.0, 0.5, exclusive_min=True)
    windows = payload.get("windows") or {}
    if not isinstance(windows, dict):
        raise runtime.InvalidRequest("`windows`, when given, must be an object.")

    def _ints(key, default):
        raw = windows.get(key, default)
        if isinstance(raw, int) and not isinstance(raw, bool):
            raw = [raw]
        if (not isinstance(raw, list) or not raw or len(raw) > 10
                or not all(isinstance(w, int) and not isinstance(w, bool) and 2 <= w <= 5000 for w in raw)):
            raise runtime.InvalidRequest(f"`windows.{key}` must be whole numbers from 2 to 5000 (up to 10).")
        return list(dict.fromkeys(raw))

    req["windows"] = {"sma": _ints("sma", [20, 50, 200]), "ema": _ints("ema", [12, 26]),
                      "rsi": _ints("rsi", [14])[0], "bollinger": _ints("bollinger", [20])[0]}
    option = payload.get("option")
    if "black_scholes" in req["metrics"]:
        if not isinstance(option, dict):
            raise runtime.InvalidRequest("`option` {type, strike, rate, volatility, time_to_expiry_years, "
                                         "optional spot and dividend_yield} is required for black_scholes.")
        kind = option.get("type")
        if kind not in ("call", "put"):
            raise runtime.InvalidRequest("`option.type` must be call or put.")
        req["option"] = {
            "type": kind,
            "strike": _number(option.get("strike"), "option.strike", 0.0, exclusive_min=True),
            "rate": _number(option.get("rate", req["risk_free_rate"]), "option.rate", -1.0, 10.0),
            "volatility": _number(option.get("volatility"), "option.volatility", 0.0, 100.0, exclusive_min=True)
            if option.get("volatility") is not None else None,
            "time_to_expiry_years": _number(option.get("time_to_expiry_years"), "option.time_to_expiry_years",
                                            0.0, 100.0, exclusive_min=True),
            "dividend_yield": _number(option.get("dividend_yield", 0.0), "option.dividend_yield", 0.0, 10.0),
            "spot": _number(option.get("spot"), "option.spot", 0.0, exclusive_min=True)
            if option.get("spot") is not None else None,
        }
    else:
        req["option"] = None
    kelly_in = payload.get("kelly")
    if "kelly" in req["metrics"]:
        if not isinstance(kelly_in, dict):
            raise runtime.InvalidRequest("`kelly` {win_probability, win_loss_ratio} is required for kelly.")
        req["kelly"] = {"win_probability": _number(kelly_in.get("win_probability"), "kelly.win_probability", 0.0, 1.0),
                        "win_loss_ratio": _number(kelly_in.get("win_loss_ratio"), "kelly.win_loss_ratio", 0.0,
                                                  exclusive_min=True)}
    else:
        req["kelly"] = None
    if "beta" in req["metrics"] and req["benchmark_prices"] is None and req["benchmark_symbol"] is None:
        raise runtime.InvalidRequest("`beta` needs `benchmark_prices` or `benchmark_symbol`.")
    return req


def precheck_analytics(payload: dict) -> None:
    parse_analytics(payload)


def compute(prices: list, req: dict, benchmark: Optional[list] = None) -> dict:
    """Every requested metric from the series alone. Pure."""
    metrics = req["metrics"]
    ppy = req["periods_per_year"]
    rf_period = req["risk_free_rate"] / ppy
    rets = simple_returns(prices)
    logs = log_returns(prices)
    n_ret = len(rets)
    out = {m: None for m in METRICS}
    notes = []

    if "returns" in metrics:
        total = prices[-1] / prices[0] - 1.0
        years = n_ret / ppy
        out["returns"] = {
            "n_returns": n_ret,
            "first_price": prices[0], "last_price": prices[-1],
            "total_return": total,
            "cagr": (prices[-1] / prices[0]) ** (1.0 / years) - 1.0 if years > 0 else None,
            "mean_period_return": _mean(rets),
            "mean_log_return": _mean(logs),
            "annualized_mean_return": _mean(rets) * ppy,
            "best_period": max(rets), "worst_period": min(rets),
        }
    period_sd = _std(rets)
    if "volatility" in metrics:
        downside = [min(r - rf_period, 0.0) for r in rets]
        dd_dev = math.sqrt(math.fsum(d * d for d in downside) / n_ret) if n_ret else None
        out["volatility"] = {
            "period_std": period_sd,
            "annualized": period_sd * math.sqrt(ppy) if period_sd is not None else None,
            "log_return_std": _std(logs),
            "downside_deviation_annualized": dd_dev * math.sqrt(ppy) if dd_dev is not None else None,
        }
        if period_sd is None:
            notes.append("volatility needs at least 3 prices (2 returns).")
    if "sharpe" in metrics:
        excess = _mean(rets) - rf_period
        out["sharpe"] = (excess / period_sd * math.sqrt(ppy)) if period_sd else None
        if not period_sd:
            notes.append("sharpe is undefined when the return standard deviation is zero or unavailable.")
    if "sortino" in metrics:
        downside = [min(r - rf_period, 0.0) for r in rets]
        dd_dev = math.sqrt(math.fsum(d * d for d in downside) / n_ret) if n_ret else 0.0
        out["sortino"] = ((_mean(rets) - rf_period) / dd_dev * math.sqrt(ppy)) if dd_dev else None
        if not dd_dev:
            notes.append("sortino is undefined when there is no downside deviation.")
    if "drawdown" in metrics:
        out["drawdown"] = max_drawdown(prices)
    if "var" in metrics:
        srt = sorted(rets)
        q = _quantile(srt, req["alpha"])
        tail = [r for r in srt if r <= q]
        mu = _mean(rets)
        z = norm_ppf(req["alpha"])
        out["var"] = {
            "alpha": req["alpha"],
            "horizon_periods": 1,
            "historical_var": -q,
            "historical_cvar": -_mean(tail) if tail else None,
            "parametric_var": -(mu + z * period_sd) if period_sd is not None else None,
            "parametric_z": z,
        }
    if "beta" in metrics:
        if benchmark is None:
            out["beta"] = None
        else:
            b_rets = simple_returns(benchmark)
            m = min(len(rets), len(b_rets))
            if m < 2:
                out["beta"] = None
                notes.append("beta needs at least 3 aligned prices in both series.")
            else:
                a, b = rets[-m:], b_rets[-m:]
                if m != n_ret or m != len(b_rets):
                    notes.append(f"beta aligned the last {m} returns of each series.")
                cov, var_b = _cov(a, b), _var(b)
                sd_a, sd_b = _std(a), _std(b)
                beta_v = cov / var_b if var_b else None
                out["beta"] = {
                    "n": m, "beta": beta_v,
                    "alpha_annualized": ((_mean(a) - rf_period) - beta_v * (_mean(b) - rf_period)) * ppy
                    if beta_v is not None else None,
                    "correlation": (cov / (sd_a * sd_b)) if sd_a and sd_b else None,
                    "benchmark_annualized_volatility": sd_b * math.sqrt(ppy) if sd_b is not None else None,
                }
    if "moving_averages" in metrics:
        out["moving_averages"] = {
            "sma": {str(w): sma(prices, w) for w in req["windows"]["sma"]},
            "ema": {str(w): ema(prices, w) for w in req["windows"]["ema"]},
            "last_price": prices[-1],
        }
    if "rsi" in metrics:
        w = req["windows"]["rsi"]
        out["rsi"] = {"window": w, "value": rsi(prices, w)}
        if out["rsi"]["value"] is None:
            notes.append(f"rsi needs at least {w + 1} prices.")
    if "bollinger" in metrics:
        out["bollinger"] = bollinger(prices, req["windows"]["bollinger"])
        if out["bollinger"] is None:
            notes.append(f"bollinger needs at least {req['windows']['bollinger']} prices.")
    if "black_scholes" in metrics and req["option"]:
        o = dict(req["option"])
        spot = o["spot"] if o["spot"] is not None else prices[-1]
        vol = o["volatility"]
        if vol is None:
            log_sd = _std(logs)
            vol = log_sd * math.sqrt(ppy) if log_sd else None
            if vol is None or vol <= 0:
                raise runtime.InvalidRequest("`option.volatility` is required when the series cannot supply one.")
            notes.append("black_scholes used the series' annualized log-return volatility as sigma.")
        out["black_scholes"] = black_scholes(o["type"], spot, o["strike"], o["rate"], vol,
                                             o["time_to_expiry_years"], o["dividend_yield"])
    if "kelly" in metrics and req["kelly"]:
        out["kelly"] = kelly(req["kelly"]["win_probability"], req["kelly"]["win_loss_ratio"])
    return {"metrics": out, "notes": notes, "n_returns": n_ret}


METHOD = ("simple returns p_t/p_{t-1}-1 and log returns ln(p_t/p_{t-1}); sample standard deviation (n-1); "
          "annualization by sqrt(periods_per_year) for volatility and by periods_per_year for means; "
          "Sharpe = (mean period return - rf/ppy)/std * sqrt(ppy); Sortino uses downside deviation vs rf/ppy "
          "(population form over all returns); max drawdown from the running peak; historical VaR = -quantile "
          "(linear interpolation, R type 7) of period returns at alpha, CVaR = -mean of returns at or below it, "
          "parametric VaR = -(mean + z_alpha*std); beta = cov/var vs the benchmark's simple returns, alpha "
          "annualized by ppy; SMA/EMA (EMA seeded with the first-window SMA, smoothing 2/(w+1)); Wilder RSI; "
          "Bollinger = SMA +/- k*population std of the window; Black-Scholes-Merton with continuous dividend "
          "yield (Greeks per unit: vega per 1.0 vol, theta per year); Kelly f* = p-(1-p)/b. All sums via "
          "math.fsum; normal CDF and its inverse from the standard library (AS241).")


async def analytics(ctx, payload: dict) -> dict:
    req = parse_analytics(payload)
    prices, dates, as_of, source = req["prices"], req["dates"], None, {"type": "prices"}
    if req["symbol"]:
        quote = await _quote(ctx, req["symbol"], req["range"], step="prices")
        history = [h for h in quote.get("history") or [] if h.get("close") is not None]
        if len(history) < MIN_PRICES:
            raise runtime.InvalidProviderResponse(
                f"{req['symbol']}: the provider returned {len(history)} closes; at least {MIN_PRICES} are needed.")
        prices = [float(h["close"]) for h in history]
        dates = [h["date"] for h in history]
        as_of = quote.get("as_of") or dates[-1]
        source = {"type": "symbol", "symbol": quote["symbol"], "range": req["range"],
                  "provider": (ctx.providers_used[-1] if getattr(ctx, "providers_used", None) else None)}
    benchmark = req["benchmark_prices"]
    if req["benchmark_symbol"]:
        bq = await _quote(ctx, req["benchmark_symbol"], req["range"] or "1y", step="benchmark")
        benchmark = [float(h["close"]) for h in bq.get("history") or [] if h.get("close") is not None]
        source["benchmark_symbol"] = req["benchmark_symbol"]
    if dates and as_of is None:
        as_of = dates[-1]
    result = compute(prices, req, benchmark)
    return {
        "source": {"type": source["type"], "symbol": source.get("symbol"), "range": source.get("range"),
                   "provider": source.get("provider"), "benchmark_symbol": source.get("benchmark_symbol"),
                   "benchmark_n": len(benchmark) if benchmark else None},
        "n": len(prices),
        "n_returns": result["n_returns"],
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "periods_per_year": req["periods_per_year"],
        "risk_free_rate": req["risk_free_rate"],
        "metrics_computed": [m for m in req["metrics"] if result["metrics"].get(m) is not None],
        **result["metrics"],
        "notes": result["notes"],
        "method": METHOD,
        "as_of": as_of,
        "checked_at": _now(),
    }


SKILLS = {"market.stock": stock, "market.fundamentals": fundamentals, "finance.analytics": analytics}
PRECHECKS = {"market.stock": precheck_stock, "market.fundamentals": precheck_fundamentals,
             "finance.analytics": precheck_analytics}
