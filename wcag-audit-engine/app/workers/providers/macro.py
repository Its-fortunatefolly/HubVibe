"""Official economic statistics, keyless: the World Bank for every country
(annual), Eurostat for the EU (monthly).

The World Bank's indicator API serves 200+ economies and thousands of
series with the source's own `lastupdated` stamp; Eurostat's dissemination
API serves the EU aggregates and members monthly (HICP inflation,
unemployment) as JSON-stat. Both verified live 2026-09-27. Nothing is
cached; `as_of` is the source's stamp, `checked_at` is ours.
"""

import os
import re
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_MACRO_TIMEOUT_SECONDS", "25"))
USER_AGENT = os.environ.get("WORKER_MACRO_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
WORLD_BANK_BASE = os.environ.get("WORKER_WORLD_BANK_BASE", "https://api.worldbank.org/v2")
EUROSTAT_BASE = os.environ.get("WORKER_EUROSTAT_BASE",
                               "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data")
MAX_OBSERVATIONS = 60
WB_CODE = re.compile(r"^[A-Z]{2}\.[A-Z0-9]{2,}(\.[A-Z0-9]{1,})*$")

# alias -> World Bank indicator code (annual, every economy)
ALIASES = {
    "gdp_usd": "NY.GDP.MKTP.CD",
    "gdp_growth": "NY.GDP.MKTP.KD.ZG",
    "gdp_per_capita_usd": "NY.GDP.PCAP.CD",
    "inflation": "FP.CPI.TOTL.ZG",
    "unemployment": "SL.UEM.TOTL.ZS",
    "population": "SP.POP.TOTL",
    "population_growth": "SP.POP.GROW",
    "current_account_gdp": "BN.CAB.XOKA.GD.ZS",
    "reserves_usd": "FI.RES.TOTL.CD",
    "government_debt_gdp": "GC.DOD.TOTL.GD.ZS",
    "exports_gdp": "NE.EXP.GNFS.ZS",
    "imports_gdp": "NE.IMP.GNFS.ZS",
    "fdi_inflows_usd": "BX.KLT.DINV.CD.WD",
    "exchange_rate_per_usd": "PA.NUS.FCRF",
    "real_interest_rate": "FR.INR.RINR",
    "lending_rate": "FR.INR.LEND",
    "trade_gdp": "NE.TRD.GNFS.ZS",
    "internet_users_pct": "IT.NET.USER.ZS",
    "life_expectancy": "SP.DYN.LE00.IN",
    "co2_per_capita": "EN.GHG.CO2.PC.CE.AR5",
}
# Monthly series Eurostat publishes for the EU and its members.
EUROSTAT_MONTHLY = {
    "inflation": {"dataset": "prc_hicp_manr", "params": {"coicop": "CP00"}, "unit": "% change on a year earlier (HICP)"},
    "unemployment": {"dataset": "une_rt_m", "params": {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
                     "unit": "% of active population, seasonally adjusted"},
}
EU_GEOS = {"EU27_2020", "EA", "EA20", "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "EL",
           "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "NO", "IS", "CH",
           "TR", "RS", "ME", "MK", "AL", "BA", "UK"}


def resolve_indicator(raw: str) -> tuple:
    """(alias or None, World Bank code) for an alias or a raw code."""
    key = raw.strip()
    if key.lower() in ALIASES:
        return key.lower(), ALIASES[key.lower()]
    if WB_CODE.match(key.upper()):
        return None, key.upper()
    raise runtime.InvalidRequest(
        f"`indicator` {raw!r} is not a known alias ({', '.join(sorted(ALIASES))}) or a World Bank indicator code like NY.GDP.MKTP.CD.")


async def _get_json(url: str, params: dict, what: str):
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{what} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{what} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429 else "provider_transient")
    try:
        data = response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{what} did not return JSON (HTTP {response.status_code}).") from None
    return response.status_code, data


class _WorldBank:
    id = "world-bank-api"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def series(self, country: str, code: str, last: int) -> runtime.ProviderResult:
        status, data = await _get_json(f"{WORLD_BANK_BASE}/country/{country}/indicator/{code}",
                                       {"format": "json", "mrv": str(min(max(last, 1), MAX_OBSERVATIONS))},
                                       f"World Bank {code} for {country}")
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("message"):
            msg = data[0]["message"][0] if isinstance(data[0]["message"], list) and data[0]["message"] else {}
            raise runtime.InvalidRequest(f"The World Bank rejected {code} for {country}: {msg.get('value') or msg.get('key') or 'invalid value'}.")
        if status >= 400 or not isinstance(data, list) or len(data) < 2:
            raise runtime.InvalidProviderResponse(f"World Bank answered {code} for {country} with an unexpected shape (HTTP {status}).")
        meta, rows = data[0], data[1]
        if not rows:
            raise runtime.InvalidRequest(f"The World Bank has no country {country!r} (use ISO 3166 alpha-2 or alpha-3).")
        first = rows[0]
        observations = [{"period": r.get("date"), "value": r.get("value")} for r in rows if isinstance(r, dict)]
        observations.sort(key=lambda o: o["period"] or "")
        return runtime.ProviderResult(
            value={"indicator_name": (first.get("indicator") or {}).get("value"),
                   "country_code": (first.get("country") or {}).get("id"),
                   "country_name": (first.get("country") or {}).get("value"),
                   "as_of": meta.get("lastupdated"), "observations": observations,
                   "source_url": f"{WORLD_BANK_BASE}/country/{country}/indicator/{code}?format=json"},
            cost_micros=0, cost_measured=True, usage=f"code={code} country={country} rows={len(observations)}")


class _Eurostat:
    id = "eurostat-api"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def monthly(self, geo: str, alias: str, last: int) -> runtime.ProviderResult:
        spec = EUROSTAT_MONTHLY[alias]
        params = dict(spec["params"], geo=geo, lastTimePeriod=str(min(max(last, 1), MAX_OBSERVATIONS)))
        status, data = await _get_json(f"{EUROSTAT_BASE}/{spec['dataset']}", params, f"Eurostat {spec['dataset']} for {geo}")
        if status >= 400 or not isinstance(data, dict) or "dimension" not in data:
            detail = data.get("error", {}) if isinstance(data, dict) else {}
            raise runtime.InvalidProviderResponse(f"Eurostat answered {spec['dataset']} for {geo} with HTTP {status}: {str(detail)[:160]}")
        index = data["dimension"]["time"]["category"]["index"]
        values = data.get("value") or {}
        observations = sorted(({"period": t, "value": values.get(str(i))} for t, i in index.items()),
                              key=lambda o: o["period"])
        geo_label = (data["dimension"].get("geo", {}).get("category", {}).get("label", {}) or {}).get(geo)
        return runtime.ProviderResult(
            value={"indicator_name": data.get("label"), "unit": spec["unit"], "country_code": geo,
                   "country_name": geo_label, "as_of": data.get("updated"), "observations": observations,
                   "source_url": f"{EUROSTAT_BASE}/{spec['dataset']}?geo={geo}"},
            cost_micros=0, cost_measured=True, usage=f"dataset={spec['dataset']} geo={geo} rows={len(observations)}")


WORLD_BANK = _WorldBank()
EUROSTAT = _Eurostat()
PROVIDERS = [WORLD_BANK, EUROSTAT]
