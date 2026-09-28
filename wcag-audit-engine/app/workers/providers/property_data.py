"""US address facts from the agencies that publish them, keyless.

Every source here is a US federal agency (17 USC 105: no copyright in its
works) serving a public endpoint, each verified live 2026-09-28:

  Census Geocoder         address or point -> coordinates, tract, county, ZIP, district
  FEMA National Risk Index  tract-level risk for 18 hazards (notice below is mandatory)
  HUD Fair Market Rents   metro and ZIP (Small Area) rents, HUD's own ArcGIS services
  NCES EDGE               public schools near the point
  EPA                     National Walkability Index; Superfund (NPL) sites nearby

Two annual tables ship with the node (scripts/build_property_tables.py):
ACS 5-year tract estimates from the Census Bureau's keyless summary files,
and FHFA's annual tract house price index.
"""

import csv
import gzip
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_PROPERTY_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_PROPERTY_USER_AGENT", "HubVibe Hubvibe@hubvibe-io.com")
GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies"
NRI = ("https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
       "National_Risk_Index_Census_Tracts/FeatureServer/0/query")
FMR = "https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/Fair_Market_Rents/FeatureServer/0/query"
SAFMR = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest/services/"
         "HUD_PDR_Small_Area_Fair_Market_Rents/FeatureServer/1/query")
SCHOOLS = ("https://nces.ed.gov/opengis/rest/services/K12_School_Locations/"
           "EDGE_ADMINDATA_PUBLICSCH_2425/MapServer/1/query")
WALK = "https://geodata.epa.gov/arcgis/rest/services/OA/WalkabilityIndex/MapServer/0/query"
NPL = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/FRS_INTERESTS_SEMS_NPL/FeatureServer/0/query"
DATA = Path(__file__).with_name("data")
ACS_FILE = DATA / "acs5_2020_2024_tracts.csv.gz"
ACS_LABEL = "ACS 5-year estimates 2020-2024 (Census Bureau)"
FHFA_FILE = DATA / "fhfa_tract_hpi.csv.gz"

NRI_NOTICE = ("This product uses the Federal Emergency Management Agency's National Risk Index dataset API or "
              "downloadable datasets but is not endorsed by FEMA. The Federal Government or FEMA cannot vouch for "
              "the data or analyses derived from these data after the data have been retrieved from the Agency's "
              "website(s).")
NRI_HAZARDS = {
    "AVLN": "avalanche", "CFLD": "coastal_flooding", "CWAV": "cold_wave", "DRGT": "drought", "ERQK": "earthquake",
    "HAIL": "hail", "HWAV": "heat_wave", "HRCN": "hurricane", "ISTM": "ice_storm", "LNDS": "landslide",
    "LTNG": "lightning", "IFLD": "riverine_flooding", "SWND": "strong_wind", "TRND": "tornado", "TSUN": "tsunami",
    "VLCN": "volcanic_activity", "WFIR": "wildfire", "WNTW": "winter_weather",
}


async def _get(url: str, params: dict, what: str) -> dict:
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
    if response.status_code >= 400 or (isinstance(data, dict) and "error" in data and "features" not in data):
        detail = data.get("error") if isinstance(data, dict) else None
        raise runtime.InvalidProviderResponse(f"{what} answered HTTP {response.status_code}: {str(detail)[:160]}")
    return data


def _point(lng: float, lat: float, **extra) -> dict:
    params = {"geometry": f"{lng},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "outFields": "*", "f": "json"}
    params.update(extra)
    return params


def _attrs(data: dict) -> list:
    return [f.get("attributes") or {} for f in data.get("features") or []]


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(6371.0088 * 2 * math.asin(math.sqrt(a)), 2)


def _result(value, usage: str) -> runtime.ProviderResult:
    return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True, usage=usage)


class _Keyless:
    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""


class _Geocoder(_Keyless):
    id = "census-geocoder"

    async def locate(self, address: Optional[str], lat: Optional[float], lng: Optional[float]) -> runtime.ProviderResult:
        common = {"benchmark": "Public_AR_Current", "vintage": "Current_Current", "layers": "all", "format": "json"}
        if address:
            data = await _get(f"{GEOCODER}/onelineaddress", dict(common, address=address), "Census Geocoder")
            matches = (data.get("result") or {}).get("addressMatches") or []
            if not matches:
                return _result(None, "no match")
            m = matches[0]
            value = {"matched_address": m.get("matchedAddress"), "lng": m["coordinates"]["x"],
                     "lat": m["coordinates"]["y"], "geographies": m.get("geographies") or {}, "candidates": len(matches)}
        else:
            data = await _get(f"{GEOCODER}/coordinates", dict(common, x=str(lng), y=str(lat)), "Census Geocoder")
            geos = (data.get("result") or {}).get("geographies") or {}
            if not geos.get("Census Tracts"):
                return _result(None, "no tract at point")
            value = {"matched_address": None, "lng": lng, "lat": lat, "geographies": geos, "candidates": 1}
        return _result(value, f"candidates={value['candidates']}")



class _Risk(_Keyless):
    id = "fema-nri"

    async def tract(self, tract_fips: str) -> runtime.ProviderResult:
        data = await _get(NRI, {"where": f"TRACTFIPS='{tract_fips}'", "outFields": "*", "returnGeometry": "false",
                                "f": "json"}, "FEMA National Risk Index")
        rows = _attrs(data)
        return _result(rows[0] if rows else None, f"features={len(rows)}")


class _Rents(_Keyless):
    id = "hud-fmr"

    async def metro(self, lng: float, lat: float) -> runtime.ProviderResult:
        rows = _attrs(await _get(FMR, _point(lng, lat), "HUD Fair Market Rents"))
        return _result(rows[0] if rows else None, f"features={len(rows)}")

    async def zip_area(self, zcta: str) -> runtime.ProviderResult:
        rows = _attrs(await _get(SAFMR, {"where": f"ID='{zcta}'", "outFields": "*", "f": "json"},
                                 "HUD Small Area Fair Market Rents"))
        return _result(rows[0] if rows else None, f"features={len(rows)}")


class _Schools(_Keyless):
    id = "nces-schools"

    async def near(self, lng: float, lat: float, radius_km: float) -> runtime.ProviderResult:
        data = await _get(SCHOOLS, _point(lng, lat, distance=str(radius_km), units="esriSRUnit_Kilometer",
                                          returnGeometry="true", outSR="4326",
                                          outFields="SCH_NAME,SCHOOL_LEVEL,GSLO,GSHI,TOTAL,STUTERATIO,CHARTER_TEXT,LEA_NAME,LCITY,LSTATE"),
                          "NCES public schools")
        out = []
        for f in data.get("features") or []:
            a, g = f.get("attributes") or {}, f.get("geometry") or {}
            if "x" in g and "y" in g:
                a = dict(a, _lat=g["y"], _lng=g["x"])
            out.append(a)
        return _result(out, f"schools={len(out)}")


class _Walk(_Keyless):
    id = "epa-walkability"

    async def index(self, lng: float, lat: float) -> runtime.ProviderResult:
        rows = _attrs(await _get(WALK, _point(lng, lat, outFields="NatWalkInd,D4A,GEOID20,GEOID10"), "EPA walkability"))
        return _result(rows[0] if rows else None, f"features={len(rows)}")


class _Superfund(_Keyless):
    id = "epa-superfund"

    async def near(self, lng: float, lat: float, radius_km: float) -> runtime.ProviderResult:
        rows = _attrs(await _get(NPL, _point(lng, lat, distance=str(radius_km), units="esriSRUnit_Kilometer",
                                             outFields="PRIMARY_NAME,LOCATION_ADDRESS,CITY_NAME,STATE_CODE,LATITUDE83,"
                                                       "LONGITUDE83,ACTIVE_STATUS,FAC_URL"),
                                 "EPA Superfund sites"))
        return _result(rows, f"sites={len(rows)}")


def _num(v):
    if v in (None, ""):
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


@lru_cache(maxsize=1)
def _acs() -> dict:
    with gzip.open(ACS_FILE, "rt", encoding="utf-8", newline="") as fh:
        return {r["tract"]: {k: _num(v) for k, v in r.items() if k != "tract"} for r in csv.DictReader(fh)}


@lru_cache(maxsize=1)
def _fhfa() -> dict:
    with gzip.open(FHFA_FILE, "rt", encoding="utf-8", newline="") as fh:
        return {r["tract"]: {k: _num(v) for k, v in r.items() if k != "tract"} for r in csv.DictReader(fh)}


def acs_tract(tract: str) -> Optional[dict]:
    return _acs().get(tract)


def fhfa_tract(tract: str) -> Optional[dict]:
    return _fhfa().get(tract)


GEOCODE = _Geocoder()
RISK = _Risk()
RENTS = _Rents()
SCHOOLS_NEAR = _Schools()
WALKABILITY = _Walk()
SUPERFUND = _Superfund()
PROVIDERS = [GEOCODE, RISK, RENTS, SCHOOLS_NEAR, WALKABILITY, SUPERFUND]
