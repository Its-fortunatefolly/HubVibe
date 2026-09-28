"""sanctions.screen -- is this name on a government sanctions list?

Screens one person, company, vessel or aircraft name against the US (OFAC
SDN and consolidated), UK and EU sanctions lists, read from the governments'
own files (see providers/sanctions.py for sources and licences). Matching is
fuzzy and word-order free: aliases, transliterations and one-letter typos
still match; a different surname does not. Every match names its list, the
list's date, its programmes and what the list itself records (countries,
dates of birth, UN reference), so the caller can confirm or clear it.

A potential match is a lead to review, never a legal determination.
"""

import re
from datetime import datetime, timezone

from .. import runtime
from ..providers import sanctions

TYPES = ("any", "person", "entity", "vessel", "aircraft")
MAX_NAME = 200


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    name = payload.get("name")
    if not isinstance(name, str) or len(sanctions.normalize(name)) < 2:
        raise runtime.InvalidRequest("`name` is required: the person, company, vessel or aircraft to screen.")
    if len(name) > MAX_NAME:
        raise runtime.InvalidRequest(f"`name` is longer than {MAX_NAME} characters.")
    kind = (payload.get("type") or "any")
    if kind not in TYPES:
        raise runtime.InvalidRequest(f"`type` must be one of {', '.join(TYPES)}.")
    try:
        threshold = float(payload.get("threshold", 0.85))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`threshold` must be a number between 0.6 and 1.")
    if not 0.6 <= threshold <= 1.0:
        raise runtime.InvalidRequest("`threshold` must be between 0.6 and 1.")
    try:
        limit = int(payload.get("limit", 10))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`limit` must be a whole number from 1 to 50.")
    if not 1 <= limit <= 50:
        raise runtime.InvalidRequest("`limit` must be between 1 and 50.")
    lists = payload.get("lists")
    if lists is not None:
        if not isinstance(lists, list) or not lists or any(x not in sanctions.LISTS for x in lists):
            raise runtime.InvalidRequest(f"`lists`, when given, is a non-empty subset of {', '.join(sanctions.LISTS)}.")
    country = payload.get("country")
    if country is not None and (not isinstance(country, str) or not country.strip() or len(country) > 60):
        raise runtime.InvalidRequest("`country`, when given, is a country name such as Iran or Russia.")
    birth_year = payload.get("birth_year")
    if birth_year is not None:
        if isinstance(birth_year, bool) or not isinstance(birth_year, int) or not 1880 <= birth_year <= 2030:
            raise runtime.InvalidRequest("`birth_year`, when given, is a four-digit year.")
    return {"name": name.strip(), "type": kind, "threshold": threshold, "limit": limit,
            "lists": lists, "country": country.strip() if country else None, "birth_year": birth_year}


def precheck(payload: dict) -> None:
    parse(payload)


def _years(dates: list) -> set:
    return {int(y) for d in dates for y in re.findall(r"(?<!\d)(1[89]\d\d|20[0-3]\d)(?!\d)", d or "")}


async def screen(ctx, payload: dict) -> dict:
    req = parse(payload)
    fetch_limit = 50 if (req["country"] or req["birth_year"]) else req["limit"]

    async def call(provider):
        return await provider.screen(req["name"], kind=req["type"], threshold=req["threshold"],
                                     limit=fetch_limit, lists=req["lists"])

    value = await ctx.run("screen", sanctions.PROVIDERS, call, per_attempt_seconds=150, max_attempts=2)
    notes, matches = [], []
    for m in value["matches"]:
        e = m["entry"]
        if req["country"] and e["countries"] and not any(
                req["country"].casefold() in c.casefold() for c in e["countries"]):
            continue
        if req["birth_year"] and e["type"] == "person":
            years = _years(e["birth_dates"])
            if years and not any(abs(y - req["birth_year"]) <= 1 for y in years):
                continue
        meta = sanctions.LISTS[e["list"]]
        matches.append({
            "list": e["list"], "list_name": meta["name"], "id": e["id"], "name": e["primary_name"],
            "matched_name": m["matched_name"], "score": m["score"], "type": e["type"],
            "programs": e["programs"], "countries": e["countries"], "birth_dates": e["birth_dates"],
            "un_reference": e["un_reference"], "listed_on": e["listed_on"], "source_url": meta["page"],
        })
        if len(matches) >= req["limit"]:
            break
    if req["country"]:
        notes.append("Entries whose list records no country are kept; only a recorded, different country excludes a match.")
    if req["birth_year"]:
        notes.append("People whose list records no birth year are kept; a recorded year more than one year away excludes a match.")
    missing = [x["name"] for x in value["lists"] if not x["loaded"]]
    if missing:
        notes.append("Not screened (the file could not be downloaded): " + "; ".join(missing) + ".")
    if matches:
        notes.append("A potential match is a lead to review, not a determination: compare dates of birth, "
                     "nationality and identifiers on the list's own record before acting.")
    return {
        "query": req,
        # A clean answer only counts when every list asked for was actually screened.
        "verdict": "potential_match" if matches else ("incomplete" if missing else "no_match"),
        "match_count": len(matches),
        "matches": matches,
        "lists": value["lists"],
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"sanctions.screen": screen}
PRECHECKS = {"sanctions.screen": precheck}
