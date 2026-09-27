"""travel.flights -- live flight offers between two places, any language in.

An agent asks "what can I fly, when, for how much, on what terms" and gets
the airlines' own live offers: price with tax split out, every segment,
refund and change conditions, emissions, and exactly how long each offer is
valid. Places may be IATA codes or names in any language (東京, Séoul,
Nueva York); names are resolved through the provider's place index first.

Nothing is cached: every call is a fresh offer request, `expires_at` on each
offer is the airline's own deadline, and `live_mode` says whether these are
bookable offers (true) or the provider's practice data (false; never sold on
the public node, see providers/duffel.py).
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from ..providers import duffel

MAX_OFFERS = 50
MAX_DAYS_AHEAD = 365
MAX_ADULTS = 9
MAX_CHILDREN = 8
CABIN_CLASSES = ("economy", "premium_economy", "business", "first")
SORTS = ("price", "duration")
_IATA = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def duration_seconds(text) -> Optional[int]:
    """ISO 8601 duration (PT7H58M) -> seconds; None when absent or odd."""
    if not isinstance(text, str):
        return None
    m = _ISO_DURATION.match(text.strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _place_field(raw, field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise runtime.InvalidRequest(f"`{field}` is required: an IATA code (LHR) or a place name in any language.")
    value = value.strip()
    if len(value) > 80:
        raise runtime.InvalidRequest(f"`{field}` is over 80 characters.")
    return value


def _parse_date(raw, field: str, today: date) -> Optional[date]:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.match(value.strip()):
        raise runtime.InvalidRequest(f"`{field}` must be a date written YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise runtime.InvalidRequest(f"`{field}` is not a real calendar date.") from None
    if parsed < today:
        raise runtime.InvalidRequest(f"`{field}` {parsed.isoformat()} is in the past (today is {today.isoformat()} UTC).")
    if parsed > today + timedelta(days=MAX_DAYS_AHEAD):
        raise runtime.InvalidRequest(f"`{field}` is more than {MAX_DAYS_AHEAD} days ahead; airlines do not publish that far.")
    return parsed


def _int(raw, field: str, default: int, lo: int, hi: int) -> int:
    value = raw.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise runtime.InvalidRequest(f"`{field}` must be a whole number from {lo} to {hi}.")
    return value


def parse(payload: dict, today: Optional[date] = None) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    today = today or datetime.now(timezone.utc).date()
    origin = _place_field(payload, "origin")
    destination = _place_field(payload, "destination")
    if origin.upper() == destination.upper():
        raise runtime.InvalidRequest("`origin` and `destination` are the same place.")
    departure = _parse_date(payload, "departure_date", today)
    days_ahead = payload.get("days_ahead")
    if departure is None:
        if days_ahead is None:
            raise runtime.InvalidRequest("Give `departure_date` (YYYY-MM-DD) or `days_ahead` (whole days from today).")
        days_ahead = _int(payload, "days_ahead", 30, 0, MAX_DAYS_AHEAD)
        departure = today + timedelta(days=days_ahead)
    elif days_ahead is not None:
        raise runtime.InvalidRequest("Give either `departure_date` or `days_ahead`, not both.")
    return_date = _parse_date(payload, "return_date", today)
    if return_date is not None and return_date < departure:
        raise runtime.InvalidRequest("`return_date` is before `departure_date`.")
    adults = _int(payload, "adults", 1, 1, MAX_ADULTS)
    ages = payload.get("children_ages", [])
    if not isinstance(ages, list) or len(ages) > MAX_CHILDREN or any(
            isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 17 for a in ages):
        raise runtime.InvalidRequest(f"`children_ages` must be a list of up to {MAX_CHILDREN} whole ages from 0 to 17.")
    cabin = payload.get("cabin_class", "economy")
    if cabin not in CABIN_CLASSES:
        raise runtime.InvalidRequest(f"`cabin_class` must be one of {list(CABIN_CLASSES)}.")
    max_connections = payload.get("max_connections")
    if max_connections is not None:
        max_connections = _int(payload, "max_connections", 2, 0, 2)
    sort = payload.get("sort", "price")
    if sort not in SORTS:
        raise runtime.InvalidRequest(f"`sort` must be one of {list(SORTS)}.")
    return {
        "origin": origin, "destination": destination,
        "departure_date": departure.isoformat(),
        "return_date": return_date.isoformat() if return_date else None,
        "adults": adults, "children_ages": list(ages), "cabin_class": cabin,
        "max_connections": max_connections,
        "max_offers": _int(payload, "max_offers", 10, 1, MAX_OFFERS), "sort": sort,
    }


def precheck(payload: dict) -> None:
    parse(payload)


def passengers_for(adults: int, children_ages: list) -> list:
    out = [{"type": "adult"} for _ in range(adults)]
    for age in children_ages:
        # Airlines price under-2s as infants; every other child by exact age.
        out.append({"type": "infant_without_seat"} if age < 2 else {"age": age})
    return out


def pick_place(places: list, query: str) -> Optional[dict]:
    """The best match for a name: an exact IATA hit, else the first city,
    else the first airport -- cities let the airline search every airport."""
    upper = query.strip().upper()
    for p in places:
        if (p.get("iata_code") or "").upper() == upper:
            return p
    for kind in ("city", "airport"):
        for p in places:
            if p.get("type") == kind and p.get("iata_code"):
                return p
    return None


def _place_record(place: dict, given: str) -> dict:
    return {
        "iata_code": place.get("iata_code"),
        "name": place.get("name"),
        "type": place.get("type"),
        "city_name": place.get("city_name") or (place.get("name") if place.get("type") == "city" else None),
        "country_code": place.get("iata_country_code"),
        "given": given,
    }


def _money(value) -> Optional[float]:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _airport(raw) -> dict:
    raw = raw or {}
    return {"iata_code": raw.get("iata_code"), "name": raw.get("name"), "city_name": raw.get("city_name")}


def _carrier(raw) -> dict:
    raw = raw or {}
    return {"iata_code": raw.get("iata_code"), "name": raw.get("name")}


def _condition(raw) -> dict:
    raw = raw or {}
    return {"allowed": raw.get("allowed") if isinstance(raw.get("allowed"), bool) else None,
            "penalty_amount": _money(raw.get("penalty_amount")),
            "penalty_currency": raw.get("penalty_currency")}


def normalize_offer(offer: dict) -> dict:
    slices = []
    stops = 0
    total_seconds = 0
    for sl in offer.get("slices") or []:
        segments = []
        for sg in sl.get("segments") or []:
            aircraft = sg.get("aircraft") or {}
            segments.append({
                "carrier": _carrier(sg.get("marketing_carrier")),
                "flight_number": sg.get("marketing_carrier_flight_number"),
                "operating_carrier": _carrier(sg.get("operating_carrier")),
                "origin": _airport(sg.get("origin")),
                "destination": _airport(sg.get("destination")),
                "departing_at": sg.get("departing_at"),
                "arriving_at": sg.get("arriving_at"),
                "duration_seconds": duration_seconds(sg.get("duration")),
                "aircraft": aircraft.get("name") if isinstance(aircraft, dict) else None,
                "distance_km": (round(float(sg["distance"]), 1) if sg.get("distance") not in (None, "") else None),
            })
        secs = duration_seconds(sl.get("duration"))
        total_seconds += secs or 0
        stops = max(stops, max(len(segments) - 1, 0))
        slices.append({
            "origin": _airport(sl.get("origin")),
            "destination": _airport(sl.get("destination")),
            "duration_seconds": secs,
            "fare_brand": sl.get("fare_brand_name"),
            "segments": segments,
        })
    conditions = offer.get("conditions") or {}
    payment = offer.get("payment_requirements") or {}
    return {
        "id": offer.get("id"),
        "airline": _carrier(offer.get("owner")),
        "total_amount": _money(offer.get("total_amount")),
        "base_amount": _money(offer.get("base_amount")),
        "tax_amount": _money(offer.get("tax_amount")),
        "currency": offer.get("total_currency"),
        "expires_at": offer.get("expires_at"),
        "slices": slices,
        "stops": stops,
        "total_duration_seconds": total_seconds or None,
        "refund_before_departure": _condition(conditions.get("refund_before_departure")),
        "change_before_departure": _condition(conditions.get("change_before_departure")),
        "emissions_kg": (float(offer["total_emissions_kg"]) if offer.get("total_emissions_kg") not in (None, "") else None),
        "instant_payment_required": bool(payment.get("requires_instant_payment")),
        "price_guarantee_expires_at": payment.get("price_guarantee_expires_at"),
        "identity_documents_required": bool(offer.get("passenger_identity_documents_required")),
    }


def sort_offers(offers: list, sort: str) -> list:
    if sort == "duration":
        return sorted(offers, key=lambda o: (o["total_duration_seconds"] is None, o["total_duration_seconds"] or 0,
                                             o["total_amount"] or 0))
    return sorted(offers, key=lambda o: (o["total_amount"] is None, o["total_amount"] or 0,
                                         o["total_duration_seconds"] or 0))


async def _resolve(ctx, given: str, field: str) -> dict:
    if _IATA.match(given.upper()) and given.upper() == given.strip().upper() and len(given.strip()) == 3:
        code = given.strip().upper()
        return {"iata_code": code, "name": None, "type": None, "city_name": None, "country_code": None,
                "given": given}

    async def call(provider):
        return await provider.places(given)

    places = await ctx.run(f"place:{field}", duffel.PROVIDERS, call, per_attempt_seconds=20, max_attempts=2)
    place = pick_place(places, given)
    if place is None:
        raise runtime.InvalidRequest(f"`{field}` {given!r} matched no airport or city.")
    return _place_record(place, given)


async def flights(ctx, payload: dict) -> dict:
    req = parse(payload)
    origin = await _resolve(ctx, req["origin"], "origin")
    destination = await _resolve(ctx, req["destination"], "destination")
    if origin["iata_code"] == destination["iata_code"]:
        raise runtime.InvalidRequest("`origin` and `destination` resolve to the same place.")
    slices = [{"origin": origin["iata_code"], "destination": destination["iata_code"],
               "departure_date": req["departure_date"]}]
    if req["return_date"]:
        slices.append({"origin": destination["iata_code"], "destination": origin["iata_code"],
                       "departure_date": req["return_date"]})
    passengers = passengers_for(req["adults"], req["children_ages"])

    async def call(provider):
        return await provider.offers(slices, passengers, cabin_class=req["cabin_class"],
                                     max_connections=req["max_connections"])

    answer = await ctx.run("offers", duffel.PROVIDERS, call, per_attempt_seconds=45, max_attempts=1)
    offers = sort_offers([normalize_offer(o) for o in answer["offers"]], req["sort"])
    shown = offers[:req["max_offers"]]
    priced = [o for o in offers if o["total_amount"] is not None]
    timed = [o for o in offers if o["total_duration_seconds"]]
    notes = []
    if not answer["live_mode"]:
        notes.append("Practice offers from the provider's test mode: not bookable, prices are not real.")
    if not offers:
        notes.append("No airline returned an offer for this search.")
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": req["departure_date"],
        "return_date": req["return_date"],
        "passengers": {"adults": req["adults"], "children_ages": req["children_ages"]},
        "cabin_class": req["cabin_class"],
        "sort": req["sort"],
        "offers": shown,
        "offer_count": len(shown),
        "offers_available": len(offers),
        "cheapest_total": ({"amount": min(o["total_amount"] for o in priced),
                            "currency": priced[0]["currency"]} if priced else None),
        "fastest_duration_seconds": (min(o["total_duration_seconds"] for o in timed) if timed else None),
        "live_mode": answer["live_mode"],
        "offer_request_id": answer["id"],
        "source": "duffel",
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"travel.flights": flights}
PRECHECKS = {"travel.flights": precheck}
