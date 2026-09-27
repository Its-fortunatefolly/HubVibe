"""Keyless equity quotes and daily history: Nasdaq's data API first, Yahoo's
chart API as the fallback.

Neither is a contracted feed: both are the public endpoints their own
websites read, served without a key. That is exactly why they are two
providers behind the runtime's breaker and failover rather than one, and
why every result says which one answered, how old the data is, and when it
was read (`as_of`, `delayed_minutes`, `checked_at` set by the skill).

NEVER A CACHED ANSWER: each call is a fresh request. What the sources say
about staleness (`isRealTime` false on Nasdaq's quote) is passed through as
`delayed_minutes` rather than hidden.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_EQUITIES_TIMEOUT_SECONDS", "20"))
# Both hosts answer a browser-shaped agent. Nasdaq's edge STALLS (never
# answers, no error) on an agent string carrying a URL, so unlike the other
# providers this one names the node without one -- verified 2026-09-27.
USER_AGENT = os.environ.get(
    "WORKER_EQUITIES_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) HubVibe-worker/1.0")
NASDAQ_BASE = os.environ.get("WORKER_NASDAQ_BASE", "https://api.nasdaq.com")
YAHOO_BASE = os.environ.get("WORKER_YAHOO_BASE", "https://query1.finance.yahoo.com")

SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$")
RANGES = ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y")
_RANGE_DAYS = {"1d": 1, "5d": 7, "1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827}
# Nasdaq's quote endpoint says isRealTime=false; its site labels such data
# "delayed at least 15 minutes". Passed through, never assumed away.
NASDAQ_DELAY_MINUTES = 15


def validate_symbol(raw) -> str:
    if not isinstance(raw, str) or not SYMBOL.match(raw.strip()):
        raise runtime.InvalidRequest(
            "`symbol` must be a ticker of 1-12 letters, digits, dots or dashes (AAPL, BRK.B, RY-PC).")
    return raw.strip().upper()


def validate_range(raw) -> str:
    if raw is None:
        return "1mo"
    if not isinstance(raw, str) or raw not in RANGES:
        raise runtime.InvalidRequest(f"`range` must be one of {list(RANGES)}.")
    return raw


def _num(value) -> Optional[float]:
    """'$341.07', '+5.15', '+1.53%', '30,002,768', 'N/A' -> float or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.strip().replace("$", "").replace(",", "").replace("%", "")
    if cleaned in ("", "N/A", "NA", "--"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _iso_utc(epoch) -> Optional[str]:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_NASDAQ_TS = ("%b %d, %Y %I:%M %p ET", "%b %d, %Y")


def _nasdaq_as_of(text) -> Optional[str]:
    """'Sep 26, 2026 4:00 PM ET' or 'Sep 24, 2026' -> ISO 8601. A time given
    in ET is kept as a local-time stamp with its zone named, never silently
    relabelled as UTC."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    for fmt in _NASDAQ_TS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.endswith("ET"):
            return parsed.strftime("%Y-%m-%dT%H:%M:00") + " America/New_York"
        return parsed.strftime("%Y-%m-%d")
    return value


async def _get_json(url: str, params: Optional[dict] = None) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{url} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{url} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{url} returned {response.status_code}")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{url} did not return JSON.") from None


class _NasdaqDataApi:
    id = "nasdaq-data-api"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def quote(self, symbol: str, history_range: Optional[str] = None) -> runtime.ProviderResult:
        status, data = await _get_json(f"{NASDAQ_BASE}/api/quote/{symbol}/info", {"assetclass": "stocks"})
        payload = data.get("data") if isinstance(data, dict) else None
        rcode = ((data.get("status") or {}).get("rCode") if isinstance(data, dict) else None)
        if payload is None:
            messages = ((data.get("status") or {}).get("bCodeMessage") or []) if isinstance(data, dict) else []
            text = "; ".join(str(m.get("errorMessage")) for m in messages if isinstance(m, dict)) or f"HTTP {status}"
            if "not exist" in text.lower() or rcode == 400:
                # Not a Nasdaq-listed stock (could be an ETF, a foreign
                # listing, or nothing): the fallback provider decides.
                raise runtime.PermanentProviderError(f"Nasdaq has no stock quote for {symbol}: {text}")
            raise runtime.InvalidProviderResponse(f"Nasdaq returned no quote for {symbol}: {text}")
        primary = payload.get("primaryData") or {}
        price = _num(primary.get("lastSalePrice"))
        if price is None:
            raise runtime.InvalidProviderResponse(f"Nasdaq quote for {symbol} carries no last sale price.")
        key_stats = payload.get("keyStats") or {}
        day_range = _num_range((key_stats.get("dayrange") or {}).get("value"))
        value = {
            "symbol": payload.get("symbol") or symbol,
            "name": payload.get("companyName"),
            "exchange": payload.get("exchange"),
            "asset_class": payload.get("assetClass") or "stocks",
            # Nasdaq's quote carries no currency field for US listings; the
            # endpoint is the US market, priced in dollars.
            "currency": primary.get("currency") or "USD",
            "price": price,
            "change": _num(primary.get("netChange")),
            "change_pct": _num(primary.get("percentageChange")),
            "day_high": day_range[1], "day_low": day_range[0],
            "volume": _num(primary.get("volume")),
            "previous_close": None,
            "as_of": _nasdaq_as_of(primary.get("lastTradeTimestamp")),
            "market_state": payload.get("marketStatus"),
            "delayed_minutes": 0 if primary.get("isRealTime") is True else NASDAQ_DELAY_MINUTES,
            "history": [],
            "history_range": None,
        }
        if history_range:
            value["history"] = await self._history(symbol, history_range)
            value["history_range"] = history_range
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"symbol={symbol} history={len(value['history'])}")

    async def _history(self, symbol: str, history_range: str) -> list:
        days = _RANGE_DAYS[history_range]
        today = datetime.now(timezone.utc).date()
        params = {"assetclass": "stocks", "limit": str(max(days, 1) + 5),
                  "fromdate": (today - timedelta(days=days)).isoformat(),
                  "todate": today.isoformat()}
        _, data = await _get_json(f"{NASDAQ_BASE}/api/quote/{symbol}/historical", params)
        table = ((data.get("data") or {}).get("tradesTable") or {}) if isinstance(data, dict) else {}
        rows = []
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            date = _us_date(row.get("date"))
            if not date:
                continue
            rows.append({"date": date, "open": _num(row.get("open")), "high": _num(row.get("high")),
                         "low": _num(row.get("low")), "close": _num(row.get("close")),
                         "volume": _num(row.get("volume"))})
        rows.sort(key=lambda r: r["date"])  # oldest first, the analytics order
        return rows


def _num_range(text) -> tuple:
    if not isinstance(text, str) or "-" not in text:
        return (None, None)
    low, _, high = text.partition("-")
    return (_num(low), _num(high))


def _us_date(text) -> Optional[str]:
    if not isinstance(text, str):
        return None
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


class _YahooChart:
    id = "yahoo-chart"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def quote(self, symbol: str, history_range: Optional[str] = None) -> runtime.ProviderResult:
        params = {"range": history_range or "5d", "interval": "1d"}
        status, data = await _get_json(f"{YAHOO_BASE}/v8/finance/chart/{symbol}", params)
        chart = (data.get("chart") or {}) if isinstance(data, dict) else {}
        results = chart.get("result") or []
        if not results:
            error = chart.get("error") or {}
            description = str(error.get("description") or f"HTTP {status}")
            if status == 404 or "no data found" in description.lower():
                raise runtime.InvalidRequest(f"`symbol` {symbol} is not known to Yahoo Finance ({description}).")
            raise runtime.InvalidProviderResponse(f"Yahoo returned no chart for {symbol}: {description}")
        result = results[0]
        meta = result.get("meta") or {}
        price = _num(meta.get("regularMarketPrice"))
        if price is None:
            raise runtime.InvalidProviderResponse(f"Yahoo chart for {symbol} carries no market price.")
        previous = _num(meta.get("chartPreviousClose") if meta.get("previousClose") is None else meta.get("previousClose"))
        change = round(price - previous, 6) if previous is not None else None
        history = []
        if history_range:
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
            for i, ts in enumerate(result.get("timestamp") or []):
                day = _iso_utc(ts)
                if not day:
                    continue
                history.append({"date": day[:10],
                                "open": _num(_at(quote.get("open"), i)), "high": _num(_at(quote.get("high"), i)),
                                "low": _num(_at(quote.get("low"), i)), "close": _num(_at(quote.get("close"), i)),
                                "volume": _num(_at(quote.get("volume"), i))})
        value = {
            "symbol": meta.get("symbol") or symbol,
            "name": meta.get("longName") or meta.get("shortName"),
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
            "asset_class": (meta.get("instrumentType") or "").lower() or None,
            "currency": meta.get("currency"),
            "price": price,
            "change": change,
            "change_pct": round(change / previous * 100, 4) if change is not None and previous else None,
            "day_high": _num(meta.get("regularMarketDayHigh")), "day_low": _num(meta.get("regularMarketDayLow")),
            "volume": _num(meta.get("regularMarketVolume")),
            "previous_close": previous,
            "as_of": _iso_utc(meta.get("regularMarketTime")),
            "market_state": None,
            # Yahoo does not state the delay on this endpoint; unknown, not zero.
            "delayed_minutes": None,
            "history": history,
            "history_range": history_range,
        }
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"symbol={symbol} history={len(history)}")


def _at(values, i):
    if isinstance(values, list) and i < len(values):
        return values[i]
    return None


PROVIDERS = [_NasdaqDataApi(), _YahooChart()]
