"""traffic.route -- traffic-aware travel time and distance between two
places, from the Google Routes API (computeRoutes).

One request, one route: how long the trip takes with the traffic Google
sees now (or predicts for `departure_time`), how long it takes without
traffic, the difference, and the distance -- plus Google's own localized
texts, route description, warnings and travel advisory, passed through.

ALWAYS CURRENT: every call is a fresh computeRoutes request; `checked_at`
says when, `as_of` is the departure time the answer was computed for (the
time sent, or `checked_at` when the trip starts now). NEVER A GUESS: a
field Google leaves out is null; `delay_seconds` exists only when both
durations do.
"""

from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import google_routes

MAX_PLACE_CHARS = 300
TRAVEL_MODES = google_routes.TRAVEL_MODES
TRAFFIC = ("aware", "optimal", "none")
_ROUTING_PREFERENCE = {"aware": "TRAFFIC_AWARE", "optimal": "TRAFFIC_AWARE_OPTIMAL", "none": "TRAFFIC_UNAWARE"}
SOURCE = "google-routes"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _place(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise runtime.InvalidRequest(f"`{field}` is required: an address, a place name, or \"lat,lng\".")
    value = value.strip()
    if len(value) > MAX_PLACE_CHARS:
        raise runtime.InvalidRequest(f"`{field}` is {len(value)} characters, over the {MAX_PLACE_CHARS} limit.")
    google_routes.waypoint(value, field)  # coordinates out of range are refused here, before payment
    return value


def _departure_time(raw) -> Optional[str]:
    """RFC 3339 with a zone, in the future -> the UTC 'Z' form sent to Google."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest("`departure_time`, when given, must be an RFC 3339 timestamp such as 2026-09-27T16:30:00Z.")
    text = raw.strip()
    # Python 3.10's fromisoformat does not accept a trailing Z; spell it out.
    iso = text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise runtime.InvalidRequest(
            f"`departure_time` {text!r} is not an RFC 3339 timestamp (e.g. 2026-09-27T16:30:00Z).") from None
    if parsed.tzinfo is None:
        raise runtime.InvalidRequest("`departure_time` must carry a zone (Z or +hh:mm).")
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    if parsed <= datetime.now(timezone.utc):
        raise runtime.InvalidRequest("`departure_time` must be in the future; omit it to leave now.")
    return parsed.isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    origin = _place(payload, "origin")
    destination = _place(payload, "destination")
    travel_mode = payload.get("travel_mode", "DRIVE")
    if not isinstance(travel_mode, str) or travel_mode not in TRAVEL_MODES:
        raise runtime.InvalidRequest(f"`travel_mode` must be one of {list(TRAVEL_MODES)}.")
    traffic = payload.get("traffic", "aware")
    if not isinstance(traffic, str) or traffic not in TRAFFIC:
        raise runtime.InvalidRequest(f"`traffic` must be one of {list(TRAFFIC)}.")
    # The API accepts a routingPreference only with DRIVE and TWO_WHEELER;
    # for the other modes none is sent and the answer says so.
    if travel_mode in google_routes.TRAFFIC_MODES:
        routing_preference = _ROUTING_PREFERENCE[traffic]
    else:
        traffic, routing_preference = "none", None
    return {
        "origin": origin,
        "destination": destination,
        "travel_mode": travel_mode,
        "traffic": traffic,
        "routing_preference": routing_preference,
        "departure_time": _departure_time(payload.get("departure_time")),
    }


def precheck(payload: dict) -> None:
    parse(payload)


async def route(ctx, payload: dict) -> dict:
    req = parse(payload)

    async def call(provider):
        return await provider.compute(req["origin"], req["destination"], req["travel_mode"],
                                      departure_time=req["departure_time"],
                                      routing_preference=req["routing_preference"])

    value = await ctx.run("compute_routes", google_routes.PROVIDERS, call, per_attempt_seconds=25,
                          max_attempts=2)
    checked_at = _now()
    duration = value["duration_seconds"]
    static = value.get("static_duration_seconds")
    return {
        "origin": req["origin"],
        "destination": req["destination"],
        "travel_mode": req["travel_mode"],
        "traffic": req["traffic"],
        "departure_time": req["departure_time"],
        "distance_meters": value["distance_meters"],
        "duration_seconds": duration,
        "static_duration_seconds": static,
        "delay_seconds": (duration - static) if static is not None else None,
        "duration_text": value.get("duration_text"),
        "static_duration_text": value.get("static_duration_text"),
        "distance_text": value.get("distance_text"),
        "description": value.get("description"),
        "warnings": list(value.get("warnings") or []),
        "advisory": value.get("advisory"),
        "source": SOURCE,
        "as_of": req["departure_time"] or checked_at,
        "checked_at": checked_at,
    }


SKILLS = {"traffic.route": route}
PRECHECKS = {"traffic.route": precheck}
