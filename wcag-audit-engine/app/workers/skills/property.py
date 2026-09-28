"""property.context -- everything the public record says about a US address, in one call.

The address (or a point) is matched by the Census Geocoder; then, in
parallel, FEMA's National Risk Index (flood and 17 other hazards), HUD's fair market rents
for the metro and the ZIP, nearby public schools, EPA walkability and
Superfund sites; plus the tract's ACS housing figures and FHFA price trend.
A source that fails is named in `sections_failed` and its section is null:
the rest still ships, and nothing is guessed to fill the gap.
"""

import asyncio
from datetime import datetime, timezone

from .. import runtime
from ..providers import property_data as P

MAX_SCHOOL_KM = 5.0
MAX_SUPERFUND_KM = 15.0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise runtime.InvalidRequest(f"`{name}` must be a number from {low} to {high}.")
    return float(value)


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    address = payload.get("address")
    lat, lng = payload.get("lat"), payload.get("lng")
    if address is not None:
        if not isinstance(address, str) or not 5 <= len(address.strip()) <= 200:
            raise runtime.InvalidRequest("`address` must be a US street address, 5 to 200 characters.")
        address = address.strip()
    elif lat is not None or lng is not None:
        lat = _number(lat, "lat", 17.0, 72.0)
        lng = _number(lng, "lng", -180.0, -64.0)
    else:
        raise runtime.InvalidRequest("Send `address` (a US street address) or `lat` and `lng`.")
    schools = _number(payload.get("school_radius_km", 1.5), "school_radius_km", 0.1, MAX_SCHOOL_KM)
    superfund = _number(payload.get("superfund_radius_km", 5.0), "superfund_radius_km", 0.1, MAX_SUPERFUND_KM)
    return {"address": address, "lat": lat, "lng": lng, "school_km": schools, "superfund_km": superfund}


def precheck(payload: dict) -> None:
    parse(payload)


def _first(geos: dict, *names):
    for name in names:
        rows = geos.get(name) or []
        if rows:
            return rows[0]
    return {}


def geography(geos: dict) -> dict:
    tract = _first(geos, "Census Tracts")
    county = _first(geos, "Counties")
    state = _first(geos, "States")
    cbsa = _first(geos, "Metropolitan Statistical Areas", "Micropolitan Statistical Areas")
    place = _first(geos, "Incorporated Places", "Census Designated Places")
    district = next((rows[0] for name, rows in geos.items() if name.endswith("Congressional Districts") and rows), {})
    school = _first(geos, "Unified School Districts", "Elementary School Districts", "Secondary School Districts")
    return {
        "state": state.get("NAME"), "state_fips": state.get("GEOID"),
        "county": county.get("NAME"), "county_fips": county.get("GEOID"),
        "tract": tract.get("GEOID"), "block_group": _first(geos, "Census Block Groups").get("GEOID"),
        "place": place.get("NAME"),
        "zip": _first(geos, "2020 Census ZIP Code Tabulation Areas").get("BASENAME")
               or _first(geos, "2020 Census ZIP Code Tabulation Areas").get("GEOID"),
        "metro": cbsa.get("NAME"), "metro_code": cbsa.get("GEOID"),
        "congressional_district": district.get("NAME"),
        "school_district": school.get("NAME"),
    }



def risk(row):
    if row is None:
        return None
    score = row.get("RISK_SCORE")
    return {"overall_rating": row.get("RISK_RATNG"),
            "overall_score": round(score, 2) if isinstance(score, (int, float)) else None,
            "expected_annual_loss_usd": round(row["EAL_VALT"]) if isinstance(row.get("EAL_VALT"), (int, float)) else None,
            "social_vulnerability": row.get("SOVI_RATNG"), "community_resilience": row.get("RESL_RATNG"),
            "hazards": {name: row.get(f"{code}_RISKR") for code, name in NRI_ORDER},
            "version": row.get("NRI_VER"), "notice": P.NRI_NOTICE}


NRI_ORDER = sorted(P.NRI_HAZARDS.items(), key=lambda kv: kv[1])


def rents(metro, zip_row, zcta):
    if metro is None and zip_row is None:
        return None
    out = {"area": (metro or {}).get("FMR_AREANAME"),
           "studio": (metro or {}).get("FMR_0BDR"), "one_bedroom": (metro or {}).get("FMR_1BDR"),
           "two_bedroom": (metro or {}).get("FMR_2BDR"), "three_bedroom": (metro or {}).get("FMR_3BDR"),
           "four_bedroom": (metro or {}).get("FMR_4BDR"), "zip": None}
    if zip_row:
        out["zip"] = {"zip": zcta, "studio": zip_row.get("SAFMR_0BR"), "one_bedroom": zip_row.get("SAFMR_1BR"),
                      "two_bedroom": zip_row.get("SAFMR_2BR"), "three_bedroom": zip_row.get("SAFMR_3BR"),
                      "four_bedroom": zip_row.get("SAFMR_4BR")}
    return out


def schools(rows: list, lat: float, lng: float) -> list:
    out = []
    for r in rows or []:
        lo, hi = r.get("GSLO"), r.get("GSHI")
        out.append({"name": r.get("SCH_NAME"), "level": r.get("SCHOOL_LEVEL"),
                    "grades": f"{lo}-{hi}" if lo and hi else None,
                    "enrollment": r.get("TOTAL") if isinstance(r.get("TOTAL"), (int, float)) and r["TOTAL"] >= 0 else None,
                    "students_per_teacher": r.get("STUTERATIO") if isinstance(r.get("STUTERATIO"), (int, float)) and r["STUTERATIO"] >= 0 else None,
                    "charter": r.get("CHARTER_TEXT") == "Yes", "district": r.get("LEA_NAME"), "city": r.get("LCITY"),
                    "distance_km": P.distance_km(lat, lng, r["_lat"], r["_lng"]) if "_lat" in r else None})
    return sorted(out, key=lambda s: (s["distance_km"] is None, s["distance_km"] or 0))


def superfund(rows: list, lat: float, lng: float) -> list:
    out = []
    for r in rows or []:
        slat, slng = r.get("LATITUDE83"), r.get("LONGITUDE83")
        out.append({"name": r.get("PRIMARY_NAME"), "address": r.get("LOCATION_ADDRESS"), "city": r.get("CITY_NAME"),
                    "state": r.get("STATE_CODE"), "status": r.get("ACTIVE_STATUS"), "url": r.get("FAC_URL"),
                    "distance_km": P.distance_km(lat, lng, slat, slng)
                    if isinstance(slat, (int, float)) and isinstance(slng, (int, float)) else None})
    return sorted(out, key=lambda s: (s["distance_km"] is None, s["distance_km"] or 0))


SOURCES = [
    {"name": "US Census Bureau Geocoder", "url": "https://geocoding.geo.census.gov/"},
    {"name": "FEMA National Risk Index", "url": "https://hazards.fema.gov/nri/"},
    {"name": "HUD Fair Market Rents", "url": "https://www.huduser.gov/portal/datasets/fmr.html"},
    {"name": P.ACS_LABEL, "url": "https://www.census.gov/programs-surveys/acs"},
    {"name": "FHFA House Price Index (annual, census tract)", "url": "https://www.fhfa.gov/data/hpi/datasets"},
    {"name": "NCES EDGE public school locations 2024-25", "url": "https://nces.ed.gov/programs/edge/"},
    {"name": "EPA National Walkability Index", "url": "https://www.epa.gov/smartgrowth/smart-location-mapping"},
    {"name": "EPA Superfund National Priorities List (FRS)", "url": "https://www.epa.gov/superfund"},
]


async def context(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes, failed = [], []

    async def locate(provider):
        return await provider.locate(req["address"], req["lat"], req["lng"])
    place = await ctx.run("geocode", [P.GEOCODE], locate, per_attempt_seconds=20, max_attempts=2)
    if place is None:
        raise runtime.InvalidRequest(
            "The Census Geocoder found no US match for this location. Check the street, city and state or ZIP, "
            "or send `lat` and `lng`. Nothing was charged.")
    lat, lng, geos = place["lat"], place["lng"], place["geographies"]
    geo = geography(geos)
    tract, zcta = geo["tract"], geo["zip"]

    async def step(name, provider, fn):
        try:
            return await ctx.run(name, [provider], fn, per_attempt_seconds=20, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append(name)
            notes.append(f"{name}: {exc.detail if hasattr(exc, 'detail') else exc}"[:200])
            return None

    async def none():
        return None

    jobs = {
        "risk": step("risk", P.RISK, lambda p: p.tract(tract)) if tract else none(),
        "rents": step("rents", P.RENTS, lambda p: p.metro(lng, lat)),
        "zip_rents": step("zip_rents", P.RENTS, lambda p: p.zip_area(zcta)) if zcta else none(),
        "schools": step("schools", P.SCHOOLS_NEAR, lambda p: p.near(lng, lat, req["school_km"])),
        "walkability": step("walkability", P.WALKABILITY, lambda p: p.index(lng, lat)),
        "superfund": step("superfund", P.SUPERFUND, lambda p: p.near(lng, lat, req["superfund_km"])),
    }
    got = dict(zip(jobs, await asyncio.gather(*jobs.values())))

    acs = P.acs_tract(tract) if tract else None
    housing = dict(acs, survey=P.ACS_LABEL, level="census tract") if acs else None
    if housing is None:
        notes.append("No ACS tract estimates for this tract.")
    trend = P.fhfa_tract(tract) if tract else None
    walk = got["walkability"]
    if place.get("candidates", 1) > 1:
        notes.append(f"The geocoder matched {place['candidates']} addresses; the first is used.")
    return {
        "input": {"address": req["address"], "lat": req["lat"], "lng": req["lng"]},
        "matched_address": place["matched_address"],
        "lat": lat, "lng": lng,
        "geography": geo,
        "hazard_risk": risk(got["risk"]),
        "housing": housing,
        "price_trend": dict(trend, source="FHFA House Price Index, annual, census tract") if trend else None,
        "fair_market_rent": rents(got["rents"], got["zip_rents"], zcta),
        "schools": schools(got["schools"], lat, lng),
        "school_radius_km": req["school_km"],
        "walkability": ({"index": round(walk["NatWalkInd"], 2) if isinstance(walk.get("NatWalkInd"), (int, float)) else None,
                         "meters_to_transit": walk.get("D4A") if isinstance(walk.get("D4A"), (int, float)) and walk["D4A"] >= 0 else None,
                         "block_group": walk.get("GEOID20") or walk.get("GEOID10")} if walk else None),
        "superfund_sites": superfund(got["superfund"], lat, lng),
        "superfund_radius_km": req["superfund_km"],
        "sources": SOURCES,
        "sections_failed": failed,
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"property.context": context}
PRECHECKS = {"property.context": precheck}
