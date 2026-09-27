"""News feeds, keyless: Google News search editions in 71 verified
language/region pairs, and Yahoo Finance per-ticker headlines.

Google News RSS is a search over the world's publishers in the reader's own
language and edition: `hl` (language), `gl` (country) and `ceid`
(country:language) pick the edition, and a query in any script returns
that edition's matches, newest first, up to 100 per page. Every pair in
EDITIONS returned a full page on 2026-09-27; an unknown pair is still tried
(Google serves many more) and falls back to en/US with a note when it comes
back empty. Yahoo Finance's RSS gives the headlines tagged to one ticker,
including non-US listings (7203.T, 005930.KS).

Both are fetched at call time and never cached here.
"""

import html
import os
import re
from datetime import timezone
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_NEWS_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_NEWS_USER_AGENT",
                            "Mozilla/5.0 (X11; Linux x86_64) HubVibe-worker/1.0")
GOOGLE_NEWS_BASE = os.environ.get("WORKER_GOOGLE_NEWS_BASE", "https://news.google.com/rss")
YAHOO_RSS_BASE = os.environ.get("WORKER_YAHOO_RSS_BASE", "https://feeds.finance.yahoo.com/rss/2.0/headline")
MAX_SUMMARY_CHARS = 500

# language -> default country edition; every pair verified live 2026-09-27.
DEFAULT_REGION = {
    "en": "US", "ja": "JP", "ko": "KR", "zh-TW": "TW", "zh-HK": "HK", "zh-CN": "CN", "zh": "CN",
    "de": "DE", "fr": "FR", "es": "ES", "es-419": "MX", "pt-BR": "BR", "pt-PT": "PT", "pt": "BR",
    "it": "IT", "nl": "NL", "ru": "RU", "ar": "AE", "hi": "IN", "id": "ID", "th": "TH", "vi": "VN",
    "tr": "TR", "pl": "PL", "sv": "SE", "uk": "UA", "he": "IL", "ms": "MY", "el": "GR", "cs": "CZ",
    "hu": "HU", "ro": "RO", "da": "DK", "no": "NO", "fi": "FI", "bn": "BD", "ta": "IN", "te": "IN",
    "mr": "IN", "sw": "KE", "fil": "PH", "ur": "PK", "fa": "IR", "sr": "RS", "bg": "BG", "hr": "HR",
    "sk": "SK", "sl": "SI", "lt": "LT", "lv": "LV", "et": "EE",
}
EDITIONS = set(DEFAULT_REGION.items()) | {
    ("en", "GB"), ("en", "IN"), ("en", "AU"), ("en", "SG"), ("ar", "EG"), ("en", "ZA"), ("en", "NG"),
    ("fr", "CA"), ("en", "CA"), ("de", "AT"), ("de", "CH"), ("fr", "BE"), ("nl", "BE"), ("es", "AR"),
    ("es", "CO"), ("es", "CL"), ("es", "PE"), ("en", "PH"), ("en", "NZ"), ("en", "IE"), ("en", "PK"),
    ("ja", "US"),
}


def edition_for(language: str, region: Optional[str]) -> tuple:
    """(hl, gl, ceid) for a language tag and optional country."""
    lang = language.strip()
    base = lang.split("-")[0].lower()
    key = lang if lang in DEFAULT_REGION else (base if base in DEFAULT_REGION else lang)
    hl = key if key in DEFAULT_REGION else lang
    gl = (region or DEFAULT_REGION.get(key) or "US").upper()
    return hl, gl, f"{gl}:{hl}"


def is_verified_edition(hl: str, gl: str) -> bool:
    return (hl, gl) in EDITIONS


_TAG = re.compile(r"<[^>]+>")


def strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return html.unescape(_TAG.sub(" ", text)).replace("\xa0", " ").strip()


def _iso(pubdate: Optional[str]) -> Optional[str]:
    if not pubdate:
        return None
    try:
        dt = parsedate_to_datetime(pubdate)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_rss(body: bytes, feed: str) -> list:
    """RSS 2.0 items -> article records. Google News writes 'Title - Source'
    and carries a <source url=...> element; Yahoo carries neither."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise runtime.InvalidProviderResponse(f"{feed} did not return RSS: {exc}") from None
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        source = item.find("source")
        source_name = (source.text or "").strip() if source is not None and source.text else None
        source_url = source.get("url") if source is not None else None
        if source_name and title.endswith(" - " + source_name):
            title = title[: -len(source_name) - 3].strip()
        summary = strip_html(item.findtext("description"))
        if summary == title or (summary and summary.startswith(title) and len(summary) - len(title) < 12):
            summary = ""
        out.append({
            "title": title,
            "url": link,
            "source_name": source_name,
            "source_url": source_url,
            "published_at": _iso(item.findtext("pubDate")),
            "summary": summary[:MAX_SUMMARY_CHARS] or None,
            "feed": feed,
        })
    return out


async def _get(url: str, params: dict, what: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "application/rss+xml, application/xml, text/xml"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{what} unreachable: {exc}") from exc
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{what} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429 else "provider_transient")
    if response.status_code >= 400:
        raise runtime.PermanentProviderError(f"{what} returned {response.status_code}")
    return response.content


class _GoogleNews:
    id = "google-news-rss"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def search(self, query: str, language: str = "en", region: Optional[str] = None) -> runtime.ProviderResult:
        hl, gl, ceid = edition_for(language, region)
        body = await _get(f"{GOOGLE_NEWS_BASE}/search", {"q": query, "hl": hl, "gl": gl, "ceid": ceid},
                          f"Google News ({ceid})")
        articles = parse_rss(body, "google_news")
        return runtime.ProviderResult(
            value={"edition": {"hl": hl, "gl": gl, "ceid": ceid, "verified": is_verified_edition(hl, gl)},
                   "articles": articles},
            cost_micros=0, cost_measured=True, usage=f"edition={ceid} items={len(articles)}")


class _YahooFinanceRss:
    id = "yahoo-finance-rss"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def headlines(self, symbol: str) -> runtime.ProviderResult:
        body = await _get(YAHOO_RSS_BASE, {"s": symbol, "region": "US", "lang": "en-US"},
                          f"Yahoo Finance RSS ({symbol})")
        articles = parse_rss(body, "yahoo_finance")
        for a in articles:
            a["source_name"] = a["source_name"] or "Yahoo Finance"
            a["source_url"] = a["source_url"] or "https://finance.yahoo.com"
        return runtime.ProviderResult(value={"articles": articles}, cost_micros=0, cost_measured=True,
                                      usage=f"symbol={symbol} items={len(articles)}")


GOOGLE = _GoogleNews()
YAHOO = _YahooFinanceRss()
PROVIDERS = [GOOGLE, YAHOO]
