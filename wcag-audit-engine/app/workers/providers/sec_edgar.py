"""SEC EDGAR, official and keyless: XBRL company facts (the filer's own
reported numbers) and insider ownership filings (Form 4, as filed).

The SEC's fair-access policy asks for a descriptive User-Agent and no more
than 10 requests a second; both are honoured here. Two requests per job at
most: the ticker-to-CIK map (a reference table, kept in memory for a few
hours because tickers do not change by the minute -- it is not an answer)
and the company's facts document, fetched fresh every call. Insider reads
take the issuer's filing list plus one small XML document per Form 4, at
most five in flight.
"""

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_SEC_TIMEOUT_SECONDS", "45"))
USER_AGENT = os.environ.get("WORKER_SEC_USER_AGENT", "HubVibe Hubvibe@hubvibe-io.com")
TICKERS_URL = os.environ.get("WORKER_SEC_TICKERS_URL", "https://www.sec.gov/files/company_tickers.json")
FACTS_BASE = os.environ.get("WORKER_SEC_FACTS_BASE", "https://data.sec.gov/api/xbrl/companyfacts")
SUBMISSIONS_BASE = os.environ.get("WORKER_SEC_SUBMISSIONS_BASE", "https://data.sec.gov/submissions")
ARCHIVES_BASE = os.environ.get("WORKER_SEC_ARCHIVES_BASE", "https://www.sec.gov/Archives/edgar/data")
MAX_FORM4_BYTES = 2_000_000
_FORM4_IN_FLIGHT = 5
TICKER_MAP_TTL_SECONDS = float(os.environ.get("WORKER_SEC_TICKER_TTL_SECONDS", str(6 * 3600)))

_ticker_map: dict = {}
_ticker_map_at: float = 0.0


async def _fetch(url: str, accept: str = "application/json", client: Optional[httpx.AsyncClient] = None) -> httpx.Response:
    try:
        if client is not None:
            response = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        else:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as own:
                response = await own.get(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{url} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{url} unreachable: {exc}") from exc
    if response.status_code in (403, 429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"SEC returned {response.status_code} for {url}")
    if response.status_code >= 400 and response.status_code != 404:
        raise runtime.PermanentProviderError(f"SEC returned {response.status_code} for {url}")
    return response


async def _get_json(url: str) -> tuple:
    response = await _fetch(url)
    if response.status_code == 404:
        return 404, None
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{url} did not return JSON.") from None


class _SecEdgar:
    id = "sec-edgar"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def cik_for(self, symbol: str) -> Optional[dict]:
        """{'cik': int, 'title': str} for a ticker, or None when the SEC's
        ticker table does not list it."""
        global _ticker_map, _ticker_map_at
        now = time.monotonic()
        if not _ticker_map or now - _ticker_map_at > TICKER_MAP_TTL_SECONDS:
            _, data = await _get_json(TICKERS_URL)
            if not isinstance(data, dict):
                raise runtime.InvalidProviderResponse("SEC ticker table is not the expected object.")
            fresh = {}
            for entry in data.values():
                if isinstance(entry, dict) and isinstance(entry.get("ticker"), str):
                    fresh[entry["ticker"].upper()] = {"cik": int(entry.get("cik_str") or 0),
                                                      "title": entry.get("title")}
            if not fresh:
                raise runtime.InvalidProviderResponse("SEC ticker table came back empty.")
            _ticker_map, _ticker_map_at = fresh, now
        return _ticker_map.get(symbol.upper())

    async def company_facts(self, cik: int) -> runtime.ProviderResult:
        status, data = await _get_json(f"{FACTS_BASE}/CIK{int(cik):010d}.json")
        if status == 404 or not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
            raise runtime.InvalidRequest(f"The SEC has no XBRL company facts for CIK {int(cik):010d}.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True,
                                      usage=f"cik={int(cik):010d} taxonomies={len(data['facts'])}")

    async def insider_filings(self, cik: int) -> runtime.ProviderResult:
        """The issuer's filing list from data.sec.gov/submissions: name,
        tickers and the recent filings (at least the last year, up to 1,000)."""
        status, data = await _get_json(f"{SUBMISSIONS_BASE}/CIK{int(cik):010d}.json")
        if status == 404 or not isinstance(data, dict) or not isinstance((data.get("filings") or {}).get("recent"), dict):
            raise runtime.InvalidRequest(f"The SEC has no filings list for CIK {int(cik):010d}.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True,
                                      usage=f"cik={int(cik):010d} recent={len(data['filings']['recent'].get('form') or [])}")

    async def form4_documents(self, filings: list) -> runtime.ProviderResult:
        """Each filing's ownership XML parsed; a document that fails is
        returned as {'error': ...} so one bad filing never sinks the rest."""
        gate = asyncio.Semaphore(_FORM4_IN_FLIGHT)

        async def one(client, f):
            error = None
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(0.5 * attempt)
                async with gate:
                    try:
                        response = await _fetch(f["xml_url"], accept="application/xml", client=client)
                    except runtime.TransientProviderError as exc:
                        error = str(exc)[:160]
                        continue
                    except runtime.WorkerError as exc:
                        return {"error": str(exc)[:160]}
                if response.status_code == 404 or len(response.content) > MAX_FORM4_BYTES:
                    return {"error": f"HTTP {response.status_code}, {len(response.content)} bytes"}
                try:
                    return parse_form4(response.content)
                except ET.ParseError as exc:
                    return {"error": f"not an ownership XML document: {exc}"[:160]}
            return {"error": error}
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                     limits=httpx.Limits(max_connections=_FORM4_IN_FLIGHT)) as client:
            docs = await asyncio.gather(*(one(client, f) for f in filings))
        ok = sum(1 for d in docs if "error" not in d)
        if filings and not ok:
            raise runtime.TransientProviderError(f"None of {len(filings)} Form 4 documents could be read: {docs[0]['error']}")
        return runtime.ProviderResult(value=docs, cost_micros=0, cost_measured=True, usage=f"form4={ok}/{len(filings)}")


def _text(node, path: str) -> Optional[str]:
    el = node.find(path) if node is not None else None
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _num(text: Optional[str]):
    if text is None:
        return None
    try:
        f = float(text.replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _flag(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in ("1", "true")


def parse_form4(xml_bytes: bytes) -> dict:
    """One ownership document (Form 3/4/5 XML schema) as plain data."""
    root = ET.fromstring(xml_bytes)
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        owners.append({"name": _text(o, "reportingOwnerId/rptOwnerName"),
                       "cik": _text(o, "reportingOwnerId/rptOwnerCik"),
                       "director": _flag(_text(rel, "isDirector")), "officer": _flag(_text(rel, "isOfficer")),
                       "ten_percent_owner": _flag(_text(rel, "isTenPercentOwner")), "other": _flag(_text(rel, "isOther")),
                       "officer_title": _text(rel, "officerTitle") or _text(rel, "otherText")})
    footnotes = {fn.get("id"): " ".join((fn.text or "").split()) for fn in root.findall("footnotes/footnote")}
    rows = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        for t in root.findall(f"{table}/{'derivativeTransaction' if derivative else 'nonDerivativeTransaction'}"):
            notes = sorted({ref.get("id") for ref in t.iter("footnoteId") if ref.get("id")})
            rows.append({
                "security": _text(t, "securityTitle/value"),
                "date": _text(t, "transactionDate/value"),
                "code": _text(t, "transactionCoding/transactionCode"),
                "acquired_disposed": _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
                "shares": _num(_text(t, "transactionAmounts/transactionShares/value")),
                "price": _num(_text(t, "transactionAmounts/transactionPricePerShare/value")),
                "shares_after": _num(_text(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")),
                "ownership": _text(t, "ownershipNature/directOrIndirectOwnership/value"),
                "derivative": derivative,
                "exercise_price": _num(_text(t, "conversionOrExercisePrice/value")) if derivative else None,
                "underlying_security": _text(t, "underlyingSecurity/underlyingSecurityTitle/value") if derivative else None,
                "footnotes": [footnotes[n] for n in notes if footnotes.get(n)],
            })
    return {"document_type": _text(root, "documentType"), "period": _text(root, "periodOfReport"),
            "issuer_name": _text(root, "issuer/issuerName"), "issuer_symbol": _text(root, "issuer/issuerTradingSymbol"),
            "rule_10b5_1": _flag(_text(root, "aff10b5One")), "owners": owners, "transactions": rows}


def reset_for_tests() -> None:
    global _ticker_map, _ticker_map_at
    _ticker_map, _ticker_map_at = {}, 0.0


PROVIDERS = [_SecEdgar()]
