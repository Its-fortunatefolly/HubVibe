"""Points of interest from Overture Maps, read through Google BigQuery.

Overture's places theme is open data: Meta, Microsoft and most partners
under CDLA Permissive 2.0, Foursquare under Apache 2.0, AllThePlaces under
CC0 (docs.overturemaps.org/attribution). Each place keeps the datasets it
came from, and every answer carries the Overture credit. The table is
`bigquery-public-data.overture_maps.place` (monthly releases, clustered by
geometry), where a nearby search bills about 10 MB.
"""

import re
from typing import Optional

from .. import runtime
from . import bigquery

TABLE = "bigquery-public-data.overture_maps.place"
CREDIT = {"text": "Places data from Overture Maps Foundation (Meta, Microsoft and partners: CDLA Permissive 2.0; "
                  "Foursquare: Apache 2.0; AllThePlaces: CC0)",
          "url": "https://docs.overturemaps.org/attribution/"}
STOPWORDS = {"a", "an", "the", "best", "good", "great", "top", "nearby", "near", "around", "open", "now", "cheap",
             "some", "any", "places", "place", "spot", "spots", "shop", "shops", "store", "stores", "me", "to", "for",
             "with", "and", "of"}
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def keywords(what: str) -> list:
    words = []
    for w in _WORD.findall((what or "").lower()):
        if w in STOPWORDS or len(w) < 2:
            continue
        if w.isascii() and len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # cafes -> cafe, pharmacies stays close enough via LIKE
        words.append(w)
    return words[:5]


def _lit(text: str) -> str:
    return "'%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "\\'") + "%'"


def build_sql(lat: float, lon: float, radius_m: int, words: list, limit: int) -> str:
    point = f"ST_GEOGPOINT({float(lon)!r}, {float(lat)!r})"
    match = " AND ".join(
        f"(LOWER(IFNULL(basic_category, '')) LIKE {_lit(w)} OR LOWER(IFNULL(taxonomy.primary, '')) LIKE {_lit(w)} "
        f"OR LOWER(IFNULL(names.primary, '')) LIKE {_lit(w)})" for w in words) or "TRUE"
    # A place whose category is the thing asked for ranks above one that only has the word in its name.
    by_category = " AND ".join(
        f"(LOWER(IFNULL(basic_category, '')) LIKE {_lit(w)} OR LOWER(IFNULL(taxonomy.primary, '')) LIKE {_lit(w)})"
        for w in words) or "TRUE"
    return (
        "SELECT id, names.primary AS name, basic_category, taxonomy.primary AS category, operating_status, confidence, "
        f"ROUND(ST_DISTANCE(geometry, {point})) AS meters, ST_Y(geometry) AS lat, ST_X(geometry) AS lon, "
        "(SELECT a.element.freeform FROM UNNEST(addresses.list) a LIMIT 1) AS street, "
        "(SELECT a.element.locality FROM UNNEST(addresses.list) a LIMIT 1) AS city, "
        "(SELECT a.element.region FROM UNNEST(addresses.list) a LIMIT 1) AS region, "
        "(SELECT a.element.postcode FROM UNNEST(addresses.list) a LIMIT 1) AS postcode, "
        "(SELECT a.element.country FROM UNNEST(addresses.list) a LIMIT 1) AS country, "
        "(SELECT w.element FROM UNNEST(websites.list) w LIMIT 1) AS website, "
        "(SELECT p.element FROM UNNEST(phones.list) p LIMIT 1) AS phone, brand.names.primary AS brand, "
        "ARRAY_TO_STRING(ARRAY(SELECT DISTINCT s.element.dataset FROM UNNEST(sources.list) s), ',') AS datasets "
        f"FROM `{TABLE}` WHERE ST_DWITHIN(geometry, {point}, {int(radius_m)}) "
        "AND IFNULL(operating_status, 'open') != 'permanently_closed' "
        f"AND {match} ORDER BY IF({by_category}, 0, 1), meters LIMIT {int(limit)}")


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def place(row: dict) -> dict:
    street, city, region, postcode = row.get("street"), row.get("city"), row.get("region"), row.get("postcode")
    return {"id": row.get("id"), "name": row.get("name"), "category": row.get("category") or row.get("basic_category"),
            "distance_m": int(_num(row.get("meters")) or 0), "lat": _num(row.get("lat")), "lon": _num(row.get("lon")),
            "address": ", ".join(x for x in (street, city, region, postcode) if x) or None,
            "country": row.get("country"), "website": row.get("website"), "phone": row.get("phone"),
            "brand": row.get("brand"), "operating_status": row.get("operating_status"),
            "confidence": round(_num(row.get("confidence")) or 0, 3),
            "sources": [d for d in (row.get("datasets") or "").split(",") if d and not d.startswith("Overture")]}


class _Overture:
    id = "overture-bigquery"

    def available(self) -> bool:
        return bigquery.PROVIDERS[0].available()

    def unavailable_reason(self) -> str:
        return bigquery.PROVIDERS[0].unavailable_reason()

    async def nearby(self, lat: float, lon: float, radius_m: int, words: list, limit: int) -> runtime.ProviderResult:
        result = await bigquery.PROVIDERS[0].query_clustered(build_sql(lat, lon, radius_m, words, limit))
        return runtime.ProviderResult(value=[place(r) for r in result.value["rows"] if isinstance(r, dict)],
                                      cost_micros=result.cost_micros, cost_measured=result.cost_measured,
                                      usage=result.usage)


OVERTURE = _Overture()
PROVIDERS = [OVERTURE]
