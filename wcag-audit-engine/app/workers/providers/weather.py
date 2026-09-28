"""Weather from public forecasters that allow reuse, keyless, verified 2026-09-28.

  MET Norway     locationforecast 2.0, every point on Earth, CC BY 4.0 / NLOD:
                 credit "Data from MET Norway"; an identifying User-Agent with
                 contact; responses reused until their own Expires header and
                 coordinates cut to 4 decimals, as MET's terms ask
  US NWS         active alerts for a US point (US federal, public domain)
  Place names    Wikidata (CC0) coordinates for a named place; US street
                 addresses through the Census Geocoder; airport codes from
                 the bundled OurAirports table (public domain)
"""

import email.utils
import os
import time
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_WEATHER_TIMEOUT_SECONDS", "15"))
USER_AGENT = os.environ.get("WORKER_WEATHER_USER_AGENT", "HubVibe/1.0 Hubvibe@hubvibe-io.com https://hubvibe-io.com")
MET = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
NWS_ALERTS = "https://api.weather.gov/alerts/active"
WD_API = "https://www.wikidata.org/w/api.php"
MET_CREDIT = {"text": "Data from MET Norway", "url": "https://www.met.no/en", "license": "CC BY 4.0"}
NWS_CREDIT = {"text": "Alerts from the US National Weather Service", "url": "https://www.weather.gov", "license": "public domain"}
_met_cache: dict = {}


async def _get(url: str, params: Optional[dict], what: str, accept: str = "application/json"):
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": accept})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{what} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{what} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429 else "provider_transient")
    if response.status_code >= 400:
        raise runtime.InvalidProviderResponse(f"{what} answered HTTP {response.status_code}.")
    try:
        return response.json(), response.headers
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{what} did not return JSON.") from None


def _result(value, usage: str) -> runtime.ProviderResult:
    return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True, usage=usage)


class _Keyless:
    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""


class _Met(_Keyless):
    id = "met-norway"

    async def forecast(self, lat: float, lon: float) -> runtime.ProviderResult:
        key = (round(lat, 4), round(lon, 4))
        hit = _met_cache.get(key)
        if hit and hit[0] > time.time():
            return _result(hit[1], "cache")
        data, headers = await _get(MET, {"lat": f"{key[0]:.4f}", "lon": f"{key[1]:.4f}"}, "MET Norway")
        expires = headers.get("expires")
        try:
            until = email.utils.parsedate_to_datetime(expires).timestamp() if expires else time.time() + 600
        except (TypeError, ValueError):
            until = time.time() + 600
        _met_cache[key] = (until, data)
        if len(_met_cache) > 5000:
            now = time.time()
            for k in [k for k, v in _met_cache.items() if v[0] <= now]:
                _met_cache.pop(k, None)
        return _result(data, f"timeseries={len(((data or {}).get('properties') or {}).get('timeseries') or [])}")


class _NwsAlerts(_Keyless):
    id = "nws-alerts"

    async def active(self, lat: float, lon: float) -> runtime.ProviderResult:
        data, _ = await _get(NWS_ALERTS, {"point": f"{lat:.4f},{lon:.4f}"}, "NWS alerts", "application/geo+json")
        return _result((data or {}).get("features") or [], "alerts")


class _Places(_Keyless):
    id = "wikidata-places"

    async def find(self, name: str) -> runtime.ProviderResult:
        """(label, description, lat, lon, qid) of the first search hit that has coordinates."""
        found, _ = await _get(WD_API, {"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                                       "limit": 6, "format": "json"}, "Wikidata search")
        for hit in (found or {}).get("search", []):
            claims, _ = await _get(WD_API, {"action": "wbgetclaims", "entity": hit["id"], "property": "P625",
                                            "format": "json"}, "Wikidata coordinates")
            for c in ((claims or {}).get("claims") or {}).get("P625", []):
                v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
                if isinstance(v.get("latitude"), (int, float)) and (v.get("globe") or "").endswith("Q2"):
                    return _result({"name": hit.get("label"), "description": hit.get("description"),
                                    "lat": v["latitude"], "lon": v["longitude"], "wikidata": hit["id"]},
                                   f"qid={hit['id']}")
        return _result(None, "no place")


FORECAST = _Met()
ALERTS = _NwsAlerts()
PLACES = _Places()
PROVIDERS = [FORECAST, ALERTS, PLACES]
