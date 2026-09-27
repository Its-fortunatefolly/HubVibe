"""SEC EDGAR XBRL company facts: the filer's own reported numbers, from the
official, keyless API at data.sec.gov.

The SEC's fair-access policy asks for a descriptive User-Agent and no more
than 10 requests a second; both are honoured here. Two requests per job at
most: the ticker-to-CIK map (a reference table, kept in memory for a few
hours because tickers do not change by the minute -- it is not an answer)
and the company's facts document, fetched fresh every call.
"""

import os
import time
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_SEC_TIMEOUT_SECONDS", "45"))
USER_AGENT = os.environ.get("WORKER_SEC_USER_AGENT", "HubVibe hubvibe-io.com (+https://hubvibe-io.com)")
TICKERS_URL = os.environ.get("WORKER_SEC_TICKERS_URL", "https://www.sec.gov/files/company_tickers.json")
FACTS_BASE = os.environ.get("WORKER_SEC_FACTS_BASE", "https://data.sec.gov/api/xbrl/companyfacts")
TICKER_MAP_TTL_SECONDS = float(os.environ.get("WORKER_SEC_TICKER_TTL_SECONDS", str(6 * 3600)))

_ticker_map: dict = {}
_ticker_map_at: float = 0.0


async def _get_json(url: str) -> tuple:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{url} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{url} unreachable: {exc}") from exc
    if response.status_code in (403, 429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"SEC returned {response.status_code} for {url}")
    if response.status_code == 404:
        return 404, None
    if response.status_code >= 400:
        raise runtime.PermanentProviderError(f"SEC returned {response.status_code} for {url}")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{url} did not return JSON.") from None


class _SecEdgar:
    id = "sec-edgar"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def cik_for(self, symbol: str) -> Optional[dict]:
        """{'cik': int, 'title': str} for a ticker, or None when the SEC's
        ticker table does not list it."""
        global _ticker_map, _ticker_map_at
        now = time.monotonic()
        if not _ticker_map or now - _ticker_map_at > TICKER_MAP_TTL_SECONDS:
            _, data = await _get_json(TICKERS_URL)
            if not isinstance(data, dict):
                raise runtime.InvalidProviderResponse("SEC ticker table is not the expected object.")
            fresh = {}
            for entry in data.values():
                if isinstance(entry, dict) and isinstance(entry.get("ticker"), str):
                    fresh[entry["ticker"].upper()] = {"cik": int(entry.get("cik_str") or 0),
                                                      "title": entry.get("title")}
            if not fresh:
                raise runtime.InvalidProviderResponse("SEC ticker table came back empty.")
            _ticker_map, _ticker_map_at = fresh, now
        return _ticker_map.get(symbol.upper())

    async def company_facts(self, cik: int) -> runtime.ProviderResult:
        status, data = await _get_json(f"{FACTS_BASE}/CIK{int(cik):010d}.json")
        if status == 404 or not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
            raise runtime.InvalidRequest(f"The SEC has no XBRL company facts for CIK {int(cik):010d}.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True,
                                      usage=f"cik={int(cik):010d} taxonomies={len(data['facts'])}")


def reset_for_tests() -> None:
    global _ticker_map, _ticker_map_at
    _ticker_map, _ticker_map_at = {}, 0.0


PROVIDERS = [_SecEdgar()]
