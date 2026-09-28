"""Current news from the GDELT Project, read through Google BigQuery.

GDELT monitors the world's news in 65 languages and publishes every article
it sees, every 15 minutes, as open data: "available for unlimited and
unrestricted use for any academic, commercial, or governmental use of any
kind without fee", with a citation and a link to https://www.gdeltproject.org/
on every use (gdeltproject.org/about.html). Every answer here carries both.

Why BigQuery and not GDELT's DOC API: the API allows one request per five
seconds per address and refused this node outright on 2026-09-28, while the
same data sits in BigQuery's public `gdelt-bq.gdeltv2.gkg_partitioned`
table, where a one-day search scans about 100 MB (well under a tenth of a
cent) and never rate-limits.

Titles are kept in their original language. GDELT stores non-ASCII
characters as HTML numeric entities (半 -> &#x534A;), so a search term is
encoded the same way before matching and titles are decoded on the way out.
"""

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .. import runtime
from . import bigquery

TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"
CITATION = {"text": "Source: The GDELT Project", "url": "https://www.gdeltproject.org/"}
MAX_TERMS = 6
MAX_GIB = 20.0
# BCP-47 primary language -> the ISO 639-3 code in GKG's TranslationInfo
# ("srclc:jpn;eng:GT-JPN 1.0"). English articles carry no TranslationInfo.
SRCLC = {
    "ar": "ara", "bg": "bul", "bn": "ben", "ca": "cat", "cs": "ces", "da": "dan", "de": "deu", "el": "ell",
    "es": "spa", "et": "est", "fa": "fas", "fi": "fin", "fr": "fra", "he": "heb", "hi": "hin", "hr": "hrv",
    "hu": "hun", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor", "lt": "lit", "lv": "lav", "ms": "msa",
    "nl": "nld", "no": "nor", "nb": "nor", "pl": "pol", "pt": "por", "ro": "ron", "ru": "rus", "sk": "slk",
    "sl": "slv", "sr": "srp", "sv": "swe", "sw": "swa", "ta": "tam", "th": "tha", "tl": "tgl", "tr": "tur",
    "uk": "ukr", "ur": "urd", "vi": "vie", "zh": "zho",
}
LANG_OF_SRCLC = {v: k for k, v in SRCLC.items() if k != "nb"}
_SRCLC = re.compile(r"srclc:([a-z]{3})")


def encode_term(term: str) -> str:
    """A search term as it appears inside GKG's PAGE_TITLE: non-ASCII
    characters as &#xHHHH; entities, then lower-cased (the SQL compares
    LOWER(title), and lower-casing both sides keeps the hex digits aligned)."""
    return "".join(ch if ord(ch) < 128 else f"&#x{ord(ch):X};" for ch in term).lower()


def _like_literal(pattern: str) -> str:
    """A LIKE pattern body inside a single-quoted SQL literal."""
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "\\'")
    return f"'%{escaped}%'"


def terms_of(query: str) -> list:
    words = [w for w in re.split(r"[\s　]+", query.strip()) if w]
    return words[:MAX_TERMS]


def build_sql(terms: list, since: datetime, language: Optional[str], limit: int) -> str:
    days = max(1, (datetime.now(timezone.utc) - since).days + 1)
    stamp = since.strftime("%Y%m%d%H%M%S")
    matches = " AND ".join(f"LOWER(title) LIKE {_like_literal(encode_term(t))}" for t in terms) or "TRUE"
    if language is None:
        lang = "TRUE"
    elif language == "en":
        lang = "TranslationInfo IS NULL"
    else:
        lang = f"TranslationInfo LIKE 'srclc:{SRCLC[language]}%'"
    return (
        "SELECT DATE, SourceCommonName, DocumentIdentifier, TranslationInfo, title FROM ("
        "SELECT DATE, SourceCommonName, DocumentIdentifier, TranslationInfo, "
        "REGEXP_EXTRACT(Extras, r'<PAGE_TITLE>(.*?)</PAGE_TITLE>') AS title "
        f"FROM `{TABLE}` "
        f"WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY) "
        f"AND DATE >= {stamp} AND {lang}) "
        f"WHERE title IS NOT NULL AND {matches} "
        f"ORDER BY DATE DESC LIMIT {int(limit)}")


def _iso(date_value) -> Optional[str]:
    try:
        return datetime.strptime(str(date_value)[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) \
            .isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def article(row: dict) -> dict:
    src = _SRCLC.search(row.get("TranslationInfo") or "")
    domain = row.get("SourceCommonName") or None
    return {"title": html.unescape(row.get("title") or "").strip(),
            "url": row.get("DocumentIdentifier"),
            "source_name": domain,
            "source_url": f"https://{domain}" if domain else None,
            "published_at": _iso(row.get("DATE")),
            "summary": None,
            "language": LANG_OF_SRCLC.get(src.group(1), src.group(1)) if src else "en",
            "feed": "gdelt"}


class _Gdelt:
    id = "gdelt-bigquery"

    def available(self) -> bool:
        return bigquery.PROVIDERS[0].available()

    def unavailable_reason(self) -> str:
        return bigquery.PROVIDERS[0].unavailable_reason()

    async def search(self, terms: list, since_hours: int, language: Optional[str], limit: int) -> runtime.ProviderResult:
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        sql = build_sql(terms, since, language, limit)
        result = await bigquery.PROVIDERS[0].query(sql, max_gib=MAX_GIB)
        value = result.value
        return runtime.ProviderResult(
            value={"articles": [article(r) for r in value["rows"] if isinstance(r, dict)],
                   "gib_processed": value.get("gib_processed")},
            cost_micros=result.cost_micros, cost_measured=result.cost_measured,
            usage=f"rows={value['row_count']} gib={value.get('gib_processed')}")


GDELT = _Gdelt()
PROVIDERS = [GDELT]
