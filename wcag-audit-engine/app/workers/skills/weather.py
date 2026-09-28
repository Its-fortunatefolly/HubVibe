"""maps.weather -- the weather at a place now and for the next seven days.

A place name, a US street address, an airport code or coordinates is
resolved to a point; MET Norway's forecast for that point gives the current
conditions, the next 24 hours and seven days; a US point adds the National
Weather Service's active alerts. Each answer carries the credits the
sources' licences ask for.
"""

import asyncio
import re
from collections import OrderedDict
from datetime import datetime, timezone

from .. import runtime
from ..providers import aviation, property_data
from ..providers import weather as WX

MAX_LOCATION_CHARS = 200
_CODE = re.compile(r"^[A-Za-z]{3,4}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    location, lat, lng = payload.get("location"), payload.get("lat"), payload.get("lng")
    if lat is not None or lng is not None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lng)) \
                or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise runtime.InvalidRequest("`lat` and `lng` must both be given, as numbers.")
        return {"location": location if isinstance(location, str) else None, "lat": float(lat), "lng": float(lng)}
    if not isinstance(location, str) or not location.strip():
        raise runtime.InvalidRequest("Send `location` (a place, US address or airport code) or `lat` and `lng`.")
    if len(location) > MAX_LOCATION_CHARS:
        raise runtime.InvalidRequest(f"`location` is over the {MAX_LOCATION_CHARS}-character limit.")
    return {"location": location.strip(), "lat": None, "lng": None}


def precheck(payload: dict) -> None:
    parse(payload)


async def resolve(ctx, req: dict) -> dict:
    if req["lat"] is not None:
        return {"name": req["location"], "lat": req["lat"], "lon": req["lng"], "country": None, "source": "coordinates"}
    text = req["location"]
    if _CODE.match(text) and (a := aviation.airport(text)):
        return {"name": a["name"], "lat": a["lat"], "lon": a["lon"], "country": a["country"], "source": "airport code"}

    async def census(provider):
        return await provider.locate(text, None, None)

    async def wikidata(name):
        async def call(provider):
            return await provider.find(name)
        return await ctx.run("place", [WX.PLACES], call, per_attempt_seconds=15, max_attempts=2)
    streetish = text[:1].isdigit()
    if streetish:
        hit = await ctx.run("geocode", [property_data.GEOCODE], census, per_attempt_seconds=20, max_attempts=2)
        if hit:
            return {"name": hit["matched_address"], "lat": hit["lat"], "lon": hit["lng"], "country": "US",
                    "source": "US Census Geocoder"}
    hit = await wikidata(text)
    if hit is None and "," in text:
        hit = await wikidata(text.split(",")[0].strip())
    if hit:
        return {"name": hit["name"] + (f" ({hit['description']})" if hit.get("description") else ""),
                "lat": hit["lat"], "lon": hit["lon"], "country": None, "source": f"Wikidata {hit['wikidata']}"}
    if not streetish:
        hit = await ctx.run("geocode", [property_data.GEOCODE], census, per_attempt_seconds=20, max_attempts=2)
        if hit:
            return {"name": hit["matched_address"], "lat": hit["lat"], "lon": hit["lng"], "country": "US",
                    "source": "US Census Geocoder"}
    raise runtime.InvalidRequest(f"No place called {text!r} could be located. Send `lat` and `lng`. Nothing was charged.")


def _in_us(lat: float, lon: float) -> bool:
    return (18 <= lat <= 72 and -180 <= lon <= -65) or (13 <= lat <= 22 and 144 <= lon <= 146)


def shape(data: dict, lon: float = 0.0) -> tuple:
    props = (data or {}).get("properties") or {}
    series = props.get("timeseries") or []
    if not series:
        raise runtime.InvalidProviderResponse("MET Norway returned no forecast for this point.")

    def point(entry):
        d = (entry.get("data") or {})
        inst = (d.get("instant") or {}).get("details") or {}
        n1 = d.get("next_1_hours") or {}
        n6 = d.get("next_6_hours") or {}
        return {"time": entry.get("time"), "temperature_c": inst.get("air_temperature"),
                "wind_speed_ms": inst.get("wind_speed"), "wind_direction_deg": inst.get("wind_from_direction"),
                "humidity_pct": inst.get("relative_humidity"), "cloud_cover_pct": inst.get("cloud_area_fraction"),
                "pressure_hpa": inst.get("air_pressure_at_sea_level"),
                "condition": ((n1.get("summary") or n6.get("summary") or {}).get("symbol_code")),
                "precipitation_mm": (n1.get("details") or {}).get("precipitation_amount",
                                                                 (n6.get("details") or {}).get("precipitation_amount"))}
    current = point(series[0])
    hourly = [point(e) for e in series[:24]]
    days = OrderedDict()
    noon = round(12 - lon / 15) % 24
    for e in series:
        p = point(e)
        day = (p["time"] or "")[:10]
        slot = days.setdefault(day, {"date": day, "min_c": None, "max_c": None, "precipitation_mm": 0.0, "condition": None})
        t = p["temperature_c"]
        if isinstance(t, (int, float)):
            slot["min_c"] = t if slot["min_c"] is None else min(slot["min_c"], t)
            slot["max_c"] = t if slot["max_c"] is None else max(slot["max_c"], t)
        n1 = ((e.get("data") or {}).get("next_1_hours") or {}).get("details") or {}
        if isinstance(n1.get("precipitation_amount"), (int, float)):
            slot["precipitation_mm"] = round(slot["precipitation_mm"] + n1["precipitation_amount"], 1)
        # the day's condition is the one nearest local midday (UTC hour 12 - lon/15)
        hour = int((p["time"] or "T00")[11:13] or 0)
        gap = min((hour - noon) % 24, (noon - hour) % 24)
        if p["condition"] and (slot["condition"] is None or gap < slot.get("_gap", 99)):
            slot["condition"], slot["_gap"] = p["condition"], gap
    daily = [{k: v for k, v in d.items() if k != "_gap"} for d in list(days.values())[:7]]
    return current, hourly, daily, (props.get("meta") or {}).get("updated_at")


async def weather(ctx, payload: dict) -> dict:
    req = parse(payload)
    place = await resolve(ctx, req)
    lat, lon = place["lat"], place["lon"]
    notes, failed = [], []

    async def forecast(provider):
        return await provider.forecast(lat, lon)

    async def alerts():
        if not _in_us(lat, lon):
            return None
        try:
            return await ctx.run("alerts", [WX.ALERTS], lambda p: p.active(lat, lon), per_attempt_seconds=15, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append("alerts")
            notes.append(f"alerts: {getattr(exc, 'detail', exc)}"[:200])
            return None
    data, found_alerts = await asyncio.gather(
        ctx.run("forecast", [WX.FORECAST], forecast, per_attempt_seconds=15, max_attempts=2), alerts())
    current, hourly, daily, updated = shape(data, lon)
    alerts_out = [{"event": (f.get("properties") or {}).get("event"), "severity": (f.get("properties") or {}).get("severity"),
                   "headline": (f.get("properties") or {}).get("headline"),
                   "effective": (f.get("properties") or {}).get("effective"),
                   "expires": (f.get("properties") or {}).get("expires")} for f in (found_alerts or [])]
    credits = [dict(WX.MET_CREDIT)] + ([dict(WX.NWS_CREDIT)] if found_alerts is not None else [])
    return {"location": req["location"], "place": place, "current": current, "hourly": hourly, "daily": daily,
            "alerts": alerts_out, "alerts_covered": found_alerts is not None,
            "units": {"temperature": "celsius", "wind_speed": "m/s", "precipitation": "mm", "pressure": "hPa"},
            "forecast_updated_at": updated, "attribution": credits, "sections_failed": failed, "notes": notes,
            "checked_at": _now()}


SKILLS = {"maps.weather": weather}
PRECHECKS = {"maps.weather": precheck}
