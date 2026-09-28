"""Company facts from sources that allow reuse, keyless, verified 2026-09-28.

  Wikidata   structured data CC0 ("released into the public domain under
             Creative Commons Zero"); found by the company's official
             website (P856) or by name; descriptive User-Agent with contact
  GLEIF      the global LEI register, CC0. GLEIF's terms forbid implying
             its endorsement, so nothing here says "GLEIF-verified"
  SEC EDGAR  US filers' own registration facts, public information; the
             declared User-Agent the SEC requires
The company's own website (schema.org Organization JSON-LD) is read by the
skill through the guarded raw fetcher, and marked as self-declared.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_COMPANY_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_COMPANY_USER_AGENT", "HubVibe Hubvibe@hubvibe-io.com (+https://hubvibe-io.com)")
WDQS = "https://query.wikidata.org/sparql"
WD_API = "https://www.wikidata.org/w/api.php"
WD_ENTITY = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
GLEIF = "https://api.gleif.org/api/v1"
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
# business, enterprise, company, public company, organization, corporation,
# conglomerate, subsidiary, privately held company, startup
COMPANY_TYPES = ("Q4830453", "Q6881511", "Q783794", "Q891723", "Q43229", "Q167037", "Q206361", "Q658255", "Q5621421",
                 "Q20012034")
COMPANY_CLASSES = " ".join(f"wd:{q}" for q in COMPANY_TYPES)


async def _get(url: str, params: Optional[dict], what: str, accept: str = "application/json"):
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept": accept})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{what} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{what} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429 else "provider_transient")
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise runtime.InvalidProviderResponse(f"{what} answered HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{what} did not return JSON.") from None


def _result(value, usage: str) -> runtime.ProviderResult:
    return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True, usage=usage)


def site_variants(domain: str) -> list:
    hosts = [domain, f"www.{domain}"] if not domain.startswith("www.") else [domain, domain[4:]]
    return [f"{scheme}://{h}{slash}" for scheme in ("https", "http") for h in hosts for slash in ("/", "")]


class _Keyless:
    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""


class _Wikidata(_Keyless):
    id = "wikidata"

    async def by_domain(self, domain: str) -> runtime.ProviderResult:
        """Items whose official website is this domain AND that are companies:
        registry identifiers, a listing, a headcount, or typed as a business.
        A product or brand sharing the site (Apple's apps, Toyota's marques)
        is not returned; the company with the most sitelinks comes first."""
        values = " ".join(f"<{u}>" for u in site_variants(domain))
        query = (f"SELECT ?item (MAX(?l) AS ?links) WHERE {{ VALUES ?site {{ {values} }} ?item wdt:P856 ?site . "
                 f"OPTIONAL {{ ?item wikibase:sitelinks ?l }} "
                 f"FILTER(EXISTS {{ ?item wdt:P1278 [] }} || EXISTS {{ ?item wdt:P5531 [] }} || EXISTS {{ ?item wdt:P414 [] }} "
                 f"|| EXISTS {{ ?item wdt:P1128 [] }} || EXISTS {{ ?item wdt:P31 ?t . VALUES ?t {{ {COMPANY_CLASSES} }} }}) }} "
                 f"GROUP BY ?item ORDER BY DESC(?links) LIMIT 3")
        try:
            data = await _get(WDQS, {"query": query, "format": "json"}, "Wikidata query", "application/sparql-results+json")
            ids = [b["item"]["value"].rsplit("/", 1)[-1] for b in ((data or {}).get("results") or {}).get("bindings", [])]
            return _result(ids, f"items={len(ids)}")
        except runtime.TransientProviderError:
            pass
        # The query service is rate-limited per address; Wikidata's search
        # index answers the same website lookup.
        ids = []
        for url in site_variants(domain)[:4]:
            data = await _get(WD_API, {"action": "query", "list": "search", "srsearch": f'haswbstatement:"P856={url}"',
                                       "srlimit": 5, "format": "json"}, "Wikidata search")
            ids += [h["title"] for h in (((data or {}).get("query") or {}).get("search") or []) if h["title"] not in ids]
        return _result(ids[:5], f"items={len(ids)} via=search")

    async def search(self, name: str) -> runtime.ProviderResult:
        data = await _get(WD_API, {"action": "wbsearchentities", "search": name, "language": "en", "type": "item",
                                   "limit": 7, "format": "json"}, "Wikidata search")
        hits = [{"id": h["id"], "label": h.get("label"), "description": h.get("description")}
                for h in (data or {}).get("search", [])]
        return _result(hits, f"hits={len(hits)}")

    async def entity(self, qid: str) -> runtime.ProviderResult:
        data = await _get(WD_ENTITY.format(qid=qid), None, "Wikidata entity")
        ent = ((data or {}).get("entities") or {}).get(qid)
        return _result(ent, f"qid={qid}")

    async def labels(self, qids: list) -> runtime.ProviderResult:
        out = {}
        for i in range(0, len(qids), 50):
            chunk = qids[i:i + 50]
            data = await _get(WD_API, {"action": "wbgetentities", "ids": "|".join(chunk), "props": "labels|claims",
                                       "languages": "en", "format": "json"}, "Wikidata labels")
            for qid, ent in ((data or {}).get("entities") or {}).items():
                iso = None
                for claim in (ent.get("claims") or {}).get("P297", []):  # ISO 3166-1 alpha-2, on countries
                    iso = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                out[qid] = {"label": ((ent.get("labels") or {}).get("en") or {}).get("value"), "iso2": iso}
        return _result(out, f"labels={len(out)}")


class _Gleif(_Keyless):
    id = "gleif-lei"

    async def by_lei(self, lei: str) -> runtime.ProviderResult:
        data = await _get(f"{GLEIF}/lei-records/{lei}", None, "GLEIF")
        return _result((data or {}).get("data"), f"lei={lei}")

    async def search(self, name: str) -> runtime.ProviderResult:
        data = await _get(f"{GLEIF}/lei-records", {"filter[entity.legalName]": name, "page[size]": 5}, "GLEIF search")
        rows = (data or {}).get("data") or []
        return _result(rows, f"rows={len(rows)}")

    async def legal_form(self, code: str) -> runtime.ProviderResult:
        data = await _get(f"{GLEIF}/entity-legal-forms/{code}", None, "GLEIF legal forms")
        names = (((data or {}).get("data") or {}).get("attributes") or {}).get("names") or []
        english = next((n.get("localName") for n in names if (n.get("language") or "").startswith("en")), None)
        return _result(english or (names[0].get("localName") if names else None), f"elf={code}")


class _Sec(_Keyless):
    id = "sec-edgar-submissions"

    async def submissions(self, cik: int) -> runtime.ProviderResult:
        data = await _get(SEC_SUBMISSIONS.format(cik=int(cik)), None, "SEC EDGAR")
        return _result(data, f"cik={cik}")


WIKIDATA = _Wikidata()
LEI = _Gleif()
SEC = _Sec()
PROVIDERS = [WIKIDATA, LEI, SEC]
