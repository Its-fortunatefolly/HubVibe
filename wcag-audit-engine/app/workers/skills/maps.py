"""Places from Overture Maps (open data, through BigQuery), and routes via
Google's managed Maps Grounding Lite MCP server."""

import re
from datetime import datetime, timezone

from .. import runtime
from ..providers import maps_grounding
from ..providers import places as places_data
from . import weather as weather_skill

MAX_QUERY_CHARS = 300


_WHERE = re.compile(r"^(?P<what>.+?)\s+(?:near|in|around|at|close to)\s+(?P<where>.+)$", re.I)
MAX_RADIUS_M = 10_000


def parse_places(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise runtime.InvalidRequest("`query` is required: what to find, e.g. \"coffee near the Ferry Building, San Francisco\".")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is over the {MAX_QUERY_CHARS}-character limit.")
    near, lat, lng = payload.get("near"), payload.get("lat"), payload.get("lng")
    what, where = query, None
    if lat is not None or lng is not None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lng)) \
                or not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise runtime.InvalidRequest("`lat` and `lng` must both be given, as numbers.")
    elif isinstance(near, str) and near.strip():
        where = near.strip()
    else:
        m = _WHERE.match(query)
        if not m:
            raise runtime.InvalidRequest("Say where: \"coffee near Shibuya Station\", or send `near` (a place, address or "
                                         "airport code) or `lat` and `lng`.")
        what, where = m.group("what").strip(), m.group("where").strip()
    radius = payload.get("radius_m", 1000)
    if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not 50 <= radius <= MAX_RADIUS_M:
        raise runtime.InvalidRequest(f"`radius_m` must be from 50 to {MAX_RADIUS_M}.")
    limit = payload.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise runtime.InvalidRequest("`limit` must be a whole number from 1 to 50.")
    return {"query": query, "what": what, "where": re.sub(r"^the\s+", "", where, flags=re.I) if where else None,
            "lat": float(lat) if lat is not None else None, "lng": float(lng) if lng is not None else None,
            "radius": int(radius), "limit": limit}


def precheck_places(payload: dict) -> None:
    parse_places(payload)


async def places(ctx, payload: dict) -> dict:
    """Places matching `what` around a point, from Overture Maps (open data)."""
    req = parse_places(payload)
    center = await weather_skill.resolve(ctx, {"location": req["where"], "lat": req["lat"], "lng": req["lng"]})
    words = places_data.keywords(req["what"])
    notes = []
    if not words:
        notes.append("No category words in the query; the nearest places of any kind are listed.")

    async def search(radius):
        async def call(provider):
            return await provider.nearby(center["lat"], center["lon"], radius, words, req["limit"])
        return await ctx.run("overture_places", [places_data.OVERTURE], call, per_attempt_seconds=30, max_attempts=2)
    radius = req["radius"]
    found = await search(radius)
    if not found and radius < MAX_RADIUS_M:
        radius = min(radius * 4, MAX_RADIUS_M)
        found = await search(radius)
        notes.append(f"Nothing matched within {req['radius']} m; the search was widened to {radius} m.")
    if not found:
        notes.append("Overture lists no matching place in this area.")
    return {"query": req["query"], "what": req["what"],
            "near": {"name": center["name"], "lat": center["lat"], "lon": center["lon"], "source": center["source"]},
            "radius_m": radius, "places": found, "place_count": len(found),
            "attribution": [dict(places_data.CREDIT)], "notes": notes,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}


_TRAVEL_MODES = {"DRIVE", "WALK", "BICYCLE", "TRANSIT"}


async def route(ctx, payload: dict) -> dict:
    origin = (payload.get("origin") or "").strip()
    destination = (payload.get("destination") or "").strip()
    if not origin or not destination:
        raise runtime.InvalidRequest("`origin` and `destination` are required.")
    travel_mode = (payload.get("travel_mode") or "DRIVE").strip().upper()
    if travel_mode not in _TRAVEL_MODES:
        raise runtime.InvalidRequest(f"`travel_mode` must be one of {sorted(_TRAVEL_MODES)}.")

    async def call(provider):
        return await provider.compute_routes(origin, destination, travel_mode=travel_mode)

    result = await ctx.run("compute_routes", maps_grounding.PROVIDERS, call, per_attempt_seconds=25)
    return {"origin": origin, "destination": destination, "travel_mode": travel_mode,
           "result": result}


SKILLS = {"maps.places": places, "maps.route": route}
PRECHECKS = {"maps.places": precheck_places}
