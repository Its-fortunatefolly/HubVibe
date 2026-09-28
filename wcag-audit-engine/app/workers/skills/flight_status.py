"""travel.flight_status -- what is happening at an airport, or to a flight, right now.

An airport code (IATA or ICAO) gets, in parallel: FAA ground stops, ground
delay programs, closures and delays (US airports); the live departure and
arrival board (Norway, Avinor); the current METAR and TAF (any airport with
an ICAO id); and the aircraft in the air around it (adsb.lol). A callsign
gets that aircraft's live position. Each section says whether its source
covers the airport, and a source that fails is named, never guessed.
"""

import asyncio
import re
from datetime import datetime, timezone

from .. import runtime
from ..providers import aviation as A

_CODE = re.compile(r"^[A-Za-z0-9]{3,4}$")
_CALLSIGN = re.compile(r"^[A-Za-z0-9]{2,8}$")
DIRECTIONS = ("both", "departures", "arrivals")
MAX_AIRCRAFT = 25


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    code, callsign = payload.get("airport"), payload.get("callsign")
    if code is None and callsign is None:
        raise runtime.InvalidRequest("Send `airport` (IATA like OSL or ICAO like ENGM) or `callsign` (like SAS1411).")
    place = None
    if code is not None:
        if not isinstance(code, str) or not _CODE.match(code.strip()):
            raise runtime.InvalidRequest("`airport` must be a 3-letter IATA or 4-letter ICAO code.")
        place = A.airport(code)
        if place is None:
            raise runtime.InvalidRequest(f"No airport with the code {code.strip().upper()!r} is known.")
    if callsign is not None and (not isinstance(callsign, str) or not _CALLSIGN.match(callsign.strip())):
        raise runtime.InvalidRequest("`callsign` must be 2 to 8 letters and digits, as broadcast (SAS1411, UAL123).")
    direction = payload.get("direction", "both")
    if direction not in DIRECTIONS:
        raise runtime.InvalidRequest(f"`direction` must be one of {list(DIRECTIONS)}.")
    hours = payload.get("hours_ahead", 3)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 12:
        raise runtime.InvalidRequest("`hours_ahead` must be a whole number from 1 to 12.")
    flight = payload.get("flight")
    if flight is not None and (not isinstance(flight, str) or not _CALLSIGN.match(flight.strip())):
        raise runtime.InvalidRequest("`flight` must be a flight number such as SK344.")
    return {"airport": place, "callsign": callsign.strip().upper() if callsign else None, "direction": direction,
            "hours": hours, "flight": flight.strip().upper() if flight else None}


def precheck(payload: dict) -> None:
    parse(payload)


def _minutes(value):
    return int(round(value)) if isinstance(value, (int, float)) else None


def delays(event: dict, checked: bool):
    if not checked:
        return None
    if not event:
        return {"status": "normal", "ground_stop": None, "ground_delay": None, "closure": None,
                "arrival_delay": None, "departure_delay": None, "deicing": False, "runways": None, "notices": [],
                "source": "FAA NAS Status"}
    gs, gd, cl = event.get("groundStop"), event.get("groundDelay"), event.get("airportClosure")
    ad, dd, cfg, ff = event.get("arrivalDelay"), event.get("departureDelay"), event.get("airportConfig"), event.get("freeForm")

    def span(d):
        return {"reason": d.get("impactingCondition") or d.get("reason"), "from": d.get("startTime"),
                "until": d.get("endTime") or d.get("programExpirationTime")}
    out = {
        "status": "ground_stop" if gs else "ground_delay" if gd else "closed" if cl else
                  "delays" if (ad or dd) else "normal",
        "ground_stop": span(gs) if gs else None,
        "ground_delay": dict(span(gd), avg_minutes=_minutes(gd.get("avgDelay")), max_minutes=_minutes(gd.get("maxDelay")))
                        if gd else None,
        "closure": dict(span(cl), text=cl.get("simpleText") or cl.get("text")) if cl else None,
        "arrival_delay": dict(span(ad), min_minutes=_minutes(ad.get("arrivalDeparture", {}).get("min") if isinstance(ad.get("arrivalDeparture"), dict) else ad.get("minDelay")),
                              max_minutes=_minutes(ad.get("maxDelay"))) if ad else None,
        "departure_delay": dict(span(dd), max_minutes=_minutes(dd.get("maxDelay"))) if dd else None,
        "deicing": bool(event.get("deicing")),
        "runways": ({"arrival": cfg.get("arrivalRunwayConfig"), "departure": cfg.get("departureRunwayConfig"),
                     "arrival_rate_per_hour": cfg.get("arrivalRate")} if cfg else None),
        "notices": [ff.get("simpleText") or ff.get("text")] if ff and (ff.get("simpleText") or ff.get("text")) else [],
        "source": "FAA NAS Status",
    }
    return out


def board(value, direction: str, flight):
    if value is None:
        return None
    rows = value["flights"]
    if flight:
        rows = [r for r in rows if (r.get("flight") or "").upper() == flight]
    for r in rows:
        other = A.airport(r["other_airport"]) if r.get("other_airport") else None
        r["other_airport_name"] = other["name"] if other else None
    deps = [r for r in rows if r.get("direction") == "D"]
    arrs = [r for r in rows if r.get("direction") == "A"]
    key = lambda r: r.get("scheduled") or ""  # noqa: E731
    return {"departures": sorted(deps, key=key) if direction in ("both", "departures") else [],
            "arrivals": sorted(arrs, key=key) if direction in ("both", "arrivals") else [],
            "last_update": value.get("last_update"),
            "attribution": A.AVINOR_ATTRIBUTION, "attribution_url": A.AVINOR_URL}


def weather(value):
    if not value or not value.get("metar"):
        return None
    m, t = value["metar"], value.get("taf") or {}
    return {"metar": m.get("rawOb"), "taf": t.get("rawTAF"), "flight_category": m.get("fltCat"),
            "temperature_c": m.get("temp"), "dewpoint_c": m.get("dewp"), "wind_dir_deg": m.get("wdir"),
            "wind_kt": m.get("wspd"), "gust_kt": m.get("wgst"),
            "visibility": str(m["visib"]) if m.get("visib") is not None else None,
            "weather": m.get("wxString"), "observed_at": m.get("reportTime"),
            "source": "NOAA Aviation Weather Center"}


def aircraft(rows, center=None):
    out = []
    for a in rows or []:
        if not isinstance(a, dict) or not isinstance(a.get("lat"), (int, float)):
            continue
        alt = a.get("alt_baro")
        out.append({"callsign": (a.get("flight") or "").strip() or None, "hex": a.get("hex"),
                    "registration": a.get("r"), "type": a.get("t"), "lat": a["lat"], "lon": a["lon"],
                    "altitude_ft": alt if isinstance(alt, (int, float)) else None, "on_ground": alt == "ground",
                    "ground_speed_kt": a.get("gs"), "track_deg": a.get("track"),
                    "vertical_rate_fpm": a.get("baro_rate"), "squawk": a.get("squawk"),
                    "distance_km": A.distance_km(center[0], center[1], a["lat"], a["lon"]) if center else None})
    if center:
        out.sort(key=lambda x: x["distance_km"])
    return out[:MAX_AIRCRAFT]


async def status(ctx, payload: dict) -> dict:
    req = parse(payload)
    place, notes, failed = req["airport"], [], []

    async def step(name, provider, fn):
        try:
            return await ctx.run(name, [provider], fn, per_attempt_seconds=15, max_attempts=2)
        except runtime.WorkerError as exc:
            failed.append(name)
            notes.append(f"{name}: {getattr(exc, 'detail', exc)}"[:200])
            return None

    async def none():
        return None

    us = bool(place and place["country"] == "US" and place.get("iata"))
    norway = bool(place and place["country"] == "NO" and place.get("iata"))
    avinor_dir = {"both": "", "departures": "D", "arrivals": "A"}[req["direction"]]
    jobs = {
        "delays": step("delays", A.FAA, lambda p: p.events(place["iata"])) if us else none(),
        "board": step("board", A.AVINOR_BOARD, lambda p: p.board(place["iata"], avinor_dir, 1, req["hours"])) if norway else none(),
        "weather": step("weather", A.WEATHER, lambda p: p.weather(place["icao"])) if place and place.get("icao") else none(),
        "nearby": step("aircraft", A.AIRCRAFT, lambda p: p.around(place["lat"], place["lon"], 25)) if place else none(),
        "callsign": step("callsign", A.AIRCRAFT, lambda p: p.callsign(req["callsign"])) if req["callsign"] else none(),
    }
    got = dict(zip(jobs, await asyncio.gather(*jobs.values())))

    faa = got["delays"]
    board_out = board(got["board"], req["direction"], req["flight"])
    weather_out = weather(got["weather"])
    planes = aircraft(got["callsign"]) if req["callsign"] else aircraft(got["nearby"], (place["lat"], place["lon"]) if place else None)
    if req["callsign"] and "callsign" not in failed and not planes:
        notes.append(f"{req['callsign']} is not transmitting a position right now (on the ground, not yet departed, "
                     "or out of receiver range).")
    if place and not us and not norway:
        notes.append("Live delay programs cover US airports (FAA) and live boards cover Norway (Avinor); this airport "
                     "gets weather and the aircraft around it.")
    attribution = []
    if board_out is not None:
        attribution.append({"text": A.AVINOR_ATTRIBUTION, "url": A.AVINOR_URL})
    if planes or "aircraft" in jobs or req["callsign"]:
        attribution.append({"text": A.ADSB_ATTRIBUTION, "url": "https://adsb.lol"})
    return {
        "query": {"airport": payload.get("airport"), "callsign": req["callsign"], "flight": req["flight"],
                  "direction": req["direction"], "hours_ahead": req["hours"]},
        "airport": place,
        "delays": delays((faa or {}).get("entry"), faa is not None),
        "board": board_out,
        "weather": weather_out,
        "aircraft": planes,
        "aircraft_scope": "callsign" if req["callsign"] else ("within 25 nautical miles" if place else None),
        "coverage": {"delays": us, "board": norway, "weather": bool(place and place.get("icao")),
                     "aircraft": True},
        "attribution": attribution,
        "sections_failed": failed,
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"travel.flight_status": status}
PRECHECKS = {"travel.flight_status": precheck}
