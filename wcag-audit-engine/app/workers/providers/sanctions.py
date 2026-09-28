"""Sanctions lists, read from the governments' own published files.

Sources, each one licensed for commercial reuse:
  * OFAC SDN list and consolidated (non-SDN) lists -- US Treasury. A work of
    the US government, so in the public domain.
  * UK Sanctions List -- FCDO, published under the Open Government Licence
    v3.0 ("Contains public sector information licensed under the Open
    Government Licence v3.0").
  * EU consolidated financial sanctions list -- European Commission, reusable
    under Commission Decision 2011/833/EU on the reuse of Commission documents.

The UN Security Council Consolidated List is NOT read directly: the un.org
terms of use allow "personal, non-commercial use, without any right to resell
or redistribute". UN designations are implemented in the UK and EU lists,
which carry the UN reference number, so they are still screened.

The files are 1-30 MB each. They are downloaded at most every few hours,
parsed into one compact index kept in memory and on the persistent volume,
and a list that fails to refresh keeps its last good copy (its date says so).
"""

import asyncio
import csv
import gzip
import io
import json
import os
import re
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from typing import Optional

import httpx

from .. import runtime

_UA = {"User-Agent": "HubVibe-worker/1.0 (+https://hubvibe-io.com)"}
_REFRESH_SECONDS = int(os.environ.get("SANCTIONS_REFRESH_SECONDS", str(6 * 3600)))

LISTS = {
    "ofac_sdn": {
        "name": "OFAC Specially Designated Nationals (SDN) List",
        "publisher": "US Department of the Treasury, Office of Foreign Assets Control",
        "license": "US government work (public domain)",
        "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML",
        "page": "https://sanctionssearch.ofac.treas.gov/",
    },
    "ofac_consolidated": {
        "name": "OFAC Consolidated (non-SDN) Sanctions Lists",
        "publisher": "US Department of the Treasury, Office of Foreign Assets Control",
        "license": "US government work (public domain)",
        "url": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONSOLIDATED.XML",
        "page": "https://sanctionssearch.ofac.treas.gov/",
    },
    "uk": {
        "name": "UK Sanctions List",
        "publisher": "UK Foreign, Commonwealth & Development Office",
        "license": "Contains public sector information licensed under the Open Government Licence v3.0",
        "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
        "page": "https://www.gov.uk/government/publications/the-uk-sanctions-list",
    },
    "eu": {
        "name": "EU Consolidated Financial Sanctions List",
        "publisher": "European Commission",
        "license": "Reuse authorised under Commission Decision 2011/833/EU; source: European Commission",
        "url": "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
        "page": "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions",
    },
}

_TYPES = {"individual": "person", "person": "person", "p": "person", "entity": "entity", "enterprise": "entity",
          "e": "entity", "vessel": "vessel", "ship": "vessel", "aircraft": "aircraft"}


# --- name normalisation and matching -------------------------------------------------

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize(text: str) -> str:
    """Lower-case ASCII letters and digits only: accents dropped, punctuation
    and spacing collapsed. "Müller-Lüdenscheidt, Hans" -> "muller ludenscheidt hans"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", ascii_only.casefold()).strip()


def _tokens(text: str) -> tuple:
    return tuple(t for t in normalize(text).split() if t)


def _token_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _coverage(src: tuple, dst: tuple) -> tuple:
    """(share of `src` words found in `dst`, all found exactly?). A word
    counts by its closeness to the best `dst` word, and only when that is at
    least 0.85: "puttin" covers "putin" (0.91), "turin" does not (0.80)."""
    total, exact = 0.0, True
    for token in src:
        best = max(_token_similarity(token, other) for other in dst)
        exact = exact and best == 1.0
        total += best if best >= 0.85 else 0.0
    return total / len(src), exact


def similarity(query_tokens: tuple, name_tokens: tuple) -> float:
    """0..1 for how well a listed name matches the query.

    Every query word found exactly in the listed name scores 0.9 to 1.0 (1.0
    for the same words), so "Saddam Hussein" finds "Saddam Hussein
    Al-Tikriti" and word order never matters. Otherwise a blend of word
    coverage (both ways, the query's side weighted double) and the whole-name
    character similarity: a one-letter misspelling still matches, a different
    surname ("Vladimir Turin" for "Vladimir Putin") does not."""
    if not query_tokens or not name_tokens:
        return 0.0
    cov_q, exact_q = _coverage(query_tokens, name_tokens)
    cov_n, _ = _coverage(name_tokens, query_tokens)
    if exact_q and (len(query_tokens) >= 2 or len(name_tokens) == 1):
        return round(0.9 + 0.1 * cov_n, 4)
    whole = SequenceMatcher(None, " ".join(sorted(query_tokens)), " ".join(sorted(name_tokens))).ratio()
    return round(0.65 * ((2 * cov_q + cov_n) / 3) + 0.35 * whole, 4)


# --- parsing --------------------------------------------------------------------------

def _text(el, tag: str, ns: str = "") -> str:
    child = el.find(f"{ns}{tag}")
    return (child.text or "").strip() if child is not None and child.text else ""


def _uniq(values) -> list:
    out = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _dmy(text: str) -> str:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else (text or "")


def parse_ofac(raw: bytes, list_id: str) -> tuple:
    """(entries, as_of) from OFAC's SDN.XML / CONSOLIDATED.XML."""
    root = ET.fromstring(raw)
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    as_of = ""
    info = root.find(f"{ns}publshInformation")
    if info is not None:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", _text(info, "Publish_Date", ns))  # MM/DD/YYYY
        as_of = f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else ""
    entries = []
    for entry in root.iter(f"{ns}sdnEntry"):
        primary = " ".join(p for p in (_text(entry, "firstName", ns), _text(entry, "lastName", ns)) if p)
        names = [primary]
        for aka in entry.iter(f"{ns}aka"):
            names.append(" ".join(p for p in (_text(aka, "firstName", ns), _text(aka, "lastName", ns)) if p))
        entries.append({
            "list": list_id, "id": _text(entry, "uid", ns),
            "type": _TYPES.get(_text(entry, "sdnType", ns).lower(), "entity"),
            "primary_name": primary, "names": _uniq(names),
            "programs": _uniq(p.text for p in entry.iter(f"{ns}program")),
            "countries": _uniq(c.text for c in entry.iter(f"{ns}country")),
            "birth_dates": _uniq(d.text for d in entry.iter(f"{ns}dateOfBirth")),
            "un_reference": None, "listed_on": None,
        })
    return entries, as_of


def parse_uk(raw: bytes) -> tuple:
    root = ET.fromstring(raw)
    as_of = _dmy(_text(root, "DateGenerated"))
    entries = []
    for d in root.iter("Designation"):
        names, primary = [], ""
        for name in d.iter("Name"):
            full = " ".join(_text(name, f"Name{i}") for i in range(1, 7) if _text(name, f"Name{i}"))
            if not full:
                continue
            names.append(full)
            if _text(name, "NameType").lower().startswith("primary") and not primary:
                primary = full
        entries.append({
            "list": "uk", "id": _text(d, "UniqueID"),
            "type": _TYPES.get(_text(d, "IndividualEntityShip").lower(), "entity"),
            "primary_name": primary or (names[0] if names else ""), "names": _uniq(names),
            "programs": _uniq([_text(d, "RegimeName")]),
            "countries": _uniq(c.text for tag in ("AddressCountry", "Nationality", "CountryOfBirth")
                               for c in d.iter(tag)),
            "birth_dates": _uniq(x.text for x in d.iter("DOB")),
            "un_reference": _text(d, "UNReferenceNumber") or None,
            "listed_on": _dmy(_text(d, "DateDesignated")) or None,
        })
    return [e for e in entries if e["names"]], as_of


def parse_eu(raw: bytes) -> tuple:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")), delimiter=";")
    by_id, as_of = {}, ""
    for row in reader:
        eid = row.get("Entity_LogicalId") or ""
        if not eid:
            continue
        as_of = as_of or _dmy(row.get("fileGenerationDate") or "")
        e = by_id.get(eid)
        if e is None:
            e = by_id[eid] = {
                "list": "eu", "id": row.get("Entity_EU_ReferenceNumber") or eid,
                "type": _TYPES.get((row.get("Entity_SubjectType_ClassificationCode") or "").lower(), "entity"),
                "primary_name": "", "names": [], "programs": [], "countries": [], "birth_dates": [],
                "un_reference": row.get("Entity_UnitedNationId") or None,
                "listed_on": row.get("Entity_DesignationDate") or row.get("Entity_Regulation_PublicationDate") or None,
            }
        whole = (row.get("NameAlias_WholeName") or "").strip()
        if whole and whole not in e["names"]:
            e["names"].append(whole)
            e["primary_name"] = e["primary_name"] or whole
        for key, field in (("programs", "Entity_Regulation_Programme"),
                           ("countries", "Address_CountryDescription"),
                           ("countries", "Citizenship_CountryDescription"),
                           ("birth_dates", "BirthDate_BirthDate")):
            value = (row.get(field) or "").strip()
            if value and value not in e[key]:
                e[key].append(value)
        year = (row.get("BirthDate_Year") or "").strip()
        if year and not any(year in b for b in e["birth_dates"]):
            e["birth_dates"].append(year)
    return [e for e in by_id.values() if e["names"]], as_of


_PARSERS = {
    "ofac_sdn": lambda raw: parse_ofac(raw, "ofac_sdn"),
    "ofac_consolidated": lambda raw: parse_ofac(raw, "ofac_consolidated"),
    "uk": parse_uk,
    "eu": parse_eu,
}


# --- the index --------------------------------------------------------------------------

def _cache_path() -> str:
    explicit = os.environ.get("SANCTIONS_CACHE_PATH")
    if explicit:
        return explicit
    base = "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else tempfile.gettempdir()
    return os.path.join(base, "hubvibe-sanctions.json.gz")


class _Index:
    """All four lists, their dates, and a word-prefix index over every name."""

    def __init__(self):
        self.lists = {}      # list_id -> {"entries": [...], "as_of": str, "fetched": float}
        self.names = []      # (entry, name, tokens)
        self.prefix = {}     # first 3 characters of a word -> [name index]

    def rebuild(self):
        names, prefix = [], {}
        for data in self.lists.values():
            for entry in data["entries"]:
                for name in entry["names"]:
                    toks = _tokens(name)
                    if not toks:
                        continue
                    i = len(names)
                    names.append((entry, name, toks))
                    for t in set(toks):
                        prefix.setdefault(t[:3], []).append(i)
        self.names, self.prefix = names, prefix

    def candidates(self, query_tokens: tuple):
        """Names sharing a word start with at least half of the query's
        distinctive words (two-letter particles like "de" or "al" are not
        used to find candidates, only to score them)."""
        keys = [t[:3] for t in query_tokens if len(t) > 2] or [t[:3] for t in query_tokens]
        need = max(1, (len(set(keys)) + 1) // 2)
        hits = {}
        for key in set(keys):
            for i in self.prefix.get(key, ()):
                hits[i] = hits.get(i, 0) + 1
        for i, count in hits.items():
            if count >= need:
                yield self.names[i]


class _Sanctions:
    id = "sanctions-lists"

    def __init__(self):
        self._index = _Index()
        self._lock = asyncio.Lock()
        self._loaded_disk = False
        self._background = None

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def _load_disk(self):
        self._loaded_disk = True
        try:
            with gzip.open(_cache_path(), "rt", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("lists"):
                self._index.lists = data["lists"]
                self._index.rebuild()
        except (OSError, ValueError, EOFError):
            pass

    def _save_disk(self):
        path = _cache_path()
        try:
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump({"lists": self._index.lists}, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    async def _fetch(self, client, list_id: str) -> Optional[tuple]:
        try:
            response = await client.get(LISTS[list_id]["url"], headers=_UA)
            if response.status_code != 200 or len(response.content) < 10_000:
                return None
            return await asyncio.to_thread(_PARSERS[list_id], response.content)
        except (httpx.HTTPError, ET.ParseError, ValueError, csv.Error):
            return None

    def _stale(self) -> list:
        now = time.time()
        return [lid for lid in LISTS
                if now - (self._index.lists.get(lid) or {}).get("fetched", 0) > _REFRESH_SECONDS]

    async def ensure_fresh(self) -> dict:
        """Each list at most _REFRESH_SECONDS old; a list that fails to
        refresh keeps its last good copy. Returns {list_id: refreshed?}."""
        if not self._loaded_disk:
            await asyncio.to_thread(self._load_disk)
        if not self._stale():
            return {}
        async with self._lock:
            stale = self._stale()
            if not stale:
                return {}
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0), follow_redirects=True) as client:
                results = await asyncio.gather(*(self._fetch(client, lid) for lid in stale))
            refreshed = {}
            for lid, result in zip(stale, results):
                if result and result[0]:
                    entries, as_of = result
                    self._index.lists[lid] = {"entries": entries, "as_of": as_of, "fetched": time.time()}
                    refreshed[lid] = True
                else:
                    refreshed[lid] = False
                    if lid in self._index.lists:  # keep the last good copy; retry in 15 minutes
                        self._index.lists[lid]["fetched"] = time.time() - _REFRESH_SECONDS + 900
            if any(refreshed.values()):
                await asyncio.to_thread(self._index.rebuild)
                await asyncio.to_thread(self._save_disk)
            return refreshed

    def _refresh_in_background(self):
        if self._background is None or self._background.done():
            self._background = asyncio.ensure_future(self.ensure_fresh())

    async def keep_warm(self, every_seconds: int = 1800):
        """Run for the life of the process: load the lists at startup and
        refresh them as they go stale, so no buyer waits on a download."""
        while True:
            try:
                await self.ensure_fresh()
            except Exception:  # pragma: no cover - never let the loop die
                pass
            await asyncio.sleep(every_seconds)

    async def screen(self, name: str, kind: str = "any", threshold: float = 0.85, limit: int = 10,
                     lists: Optional[list] = None) -> runtime.ProviderResult:
        if not self._loaded_disk:
            await asyncio.to_thread(self._load_disk)
        if not self._index.lists:
            await self.ensure_fresh()  # nothing to answer from yet: this call waits for the download
        elif self._stale():
            self._refresh_in_background()  # answer from the copy held; its date is in the result
        if not self._index.lists:
            raise runtime.TransientProviderError("No sanctions list could be downloaded; try again shortly.")
        query_tokens = _tokens(name)
        wanted = set(lists or LISTS)
        best = {}  # (list, id) -> (score, matched_name, entry)
        for entry, candidate_name, toks in self._index.candidates(query_tokens):
            if entry["list"] not in wanted or (kind != "any" and entry["type"] != kind):
                continue
            score = similarity(query_tokens, toks)
            key = (entry["list"], entry["id"])
            if score >= threshold and score > best.get(key, (0,))[0]:
                best[key] = (score, candidate_name, entry)
        ranked = sorted(best.values(), key=lambda x: (-x[0], x[2]["primary_name"]))[:limit]
        meta = [{"list": lid, "name": LISTS[lid]["name"], "publisher": LISTS[lid]["publisher"],
                 "license": LISTS[lid]["license"], "source_url": LISTS[lid]["page"],
                 "as_of": (self._index.lists.get(lid) or {}).get("as_of") or None,
                 "entries": len((self._index.lists.get(lid) or {}).get("entries") or []),
                 "loaded": lid in self._index.lists}
                for lid in LISTS if lid in wanted]
        return runtime.ProviderResult(
            value={"matches": [{"score": s, "matched_name": n, "entry": e} for s, n, e in ranked], "lists": meta},
            cost_micros=0, cost_measured=True,
            usage=f"matches={len(ranked)} names={len(self._index.names)}")


SANCTIONS = _Sanctions()
PROVIDERS = [SANCTIONS]
