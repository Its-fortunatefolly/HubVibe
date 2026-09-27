"""Regional open-data portals, keyless: one CKAN adapter, many portals.

Most government data portals in the Asia-Pacific run CKAN, whose
`package_search` action is the same call everywhere: a query in the
portal's own language, a count, and packages with their downloadable
resources. One adapter, one portal table, and the worker fans out. A
portal that does not answer is reported as unreachable for that call --
never silently dropped, never cached.

Portals here were each verified keyless on 2026-09-26 (package_search 200
with a native-language query). Adding one is adding a row.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_OPENDATA_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_OPENDATA_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
MAX_ROWS = 50

# id -> (region, country code, portal name, CKAN API base, languages the portal is searched in)
PORTALS = {
    "data.gov.au": {"region": "au", "country": "AU", "name": "data.gov.au (Australian Government)",
                    "base": "https://data.gov.au/data/api/3/action", "languages": ["en"]},
    "data.e-gov.go.jp": {"region": "jp", "country": "JP", "name": "e-Gov Data Portal (Government of Japan)",
                         "base": "https://data.e-gov.go.jp/data/api/3/action", "languages": ["ja"]},
    "catalog.data.metro.tokyo.lg.jp": {"region": "jp", "country": "JP", "name": "Tokyo Open Data Catalog",
                                       "base": "https://catalog.data.metro.tokyo.lg.jp/api/3/action",
                                       "languages": ["ja"]},
    "data.gov.hk": {"region": "hk", "country": "HK", "name": "DATA.GOV.HK (Hong Kong)",
                    "base": "https://data.gov.hk/en-data/api/3/action", "languages": ["en", "zh"]},
}
REGIONS = sorted({p["region"] for p in PORTALS.values()})


def portals_for(region: Optional[str]) -> list:
    """Portal ids for a region code, or all of them."""
    if not region or region == "all":
        return list(PORTALS)
    return [pid for pid, p in PORTALS.items() if p["region"] == region]


def _str(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _package(pkg: dict, portal_id: str) -> dict:
    portal = PORTALS[portal_id]
    org = pkg.get("organization")
    resources = []
    for r in pkg.get("resources") or []:
        if not isinstance(r, dict) or not _str(r.get("url")):
            continue
        resources.append({"url": r["url"].strip(), "format": _str(r.get("format")), "name": _str(r.get("name")),
                          "last_modified": _str(r.get("last_modified")),
                          "size_bytes": r.get("size") if isinstance(r.get("size"), int) and not isinstance(r.get("size"), bool) else None})
    notes = _str(pkg.get("notes"))
    return {
        "portal": portal_id,
        "region": portal["region"],
        "country": portal["country"],
        "id": _str(pkg.get("id")) or _str(pkg.get("name")) or "",
        "name": _str(pkg.get("name")),
        "title": _str(pkg.get("title")) or _str(pkg.get("name")) or "",
        "description": (notes[:1000] if notes else None),
        "organization": (_str(org.get("title")) or _str(org.get("name"))) if isinstance(org, dict) else _str(org),
        "license": _str(pkg.get("license_title")) or _str(pkg.get("license_id")),
        "updated_at": _str(pkg.get("metadata_modified")),
        "tags": [t["name"] for t in (pkg.get("tags") or []) if isinstance(t, dict) and _str(t.get("name"))][:20],
        "landing_url": _str(pkg.get("url")),
        "resources": resources[:25],
        "resource_count": len(pkg.get("resources") or []),
    }


class _Ckan:
    """One portal, one adapter instance; `id` is the portal id so the
    ledger and the breaker keep per-portal health."""

    def __init__(self, portal_id: str):
        self.id = f"ckan:{portal_id}"
        self.portal_id = portal_id
        self.portal = PORTALS[portal_id]

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def search(self, query: str, rows: int = 10, start: int = 0) -> runtime.ProviderResult:
        url = f"{self.portal['base']}/package_search"
        params = {"q": query, "rows": str(min(max(rows, 1), MAX_ROWS)), "start": str(max(start, 0))}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                         "Accept": "application/json"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{self.portal_id} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{self.portal_id} unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"{self.portal_id} returned {response.status_code}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(f"{self.portal_id} returned {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise runtime.InvalidProviderResponse(f"{self.portal_id} did not return JSON.") from None
        if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("result"), dict):
            raise runtime.InvalidProviderResponse(f"{self.portal_id} answered without a CKAN result.")
        result = data["result"]
        packages = [_package(p, self.portal_id) for p in result.get("results") or [] if isinstance(p, dict)]
        return runtime.ProviderResult(
            value={"portal": self.portal_id, "count": int(result.get("count") or 0), "results": packages},
            cost_micros=0, cost_measured=True,
            usage=f"portal={self.portal_id} count={result.get('count')} rows={len(packages)}")


ADAPTERS = {pid: _Ckan(pid) for pid in PORTALS}
PROVIDERS = list(ADAPTERS.values())
