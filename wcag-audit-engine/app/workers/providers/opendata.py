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

# id -> region, country code, portal name, API base, languages the portal is searched in,
# and `kind`: "ckan" (package_search), "dcat-europa" (data.europa.eu hub search) or
# "datagokr" (Korea's portal: public HTML search + dataset pages, no key).
# Every row verified keyless on the date noted; adding a portal is adding a row.
PORTALS = {
    "data.gov.au": {"region": "au", "country": "AU", "name": "data.gov.au (Australian Government)",
                    "base": "https://data.gov.au/data/api/3/action", "languages": ["en"], "kind": "ckan"},
    "data.e-gov.go.jp": {"region": "jp", "country": "JP", "name": "e-Gov Data Portal (Government of Japan)",
                         "base": "https://data.e-gov.go.jp/data/api/3/action", "languages": ["ja"], "kind": "ckan"},
    "catalog.data.metro.tokyo.lg.jp": {"region": "jp", "country": "JP", "name": "Tokyo Open Data Catalog",
                                       "base": "https://catalog.data.metro.tokyo.lg.jp/api/3/action",
                                       "languages": ["ja"], "kind": "ckan"},
    "data.bodik.jp": {"region": "jp", "country": "JP", "name": "BODIK Open Data (Japanese municipalities)",
                      "base": "https://data.bodik.jp/api/3/action", "languages": ["ja"], "kind": "ckan"},  # 2026-09-27
    "data.go.kr": {"region": "kr", "country": "KR", "name": "공공데이터포털 data.go.kr (Republic of Korea)",
                   "base": "https://www.data.go.kr", "languages": ["ko"], "kind": "datagokr"},  # 2026-09-27
    "data.gov.hk": {"region": "hk", "country": "HK", "name": "DATA.GOV.HK (Hong Kong)",
                    "base": "https://data.gov.hk/en-data/api/3/action", "languages": ["en", "zh"], "kind": "ckan"},
    "ckan.publishing.service.gov.uk": {"region": "gb", "country": "GB", "name": "data.gov.uk (United Kingdom)",
                                       "base": "https://ckan.publishing.service.gov.uk/api/3/action",
                                       "languages": ["en"], "kind": "ckan"},  # 2026-09-27
    "open.canada.ca": {"region": "ca", "country": "CA", "name": "Open Government Canada",
                       "base": "https://open.canada.ca/data/api/3/action", "languages": ["en", "fr"], "kind": "ckan"},  # 2026-09-27
    "data.europa.eu": {"region": "eu", "country": "EU", "name": "data.europa.eu (European Union, 139,000+ datasets)",
                       "base": "https://data.europa.eu/api/hub/search", "languages": ["en", "de", "fr", "es", "it"],
                       "kind": "dcat-europa"},  # 2026-09-27
    "govdata.de": {"region": "de", "country": "DE", "name": "GovData (Germany)",
                   "base": "https://www.govdata.de/ckan/api/3/action", "languages": ["de"], "kind": "ckan"},  # 2026-09-27
    "dati.gov.it": {"region": "it", "country": "IT", "name": "dati.gov.it (Italy)",
                    "base": "https://dati.gov.it/opendata/api/3/action", "languages": ["it"], "kind": "ckan"},  # 2026-09-27
    "data.gov.ie": {"region": "ie", "country": "IE", "name": "data.gov.ie (Ireland)",
                    "base": "https://data.gov.ie/api/3/action", "languages": ["en"], "kind": "ckan"},  # 2026-09-27
    "opendata.swiss": {"region": "ch", "country": "CH", "name": "opendata.swiss (Switzerland)",
                       "base": "https://ckan.opendata.swiss/api/3/action", "languages": ["de", "fr", "it"], "kind": "ckan"},  # 2026-09-27
    "data.overheid.nl": {"region": "nl", "country": "NL", "name": "data.overheid.nl (Netherlands)",
                         "base": "https://data.overheid.nl/data/api/3/action", "languages": ["nl"], "kind": "ckan"},  # 2026-09-27
    "data.gov.il": {"region": "il", "country": "IL", "name": "data.gov.il (Israel)",
                    "base": "https://data.gov.il/api/3/action", "languages": ["he", "en"], "kind": "ckan"},  # 2026-09-27
    "datos.gob.cl": {"region": "cl", "country": "CL", "name": "datos.gob.cl (Chile)",
                     "base": "https://datos.gob.cl/api/3/action", "languages": ["es"], "kind": "ckan"},  # 2026-09-27
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




def _lang_pick(value, languages: list) -> Optional[str]:
    """data.europa.eu writes titles and descriptions per language; pick the
    portal's search language, then English, then whatever exists."""
    if isinstance(value, str):
        return _str(value)
    if not isinstance(value, dict) or not value:
        return None
    for lang in list(languages) + ["en"]:
        if _str(value.get(lang)):
            return _str(value[lang])
    for v in value.values():
        if _str(v):
            return _str(v)
    return None


def _europa_package(pkg: dict, portal_id: str, languages: list) -> dict:
    portal = PORTALS[portal_id]
    resources = []
    for d in pkg.get("distributions") or []:
        if not isinstance(d, dict):
            continue
        url = d.get("download_url") or d.get("access_url")
        if isinstance(url, list):
            url = url[0] if url else None
        if not _str(url):
            continue
        fmt = d.get("format")
        resources.append({"url": url.strip(), "format": _str(fmt.get("label") or fmt.get("id")) if isinstance(fmt, dict) else _str(fmt),
                          "name": _lang_pick(d.get("title"), languages), "last_modified": _str(d.get("modified")),
                          "size_bytes": d.get("byte_size") if isinstance(d.get("byte_size"), int) and not isinstance(d.get("byte_size"), bool) else None})
    publisher = pkg.get("publisher")
    country = pkg.get("country") or {}
    description = _lang_pick(pkg.get("description"), languages)
    keywords = pkg.get("keywords") or []
    tags = []
    for k in keywords:
        label = _lang_pick(k.get("label") if isinstance(k, dict) else k, languages) if k else None
        if label:
            tags.append(label)
    return {
        "portal": portal_id,
        "region": portal["region"],
        "country": _str(country.get("id")).upper() if isinstance(country, dict) and _str(country.get("id")) else portal["country"],
        "id": _str(pkg.get("id")) or "",
        "name": _str(pkg.get("id")),
        "title": _lang_pick(pkg.get("title"), languages) or _str(pkg.get("id")) or "",
        "description": (description[:1000] if description else None),
        "organization": _str(publisher.get("name")) if isinstance(publisher, dict) else _str(publisher),
        "license": None,
        "updated_at": _str(pkg.get("modified")),
        "tags": tags[:20],
        "landing_url": _str(pkg.get("resource")),
        "resources": resources[:25],
        "resource_count": len(pkg.get("distributions") or []),
    }


class _Europa:
    """data.europa.eu's hub search: DCAT records over every EU member catalog."""

    def __init__(self, portal_id: str):
        self.id = f"dcat:{portal_id}"
        self.portal_id = portal_id
        self.portal = PORTALS[portal_id]

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def search(self, query: str, rows: int = 10, start: int = 0) -> runtime.ProviderResult:
        url = f"{self.portal['base']}/search"
        params = {"q": query, "limit": str(min(max(rows, 1), MAX_ROWS)), "page": str(max(start, 0) // max(rows, 1)),
                  "filter": "dataset"}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
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
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise runtime.InvalidProviderResponse(f"{self.portal_id} answered without a result.")
        packages = [_europa_package(p, self.portal_id, self.portal["languages"])
                    for p in result.get("results") or [] if isinstance(p, dict)]
        return runtime.ProviderResult(
            value={"portal": self.portal_id, "count": int(result.get("count") or 0), "results": packages},
            cost_micros=0, cost_measured=True,
            usage=f"portal={self.portal_id} count={result.get('count')} rows={len(packages)}")


# --- Korea: data.go.kr, keyless through its public pages -------------------
import html as _html
import re as _re

_KR_ITEM = _re.compile(r'<div class="apply-result-item">(.*?)<div class="apply-result-btn-group">', _re.S)
_KR_ID = _re.compile(r'/data/(\d+)/fileData\.do')
_KR_TITLE = _re.compile(r'<a href="/data/\d+/fileData\.do">(.*?)</a>', _re.S)
_KR_SUMMARY = _re.compile(r'<span class="apply-result-summary">(.*?)</span>', _re.S)
_KR_FIELD = _re.compile(r'<strong>([^<]+)</strong>(.*?)</li>', _re.S)
_KR_EXT = _re.compile(r'data-ext="([A-Za-z0-9+ ]+)"')
_KR_BADGE = _re.compile(r'<span class="krds-badge[^"]*">\s*([^<]+?)\s*</span>')
_KR_COUNT = _re.compile(r'파일데이터\s*<span>\s*\(([\d,]+)건\)', _re.S)
_KR_DOWNLOAD = _re.compile(r'/cmm/cmm/fileDownload\.do\?atchFileId=([A-Z0-9_]+)&(?:amp;)?fileDetailSn=(\d+)')
_KR_TAG = _re.compile(r'<[^>]+>')
KR_SEARCH_PATH = "/tcs/dss/selectDataSetList.do"
KR_DATASET_PATH = _re.compile(r"^/data/(\d+)/fileData\.do$")


def _kr_text(fragment: str) -> str:
    """Visible text of a fragment: inline markup (the portal wraps query hits in
    <em>) is dropped without inserting spaces; whitespace is collapsed."""
    return " ".join(_html.unescape(_KR_TAG.sub("", fragment or "")).split())


def parse_datagokr_search(page: str) -> dict:
    """Dataset rows from data.go.kr's search page (file datasets)."""
    m = _KR_COUNT.search(page)
    count = int(m.group(1).replace(",", "")) if m else 0
    results = []
    for block in _KR_ITEM.findall(page):
        idm = _KR_ID.search(block)
        if not idm:
            continue
        dataset_id = idm.group(1)
        tm = _KR_TITLE.search(block)
        title = _kr_text(tm.group(1)) if tm else dataset_id
        sm = _KR_SUMMARY.search(block)
        fields = {_kr_text(k): _kr_text(v) for k, v in _KR_FIELD.findall(block)}
        formats = [f.strip() for f in _KR_EXT.findall(block)]
        badges = [b.strip() for b in _KR_BADGE.findall(block)]
        formats += [b for b in badges if "JSON" in b or "XML" in b]
        keywords = [k.strip() for k in (fields.get("키워드") or "").split(",") if k.strip()]
        results.append({
            "id": dataset_id, "title": title,
            "description": (_kr_text(sm.group(1))[:1000] if sm else None),
            "organization": fields.get("제공기관") or None,
            "updated_at": fields.get("수정일") or None,
            "categories": [b for b in badges if b not in formats and "JSON" not in b],
            "formats": list(dict.fromkeys(formats)),
            "tags": keywords[:20],
            "views": fields.get("조회수"), "downloads": fields.get("다운로드"),
        })
    return {"count": count, "results": results}


def parse_datagokr_download(page: str) -> Optional[str]:
    """The keyless file link a data.go.kr dataset page carries, absolute."""
    m = _KR_DOWNLOAD.search(page)
    if not m:
        return None
    return f"https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId={m.group(1)}&fileDetailSn={m.group(2)}&insertDataPrcus=N"


def is_datagokr_dataset_url(url: str) -> Optional[str]:
    """The dataset id when `url` is a data.go.kr dataset page, else None."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("www.data.go.kr", "data.go.kr"):
        return None
    m = KR_DATASET_PATH.match(parsed.path)
    return m.group(1) if m else None


class _DataGoKr:
    """Korea's portal: the search page and dataset pages are public HTML and
    the files download without a key; only its separate JSON APIs need one."""

    def __init__(self, portal_id: str = "data.go.kr"):
        self.id = f"datagokr:{portal_id}"
        self.portal_id = portal_id
        self.portal = PORTALS[portal_id]

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _get(self, path: str, params: Optional[dict], what: str) -> str:
        url = f"{self.portal['base']}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers={"User-Agent": "Mozilla/5.0 " + USER_AGENT,
                                                                         "Accept": "text/html"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{self.portal_id} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{self.portal_id} unreachable: {exc}") from exc
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"{self.portal_id} returned {response.status_code} for {what}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(f"{self.portal_id} returned {response.status_code} for {what}")
        return response.text

    async def search(self, query: str, rows: int = 10, start: int = 0) -> runtime.ProviderResult:
        per_page = min(max(rows, 1), MAX_ROWS)
        page = await self._get(KR_SEARCH_PATH, {"dType": "FILE", "keyword": query,
                                                 "currentPage": str(max(start, 0) // per_page + 1),
                                                 "perPage": str(per_page)}, "search")
        parsed = parse_datagokr_search(page)
        if not parsed["results"] and "apply-result" not in page and "data-list-group" not in page:
            raise runtime.InvalidProviderResponse(f"{self.portal_id} did not return its search page.")
        portal = self.portal
        packages = [{
            "portal": self.portal_id, "region": portal["region"], "country": portal["country"],
            "id": r["id"], "name": None, "title": r["title"], "description": r["description"],
            "organization": r["organization"], "license": None, "updated_at": r["updated_at"],
            "tags": r["tags"], "landing_url": f"{portal['base']}/data/{r['id']}/fileData.do",
            # The file link lives on the dataset page (see resolve_download / opendata.table).
            "resources": [], "resource_count": len(r["formats"]) or 1,
        } for r in parsed["results"]]
        return runtime.ProviderResult(
            value={"portal": self.portal_id, "count": parsed["count"], "results": packages},
            cost_micros=0, cost_measured=True,
            usage=f"portal={self.portal_id} count={parsed['count']} rows={len(packages)}")

    async def resolve_download(self, dataset_id: str) -> runtime.ProviderResult:
        page = await self._get(f"/data/{dataset_id}/fileData.do", None, f"dataset {dataset_id}")
        link = parse_datagokr_download(page)
        if not link:
            raise runtime.PermanentProviderError(
                f"{self.portal_id} dataset {dataset_id} has no file download link on its page (API-only dataset?).")
        title = _re.search(r"<title>(.*?)</title>", page, _re.S)
        return runtime.ProviderResult(value={"url": link, "title": _kr_text(title.group(1)).split("|")[0].strip() if title else None},
                                      cost_micros=0, cost_measured=True, usage=f"dataset={dataset_id}")


def _adapter_for(pid: str):
    kind = PORTALS[pid].get("kind", "ckan")
    if kind == "dcat-europa":
        return _Europa(pid)
    if kind == "datagokr":
        return _DataGoKr(pid)
    return _Ckan(pid)


ADAPTERS = {pid: _adapter_for(pid) for pid in PORTALS}
PROVIDERS = list(ADAPTERS.values())
