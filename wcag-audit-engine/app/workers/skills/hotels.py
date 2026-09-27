"""travel.hotels -- live hotel availability and room rates for a place and
dates, from 2M+ properties, any language in.

Two calls to the provider inside one job: find the hotels (a free-text
place in any language, an English city plus country, a hotel name, or
coordinates), then one rates request for the dates and guests. Each hotel
comes back with its cheapest bookable rate and its room options: board,
occupancy, total with taxes and fees itemised, refundability and the
cancellation deadlines, plus the offer id an agent quotes to book.

`live_mode` is true only on a production key; sandbox rates are never sold
on the public node (see providers/liteapi.py). Nothing is cached.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from ..providers import liteapi

MAX_HOTELS = liteapi.MAX_HOTELS
MAX_NIGHTS = 30
MAX_DAYS_AHEAD = 365
MAX_ADULTS = 8
MAX_CHILDREN = 6
MAX_ROOMS_SHOWN = 5
SORTS = ("price", "rating")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO2 = re.compile(r"^[A-Za-z]{2}$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _int(raw, field, default, lo, hi):
    value = raw.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise runtime.InvalidRequest(f"`{field}` must be a whole number from {lo} to {hi}.")
    return value


def _date(raw, field, today):
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
        raise runtime.InvalidRequest(f"`{field}` is more than {MAX_DAYS_AHEAD} days ahead.")
    return parsed


def _text(raw, field, max_len):
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise runtime.InvalidRequest(f"`{field}`, when given, must be text.")
    value = " ".join(value.split())
    if len(value) > max_len:
        raise runtime.InvalidRequest(f"`{field}` is over {max_len} characters.")
    return value


def parse(payload: dict, today: Optional[date] = None) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    today = today or datetime.now(timezone.utc).date()
    place = _text(payload, "place", 200)
    city = _text(payload, "city", 80)
    hotel_name = _text(payload, "hotel_name", 120)
    country = payload.get("country_code")
    if country is not None:
        if not isinstance(country, str) or not _ISO2.match(country.strip()):
            raise runtime.InvalidRequest("`country_code` must be an ISO 3166-1 alpha-2 code (JP, KR, GB).")
        country = country.strip().upper()
    lat, lon = payload.get("latitude"), payload.get("longitude")
    if (lat is None) != (lon is None):
        raise runtime.InvalidRequest("Give both `latitude` and `longitude`, or neither.")
    if lat is not None:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lat, lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise runtime.InvalidRequest("`latitude`/`longitude` must be decimal degrees.")
    radius = _int(payload, "radius_m", 5000, 100, 50000)
    if city and not country:
        raise runtime.InvalidRequest("`city` needs `country_code` (the provider indexes cities per country).")
    if not any((place, city, hotel_name, lat is not None)):
        raise runtime.InvalidRequest("Give `place` (free text, any language), or `city` + `country_code`, or `hotel_name`, or coordinates.")
    check_in = _date(payload, "check_in", today)
    days_ahead = payload.get("days_ahead")
    if check_in is None:
        days_ahead = _int(payload, "days_ahead", 30, 0, MAX_DAYS_AHEAD)
        check_in = today + timedelta(days=days_ahead)
    elif days_ahead is not None:
        raise runtime.InvalidRequest("Give either `check_in` or `days_ahead`, not both.")
    check_out = _date(payload, "check_out", today)
    if check_out is None:
        nights = _int(payload, "nights", 2, 1, MAX_NIGHTS)
        check_out = check_in + timedelta(days=nights)
    else:
        if payload.get("nights") is not None:
            raise runtime.InvalidRequest("Give either `check_out` or `nights`, not both.")
        nights = (check_out - check_in).days
        if not 1 <= nights <= MAX_NIGHTS:
            raise runtime.InvalidRequest(f"`check_out` must be 1 to {MAX_NIGHTS} nights after `check_in`.")
    adults = _int(payload, "adults", 2, 1, MAX_ADULTS)
    ages = payload.get("children_ages", [])
    if not isinstance(ages, list) or len(ages) > MAX_CHILDREN or any(isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 17 for a in ages):
        raise runtime.InvalidRequest(f"`children_ages` must be a list of up to {MAX_CHILDREN} whole ages from 0 to 17.")
    currency = payload.get("currency", "USD")
    if not isinstance(currency, str) or not _CURRENCY.match(currency.strip()):
        raise runtime.InvalidRequest("`currency` must be an ISO 4217 code (USD, JPY, EUR).")
    nationality = payload.get("guest_nationality", "US")
    if not isinstance(nationality, str) or not _ISO2.match(nationality.strip()):
        raise runtime.InvalidRequest("`guest_nationality` must be an ISO 3166-1 alpha-2 code.")
    sort = payload.get("sort", "price")
    if sort not in SORTS:
        raise runtime.InvalidRequest(f"`sort` must be one of {list(SORTS)}.")
    return {"place": place, "city": city, "country_code": country, "hotel_name": hotel_name,
            "latitude": lat, "longitude": lon, "radius_m": radius,
            "check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "nights": nights,
            "adults": adults, "children_ages": list(ages), "currency": currency.strip().upper(),
            "guest_nationality": nationality.strip().upper(),
            "max_hotels": _int(payload, "max_hotels", 10, 1, MAX_HOTELS), "sort": sort}


def precheck(payload: dict) -> None:
    parse(payload)


def _amount(entries) -> tuple:
    """LiteAPI writes money as [{amount, currency}]; take the first."""
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        try:
            return round(float(entries[0].get("amount")), 2), entries[0].get("currency")
        except (TypeError, ValueError):
            return None, entries[0].get("currency")
    return None, None


def _room(room_type: dict, rate: dict) -> dict:
    retail = rate.get("retailRate") or {}
    total, currency = _amount(retail.get("total"))
    fees = []
    for f in retail.get("taxesAndFees") or []:
        if isinstance(f, dict):
            fees.append({"description": f.get("description"), "amount": (round(float(f["amount"]), 2) if f.get("amount") is not None else None),
                         "currency": f.get("currency"), "included": bool(f.get("included"))})
    policy = rate.get("cancellationPolicies") or {}
    tag = policy.get("refundableTag")
    refundable = True if tag == "RFN" else False if tag == "NRFN" else None
    cancellation = []
    for c in policy.get("cancelPolicyInfos") or []:
        if isinstance(c, dict):
            cancellation.append({"from": c.get("cancelTime"), "penalty_amount": (round(float(c["amount"]), 2) if c.get("amount") is not None else None),
                                 "currency": c.get("currency"), "type": c.get("type")})
    cancellation.sort(key=lambda c: c.get("from") or "")
    free_until = cancellation[0]["from"] if refundable and cancellation else None
    return {
        "name": rate.get("name"),
        "board": rate.get("boardName"),
        "max_occupancy": rate.get("maxOccupancy"),
        "total": total,
        "currency": currency,
        "taxes_and_fees": fees,
        "refundable": refundable,
        "cancel_free_until": free_until,
        "cancellation": cancellation,
        "offer_id": room_type.get("offerId"),
        "rate_id": rate.get("rateId"),
    }


def _hotel(h: dict, rooms: list) -> dict:
    priced = [r for r in rooms if r["total"] is not None]
    priced.sort(key=lambda r: r["total"])
    cheapest = priced[0] if priced else None
    return {
        "id": h.get("id"),
        "name": h.get("name"),
        "stars": h.get("stars"),
        "rating": h.get("rating"),
        "review_count": h.get("reviewCount"),
        "address": h.get("address"),
        "city": h.get("city"),
        "country_code": h.get("country"),
        "latitude": h.get("latitude"),
        "longitude": h.get("longitude"),
        "cheapest": ({"total": cheapest["total"], "currency": cheapest["currency"], "board": cheapest["board"],
                      "refundable": cheapest["refundable"], "cancel_free_until": cheapest["cancel_free_until"],
                      "offer_id": cheapest["offer_id"]} if cheapest else None),
        "rooms": priced[:MAX_ROOMS_SHOWN],
        "room_options_available": len(rooms),
    }


async def hotels(ctx, payload: dict) -> dict:
    req = parse(payload)
    provider = liteapi.PROVIDERS[0]

    async def find(p):
        return await p.hotels(country_code=req["country_code"], city_name=req["city"], hotel_name=req["hotel_name"],
                              ai_search=req["place"], latitude=req["latitude"], longitude=req["longitude"],
                              distance_m=req["radius_m"], limit=req["max_hotels"])
    found = await ctx.run("hotels", [provider], find, per_attempt_seconds=30, max_attempts=2)
    candidates = found["hotels"][:req["max_hotels"]]
    notes = []
    if not candidates:
        return _empty(req, found, notes + ["No hotel matched the place given."], live_mode=not liteapi.sandbox_key())
    occupancy = {"adults": req["adults"]}
    if req["children_ages"]:
        occupancy["children"] = req["children_ages"]

    async def price(p):
        return await p.rates([h["id"] for h in candidates if h.get("id")], req["check_in"], req["check_out"],
                             [occupancy], currency=req["currency"], guest_nationality=req["guest_nationality"])
    priced = await ctx.run("rates", [provider], price, per_attempt_seconds=45, max_attempts=1)
    by_id = {}
    for row in priced["rates"]:
        rooms = []
        for rt in row.get("roomTypes") or []:
            for rate in rt.get("rates") or []:
                if isinstance(rate, dict):
                    rooms.append(_room(rt, rate))
        by_id[row.get("hotelId")] = rooms
    hotels_out = [_hotel(h, by_id.get(h.get("id"), [])) for h in candidates]
    without = [h["name"] for h in hotels_out if h["cheapest"] is None]
    hotels_out = [h for h in hotels_out if h["cheapest"] is not None]
    if req["sort"] == "rating":
        hotels_out.sort(key=lambda h: (h["rating"] is None, -(h["rating"] or 0), h["cheapest"]["total"]))
    else:
        hotels_out.sort(key=lambda h: h["cheapest"]["total"])
    if without:
        notes.append(f"{len(without)} matched hotel(s) had no rate for these dates and guests: " + "; ".join(without[:5]) + ("..." if len(without) > 5 else ""))
    live_mode = not priced["sandbox"]
    if not live_mode:
        notes.append("Sandbox rates from the provider's test environment: not bookable, prices are not real.")
    cheapest = min(hotels_out, key=lambda h: h["cheapest"]["total"]) if hotels_out else None
    return {
        "query": {"place": req["place"], "city": req["city"], "country_code": req["country_code"],
                  "hotel_name": req["hotel_name"], "latitude": req["latitude"], "longitude": req["longitude"]},
        "check_in": req["check_in"], "check_out": req["check_out"], "nights": req["nights"],
        "guests": {"adults": req["adults"], "children_ages": req["children_ages"]},
        "currency": req["currency"], "guest_nationality": req["guest_nationality"], "sort": req["sort"],
        "hotels": hotels_out,
        "hotel_count": len(hotels_out),
        "hotels_matched": found.get("total") if isinstance(found.get("total"), int) else len(candidates),
        "hotels_without_rates": len(without),
        "cheapest_total": ({"amount": cheapest["cheapest"]["total"], "currency": cheapest["cheapest"]["currency"],
                            "hotel": cheapest["name"]} if cheapest else None),
        "live_mode": live_mode,
        "source": "liteapi",
        "notes": notes,
        "checked_at": _now(),
    }


def _empty(req, found, notes, live_mode):
    return {
        "query": {"place": req["place"], "city": req["city"], "country_code": req["country_code"],
                  "hotel_name": req["hotel_name"], "latitude": req["latitude"], "longitude": req["longitude"]},
        "check_in": req["check_in"], "check_out": req["check_out"], "nights": req["nights"],
        "guests": {"adults": req["adults"], "children_ages": req["children_ages"]},
        "currency": req["currency"], "guest_nationality": req["guest_nationality"], "sort": req["sort"],
        "hotels": [], "hotel_count": 0,
        "hotels_matched": found.get("total") if isinstance(found.get("total"), int) else 0,
        "hotels_without_rates": 0, "cheapest_total": None, "live_mode": live_mode, "source": "liteapi",
        "notes": notes, "checked_at": _now(),
    }


SKILLS = {"travel.hotels": hotels}
PRECHECKS = {"travel.hotels": precheck}
