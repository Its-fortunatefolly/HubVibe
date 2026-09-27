"""data.macro -- one official economic series for one country, current.

GDP, inflation, unemployment, population, reserves, debt, trade, rates:
the World Bank's annual series for every economy, and Eurostat's monthly
series where the EU publishes them (inflation, unemployment). One call,
one series, the source's own update stamp, and the latest-versus-previous
change computed here so the agent does not have to.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import macro

MAX_LAST = macro.MAX_OBSERVATIONS
FREQUENCIES = ("auto", "annual", "monthly")
_COUNTRY = re.compile(r"^[A-Za-z0-9_]{2,9}$")
# ISO codes the two sources spell differently.
_EUROSTAT_GEO = {"GR": "EL", "GB": "UK", "EU": "EU27_2020", "EZ": "EA20"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    raw = payload.get("indicator")
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest(f"`indicator` is required: one of {', '.join(sorted(macro.ALIASES))}, or a World Bank code.")
    alias, code = macro.resolve_indicator(raw)
    country = payload.get("country")
    if not isinstance(country, str) or not _COUNTRY.match(country.strip()):
        raise runtime.InvalidRequest("`country` is required: an ISO 3166 code (JP, KOR, DE) or an EU aggregate (EA20, EU27_2020).")
    country = country.strip().upper()
    last = payload.get("last", 10)
    if isinstance(last, bool) or not isinstance(last, int) or not 1 <= last <= MAX_LAST:
        raise runtime.InvalidRequest(f"`last` must be a whole number from 1 to {MAX_LAST}.")
    frequency = payload.get("frequency", "auto")
    if frequency not in FREQUENCIES:
        raise runtime.InvalidRequest(f"`frequency` must be one of {list(FREQUENCIES)}.")
    eurostat_geo = _EUROSTAT_GEO.get(country, country)
    monthly_possible = alias in macro.EUROSTAT_MONTHLY and eurostat_geo in macro.EU_GEOS
    if frequency == "monthly" and not monthly_possible:
        raise runtime.InvalidRequest(
            "Monthly data is published here only for `inflation` and `unemployment` in EU/EEA countries and "
            "aggregates (EA20, EU27_2020); ask for `annual` or omit `frequency`.")
    use_monthly = monthly_possible and frequency in ("auto", "monthly")
    if not use_monthly and eurostat_geo != country and country not in ("GR", "GB"):
        raise runtime.InvalidRequest(f"`country` {country} is an EU aggregate; the World Bank has no annual series for it.")
    if not use_monthly and country.startswith(("EA", "EU27")):
        raise runtime.InvalidRequest(f"`country` {country} is an EU aggregate, only available monthly for inflation and unemployment.")
    return {"alias": alias, "code": code, "country": country, "eurostat_geo": eurostat_geo, "last": last,
            "frequency": "monthly" if use_monthly else "annual"}


def precheck(payload: dict) -> None:
    parse(payload)


def _numeric(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def summarize(observations: list) -> dict:
    """latest, previous and the change between them, from the ordered series."""
    valued = [o for o in observations if _numeric(o.get("value")) is not None]
    latest = valued[-1] if valued else None
    previous = valued[-2] if len(valued) > 1 else None
    change = None
    if latest and previous:
        a, b = float(latest["value"]), float(previous["value"])
        change = {"absolute": round(a - b, 6), "pct": (round((a - b) / abs(b) * 100, 4) if b else None)}
    return {"latest": latest, "previous": previous, "change": change,
            "missing_values": len(observations) - len(valued)}


async def series(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes = []
    if req["frequency"] == "monthly":
        async def call(provider):
            return await provider.monthly(req["eurostat_geo"], req["alias"], req["last"])
        value = await ctx.run("eurostat", [macro.EUROSTAT], call, per_attempt_seconds=25, max_attempts=2)
        source = "eurostat"
        unit = value.get("unit")
    else:
        async def call(provider):
            return await provider.series(req["country"], req["code"], req["last"])
        value = await ctx.run("world-bank", [macro.WORLD_BANK], call, per_attempt_seconds=25, max_attempts=2)
        source = "world-bank"
        unit = None
        if req["alias"] in macro.EUROSTAT_MONTHLY and req["eurostat_geo"] in macro.EU_GEOS and payload.get("frequency") == "annual":
            notes.append("Monthly figures exist for this indicator and country: ask with frequency \"monthly\".")
    observations = value["observations"]
    stats = summarize(observations)
    if stats["missing_values"]:
        notes.append(f"{stats['missing_values']} of {len(observations)} periods have no published value yet.")
    if stats["latest"] is None:
        notes.append("The source has no published values for this series in the requested window.")
    return {
        "indicator": {"alias": req["alias"], "code": req["code"] if source == "world-bank" else None,
                      "name": value.get("indicator_name"), "unit": unit},
        "country": {"code": value.get("country_code") or req["country"], "name": value.get("country_name")},
        "frequency": req["frequency"],
        "source": source,
        "observations": observations,
        "observation_count": len(observations),
        "latest": stats["latest"],
        "previous": stats["previous"],
        "change": stats["change"],
        "as_of": value.get("as_of"),
        "source_url": value.get("source_url"),
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"data.macro": series}
PRECHECKS = {"data.macro": precheck}
