"""opendata.search -- find structured datasets across Asia-Pacific open-data
portals in one call, in any language, and get the download URLs.

Agent-accessible structured data outside the US is scarce; the portals
that hold it are keyless but scattered and each speaks its own language.
This worker fans one query out to every portal in the region asked for,
concurrently, and returns the datasets with their resource URLs so the
buyer can chain fetch.raw on the CSV or JSON it wants.

Partial results are disclosed, not hidden: a portal that failed for this
call is listed under `portals_failed` with the reason, and `portals_ok`
names the ones that answered. Nothing is cached; `checked_at` says when.
"""

import asyncio
from datetime import datetime, timezone

from .. import runtime
from ..providers import opendata

MAX_QUERY_CHARS = 300
MAX_LIMIT = 50


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise runtime.InvalidRequest("`query` is required: words to search for, in any language.")
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        raise runtime.InvalidRequest(f"`query` is {len(query)} characters, over the {MAX_QUERY_CHARS} limit.")
    region = payload.get("region", "all")
    if not isinstance(region, str) or region not in opendata.REGIONS + ["all"]:
        raise runtime.InvalidRequest(f"`region` must be one of {opendata.REGIONS + ['all']}.")
    limit = payload.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise runtime.InvalidRequest(f"`limit` must be a whole number from 1 to {MAX_LIMIT} (per portal).")
    portals = payload.get("portals")
    if portals is not None:
        if (not isinstance(portals, list) or not portals
                or not all(isinstance(p, str) and p in opendata.PORTALS for p in portals)):
            raise runtime.InvalidRequest(f"`portals`, when given, must be drawn from {sorted(opendata.PORTALS)}.")
        portal_ids = list(dict.fromkeys(portals))
    else:
        portal_ids = opendata.portals_for(region)
    return {"query": query, "region": region, "limit": limit, "portals": portal_ids}


def precheck(payload: dict) -> None:
    parse(payload)


async def search(ctx, payload: dict) -> dict:
    req = parse(payload)

    async def one(portal_id: str):
        adapter = opendata.ADAPTERS[portal_id]

        async def call(provider):
            return await provider.search(req["query"], rows=req["limit"])

        try:
            value = await ctx.run(f"search:{portal_id}", [adapter], call, per_attempt_seconds=20,
                                  budget_seconds=40)
            return portal_id, value, None
        except runtime.WorkerError as exc:
            return portal_id, None, exc.detail

    outcomes = await asyncio.gather(*(one(pid) for pid in req["portals"]))
    results, ok, failed, totals = [], [], [], {}
    for portal_id, value, error in outcomes:
        if value is None:
            failed.append({"portal": portal_id, "reason": error or "no answer"})
            continue
        ok.append(portal_id)
        totals[portal_id] = value["count"]
        results.extend(value["results"])
    if not ok:
        raise runtime.TransientProviderError(
            "No portal answered: " + "; ".join(f"{f['portal']}: {f['reason']}" for f in failed),
            reason="no_sources")
    results.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return {
        "query": req["query"],
        "region": req["region"],
        "portals_searched": req["portals"],
        "portals_ok": ok,
        "portals_failed": failed,
        "total_matches": {pid: totals[pid] for pid in ok},
        "results": results,
        "result_count": len(results),
        "limit_per_portal": req["limit"],
        "checked_at": _now(),
    }


SKILLS = {"opendata.search": search}
PRECHECKS = {"opendata.search": precheck}
