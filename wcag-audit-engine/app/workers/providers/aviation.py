"""Flight status from sources that allow reuse, keyless, verified 2026-09-28.

  FAA NAS Status      US airport ground stops, ground delay programs, closures,
                      arrival/departure delays, runway configuration (US federal)
  Avinor              live departure and arrival boards for Norway's airports.
                      Terms: no limits on use within Norwegian law; every
                      response must carry "Flight data from Avinor" linked to
                      www.avinor.no, which ours does
  NOAA AWC            METAR and TAF for any airport with an ICAO id (US federal)
  adsb.lol            live aircraft positions (ODbL 1.0, attribution carried)
  OurAirports         the bundled code -> airport table ("released to the
                      Public Domain"; scripts/build_airports_table.py)
"""

import asyncio
import csv
import gzip
import math
import os
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_AVIATION_TIMEOUT_SECONDS", "15"))
USER_AGENT = os.environ.get("WORKER_AVIATION_USER_AGENT", "HubVibe Hubvibe@hubvibe-io.com")
FAA_EVENTS = "https://nasstatus.faa.gov/api/airport-events"
AVINOR = "https://asrv.avinor.no/XmlFeed/v1.0"
NOAA = "https://aviationweather.gov/api/data"
ADSB = "https://api.adsb.lol/v2"
AIRPORTS_FILE = Path(__file__).with_name("data") / "airports.csv.gz"
AVINOR_ATTRIBUTION = "Flight data from Avinor"
AVINOR_URL = "https://www.avinor.no"
ADSB_ATTRIBUTION = "Aircraft positions from adsb.lol (ODbL 1.0)"
AVINOR_STATUS = {"N": "new info", "E": "new time", "D": "departed", "A": "arrived", "C": "cancelled"}
AVINOR_SECTOR = {"D": "domestic", "S": "schengen", "I": "international"}


async def _get(url: str, params: Optional[dict], what: str, want_json: bool = True):
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{what} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{what} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429 else "provider_transient")
    if response.status_code == 204:
        return None
    if response.status_code >= 400:
        raise runtime.InvalidProviderResponse(f"{what} answered HTTP {response.status_code}.")
    if not want_json:
        return response.content
    try:
        return response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{what} did not return JSON.") from None


# Each source's own refresh pace: Avinor asks partners to poll every 3 minutes
# and serve from their own cache; FAA programs change on the minute; METARs
# are hourly; aircraft positions move by the second but adsb.lol answers a
# burst with 429, so positions are shared for 10 seconds and calls spaced.
_TTL = {"faa": 60.0, "avinor": 180.0, "noaa": 300.0, "adsb": 10.0}
_cache: dict = {}
_adsb_lock = asyncio.Lock()
_adsb_last = [0.0]
ADSB_SPACING = float(os.environ.get("WORKER_ADSB_MIN_INTERVAL_SECONDS", "0.25"))


def _cached(kind: str, key: str):
    hit = _cache.get((kind, key))
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _store(kind: str, key: str, value):
    _cache[(kind, key)] = (time.monotonic() + _TTL[kind], value)
    if len(_cache) > 2000:
        now = time.monotonic()
        for k in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(k, None)
    return value


async def _adsb(key: str, url: str):
    """One adsb.lol read, shared: a caller that waited for the lock takes the
    answer the caller before it just fetched instead of asking again."""
    hit = _cached("adsb", key)
    if hit is not None:
        return hit
    async with _adsb_lock:
        hit = _cached("adsb", key)
        if hit is not None:
            return hit
        wait = ADSB_SPACING - (time.monotonic() - _adsb_last[0])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return _store("adsb", key, await _get(url, None, "adsb.lol"))
        finally:
            _adsb_last[0] = time.monotonic()


def _result(value, usage: str) -> runtime.ProviderResult:
    return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True, usage=usage)


def distance_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return round(6371.0088 * 2 * math.asin(math.sqrt(a)), 1)


@lru_cache(maxsize=1)
def _airports() -> tuple:
    by_iata, by_icao = {}, {}
    with gzip.open(AIRPORTS_FILE, "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            row = {"iata": r["iata"] or None, "icao": r["icao"] or None, "name": r["name"], "city": r["city"] or None,
                   "country": r["country"], "lat": float(r["lat"]), "lon": float(r["lon"])}
            if row["iata"] and (row["iata"] not in by_iata or r["type"] == "large_airport"):
                by_iata[row["iata"]] = row
            if row["icao"]:
                by_icao[row["icao"]] = row
    return by_iata, by_icao


def airport(code: str) -> Optional[dict]:
    by_iata, by_icao = _airports()
    code = code.strip().upper()
    return by_iata.get(code) if len(code) == 3 else by_icao.get(code)


class _Keyless:
    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""


class _Faa(_Keyless):
    id = "faa-nas-status"

    async def events(self, faa_id: str) -> runtime.ProviderResult:
        data = _cached("faa", "all")
        if data is None:
            data = _store("faa", "all", await _get(FAA_EVENTS, None, "FAA NAS Status"))
        if not isinstance(data, list):
            raise runtime.InvalidProviderResponse("FAA NAS Status did not return a list of airports.")
        entry = next((a for a in data if isinstance(a, dict) and (a.get("airportId") or "").upper() == faa_id), None)
        return _result({"entry": entry, "airports_with_events": len(data)}, f"airports={len(data)}")


class _Avinor(_Keyless):
    id = "avinor-flight-data"

    async def board(self, iata: str, direction: str, hours_back: int, hours_ahead: int) -> runtime.ProviderResult:
        params = {"airport": iata, "TimeFrom": str(hours_back), "TimeTo": str(hours_ahead)}
        if direction in ("A", "D"):
            params["direction"] = direction
        key = f"{iata}:{direction}:{hours_back}:{hours_ahead}"
        raw = _cached("avinor", key)
        if raw is None:
            raw = _store("avinor", key, await _get(AVINOR, params, "Avinor flight data", want_json=False))
        try:
            root = ET.fromstring(raw or b"<airport/>")
        except ET.ParseError as exc:
            raise runtime.InvalidProviderResponse(f"Avinor returned unreadable XML: {exc}") from None
        flights_el = root.find("flights")
        out = []
        for f in (flights_el.findall("flight") if flights_el is not None else []):
            status = f.find("status")
            code = status.get("code") if status is not None else None

            def text(tag):
                el = f.find(tag)
                return el.text.strip() if el is not None and el.text else None
            out.append({"flight": text("flight_id"), "airline": text("airline"), "direction": text("arr_dep"),
                        "other_airport": text("airport"), "scheduled": text("schedule_time"),
                        "status": AVINOR_STATUS.get(code) if code else None,
                        "status_time": status.get("time") if status is not None else None,
                        "gate": text("gate"), "check_in": text("check_in"), "belt": text("belt_number"),
                        "delayed": text("delayed") == "Y", "sector": AVINOR_SECTOR.get(text("dom_int") or "")})
        return _result({"flights": out, "last_update": flights_el.get("lastUpdate") if flights_el is not None else None},
                       f"flights={len(out)}")


class _Noaa(_Keyless):
    id = "noaa-aviation-weather"

    async def weather(self, icao: str) -> runtime.ProviderResult:
        hit = _cached("noaa", icao)
        if hit is None:
            metar, taf = await asyncio.gather(_get(f"{NOAA}/metar", {"ids": icao, "format": "json"}, "NOAA METAR"),
                                              _get(f"{NOAA}/taf", {"ids": icao, "format": "json"}, "NOAA TAF"))
            hit = _store("noaa", icao, (metar, taf))
        metar, taf = hit
        return _result({"metar": (metar or [None])[0] if isinstance(metar, list) else None,
                        "taf": (taf or [None])[0] if isinstance(taf, list) else None}, f"icao={icao}")


class _Adsb(_Keyless):
    id = "adsb-lol"

    async def callsign(self, callsign: str) -> runtime.ProviderResult:
        data = await _adsb(f"cs:{callsign}", f"{ADSB}/callsign/{callsign}")
        ac = (data or {}).get("ac") or []
        return _result(ac, f"aircraft={len(ac)}")

    async def around(self, lat: float, lon: float, radius_nm: int) -> runtime.ProviderResult:
        data = await _adsb(f"pt:{lat:.2f}:{lon:.2f}:{radius_nm}", f"{ADSB}/point/{lat:.4f}/{lon:.4f}/{radius_nm}")
        ac = (data or {}).get("ac") or []
        return _result(ac, f"aircraft={len(ac)}")


FAA = _Faa()
AVINOR_BOARD = _Avinor()
WEATHER = _Noaa()
AIRCRAFT = _Adsb()
PROVIDERS = [FAA, AVINOR_BOARD, WEATHER, AIRCRAFT]
