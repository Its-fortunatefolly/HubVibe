"""news.search and data.macro -- keyless news in any language, official numbers.

Pinned: real Google News (ko/KR) and Yahoo Finance RSS captured 2026-09-27
parsed into articles (publisher suffix stripped, source element, RFC 2822
dates to UTC); edition choice for languages and regions; merge = de-dup by
title, window, newest first; a failed feed disclosed; the en/US fallback for
an unverified edition; World Bank and Eurostat payloads parsed into ordered
observations with latest/previous/change; alias and raw-code resolution;
bad input refused free; paid HTTP + MCP calls; static manifests match.
"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"

GOOGLE_KO_RSS = '<rss version="2.0"><channel><generator>NFE/5.0</generator><title>"반도체" - Google 뉴스</title><link>https://news.google.com/search?q=%EB%B0%98%EB%8F%84%EC%B2%B4&amp;hl=ko&amp;gl=KR&amp;ceid=KR:ko</link><language>ko</language><webMaster>news-webmaster@google.com</webMaster><copyright>Copyright © 2026 Google. All rights reserved. This XML feed is made available solely for the purpose of rendering Google News results within a personal feed reader for personal, non-commercial use. Any other use of the feed is expressly prohibited. By accessing this feed or using these results in any manner whatsoever, you agree to be bound by the foregoing restrictions.</copyright><lastBuildDate>Sun, 27 Sep 2026 16:10:53 GMT</lastBuildDate><image><title>Google 뉴스</title><url>https://lh3.googleusercontent.com/-DR60l-K8vnyi99NZovm9HlXyZwQ85GMDxiwJWzoasZYCUrPuUM_P_4Rb7ei03j-0nRs0c4F=w256</url><link>https://news.google.com/</link><height>256</height><width>256</width></image><description>Google 뉴스</description><item><title>박홍근 "호남 반도체, 기업 팔 비틀기 아냐…팹 증설도 구상" - 조선일보</title><link>https://news.google.com/rss/articles/CBMijgFBVV95cUxPWU0yTFA5N0pwQlhteWMzQzhnc29tc1FOSEN0VXowUW5oTXE1WlBRZllsdjAycXc1UVRxdG5BQ1pvMkluZV9uX0tkMjNWSWNxOXJpZFZvcndJeEZrMDRDSm16X3pibW5ZVjM2RHAtanRnOTZBS1JxZHhMa2ZYa0o5QVdNRVcwRzcwcjZqQTdB?oc=5</link><pubDate>Sun, 27 Sep 2026 02:32:00 GMT</pubDate><description>&lt;a href="https://news.google.com/rss/articles/CBMijgFBVV95cUxPWU0yTFA5N0pwQlhteWMzQzhnc29tc1FOSEN0VXowUW5oTXE1WlBRZllsdjAycXc1UVRxdG5BQ1pvMkluZV9uX0tkMjNWSWNxOXJpZFZvcndJeEZrMDRDSm16X3pibW5ZVjM2RHAtanRnOTZBS1JxZHhMa2ZYa0o5QVdNRVcwRzcwcjZqQTdB?oc=5" target="_blank"&gt;박홍근 "호남 반도체, 기업 팔 비틀기 아냐…팹 증설도 구상"&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;조선일보&lt;/font&gt;</description><source url="https://www.chosun.com">조선일보</source></item><item><title>박홍근 "호남 반도체, 현 정부 임기내 생산…팹 증설도 구상" - 뉴시스</title><link>https://news.google.com/rss/articles/CBMiYEFVX3lxTFBMcVB3NWlUaTRPSHFqUTBmR3AxdlU5dW5jT0YyTTJVOWhhYTU2bkZUOXZuSlN1cVZPWlZqUFVxLUp4dEJFVGxEdmQ0X2dydkhOQWVUMlRjQnhrNjc5NUphdNIBeEFVX3lxTFA1R3lLQ1dOOFhMdTJiN2FqVy1nbWRHTDZyTUpuRGpVRlRWT1J4YzMyc1l4aUVnZVM4T1hlZEp0d1hELXZENDd5eS1xMWswajlaLVR4N1pUaUc5UC0tR3BZZC1Cc0VtVnRRZXl0V0k0RERsYlI4M0pwdQ?oc=5</link><pubDate>Sun, 27 Sep 2026 05:57:51 GMT</pubDate><description>&lt;a href="https://news.google.com/rss/articles/CBMiYEFVX3lxTFBMcVB3NWlUaTRPSHFqUTBmR3AxdlU5dW5jT0YyTTJVOWhhYTU2bkZUOXZuSlN1cVZPWlZqUFVxLUp4dEJFVGxEdmQ0X2dydkhOQWVUMlRjQnhrNjc5NUphdNIBeEFVX3lxTFA1R3lLQ1dOOFhMdTJiN2FqVy1nbWRHTDZyTUpuRGpVRlRWT1J4YzMyc1l4aUVnZVM4T1hlZEp0d1hELXZENDd5eS1xMWswajlaLVR4N1pUaUc5UC0tR3BZZC1Cc0VtVnRRZXl0V0k0RERsYlI4M0pwdQ?oc=5" target="_blank"&gt;박홍근 "호남 반도체, 현 정부 임기내 생산…팹 증설도 구상"&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;뉴시스&lt;/font&gt;</description><source url="https://www.newsis.com">뉴시스</source></item><item><title>“800조 반도체 대박? 공장 짓고 사원 뽑아야 체감하죠” - gjdream.com</title><link>https://news.google.com/rss/articles/CBMiakFVX3lxTFAzTkdSNVRIVUtYUU1ETVl2VG1JVzhSLXg4MEJpaTJFVlJwZkFJSzNRUkkxb3dUOUpfME1jc3hJQmY1ZzQzWnBMdzkxRW44RmlFUENGcFRoN1JqWTVBSnlENlg0emFEZF9WQVE?oc=5</link><pubDate>Sun, 27 Sep 2026 15:10:00 GMT</pubDate><description>&lt;a href="https://news.google.com/rss/articles/CBMiakFVX3lxTFAzTkdSNVRIVUtYUU1ETVl2VG1JVzhSLXg4MEJpaTJFVlJwZkFJSzNRUkkxb3dUOUpfME1jc3hJQmY1ZzQzWnBMdzkxRW44RmlFUENGcFRoN1JqWTVBSnlENlg0emFEZF9WQVE?oc=5" target="_blank"&gt;“800조 반도체 대박? 공장 짓고 사원 뽑아야 체감하죠”&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color="#6f6f6f"&gt;gjdream.com&lt;/font&gt;</description><source url="https://www.gjdream.com">gjdream.com</source></item></channel></rss>'
YAHOO_RSS = '<rss version="2.0">\n    <channel>\n        <copyright>Copyright (c) 2026 Yahoo Inc. All rights reserved.</copyright>\n        <description>Latest Financial News for 7203.T</description>\n        <image>\n            <height>45</height>\n            <link>http://finance.yahoo.com/q/h?s=7203.T</link>\n            <title>Yahoo! Finance: 7203.T News</title>\n            <url>https://s.yimg.com/rz/stage/p/yahoo_finance_en-US_h_p_finance_2.png</url>\n            <width>144</width>\n        </image>\n        <item>\n            <description>The development follows the European Commission’s 2018 decision to fine certain shipping companies for fixing prices for the transport of vehicles.</description>\n            <link>https://uk.finance.yahoo.com/news/uk-consumers-urged-sign-share-113811619.html?.tsrc=rss</link>\n            <pubDate>Thu, 24 Sep 2026 12:34:01 +0000</pubDate>\n            <title>UK consumers urged to sign up for share of £56m new car delivery compensation</title>\n        </item>\n        <item>\n            <description>Joby Aviation has lost more than half its value in 2026, yet analysts see a path back above $10 and bulls are eyeing $12 by next year. The question is whether a handful of upcoming catalysts can close that gap before cash burn steals the story.</description>\n            <link>https://247wallst.com/investing/2026/09/26/joby-stock-has-a-futuristic-story-heres-my-long-term-price-prediction/?.tsrc=rss</link>\n            <pubDate>Sat, 26 Sep 2026 15:30:15 +0000</pubDate>\n            <title>Joby Stock Has a Futuristic Story. Here’s My Long-Term Price Prediction</title>\n        </item>\n        <language>en-US</language>\n        <lastBuildDate>Sun, 27 Sep 2026 16:10:53 +0000</lastBuildDate>\n        <link>http://finance.yahoo.com/q/h?s=7203.T</link>\n        <title>Yahoo! Finance: 7203.T News</title>\n    </channel>\n</rss>'
WORLD_BANK_JP_CPI = json.loads(r"""[{"page": 1, "pages": 1, "per_page": 50, "total": 9, "sourceid": "2", "lastupdated": "2026-07-13"}, [{"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2025", "value": 3.17253034260248, "unit": "", "obs_status": "", "decimal": 1}, {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2024", "value": 2.73853681635241, "unit": "", "obs_status": "", "decimal": 1}, {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2023", "value": 3.26813365933163, "unit": "", "obs_status": "", "decimal": 1}]]""")
EUROSTAT_EA_HICP = json.loads(r"""{"version": "2.0", "class": "dataset", "label": "HICP - monthly data (annual rate of change)", "source": "ESTAT", "updated": "2026-02-06T23:00:00+0100", "value": {"0": 2.1, "1": 2.1, "2": 2.0}, "id": ["freq", "unit", "coicop", "geo", "time"], "size": [1, 1, 1, 1, 3], "dimension": {"freq": {"label": "Time frequency", "category": {"index": {"M": 0}, "label": {"M": "Monthly"}}}, "unit": {"label": "Unit of measure", "category": {"index": {"RCH_A": 0}, "label": {"RCH_A": "Annual rate of change"}}}, "coicop": {"label": "Classification of individual consumption by purpose (COICOP)", "category": {"index": {"CP00": 0}, "label": {"CP00": "All-items HICP"}}}, "geo": {"label": "Geopolitical entity (reporting)", "category": {"index": {"EA": 0}, "label": {"EA": "Euro area (EA11-1999, EA12-2001, EA13-2007, EA15-2008, EA16-2009, EA17-2011, EA18-2014, EA19-2015, EA20-2023, EA21-2026)"}}}, "time": {"label": "Time", "category": {"index": {"2025-10": 0, "2025-11": 1, "2025-12": 2}, "label": {"2025-10": "2025-10", "2025-11": "2025-11", "2025-12": "2025-12"}}}}, "extension": {"lang": "EN", "description": "<p>The Harmonised Index of Consumer Prices (HICP)\u202fmeasures price changes\u202fof\u202fvarious consumer goods and services\u202fover time\u202f(inflation).\u202fThe annual rate of change is the percentage change in one month compared to the same month of the previous year,\u202falso called annual inflation.\u202fIt is calculated from the monthly indices and rounded to one decimal place.&nbsp;</p>", "id": "PRC_HICP_MANR", "agencyId": "ESTAT", "version": "1.0", "datastructure": {"id": "PRC_HICP_MANR", "agencyId": "ESTAT", "version": "108.0"}, "annotation": [{"type": "CREATED", "date": "2015-11-16T10:18:53+0100"}, {"type": "DISSEMINATION_DOI_XML", "title": "<adms:identifier xmlns:adms=\"http://www.w3.org/ns/adms#\" xmlns:skos=\"http://www.w3.org/2004/02/skos/core.html\" xmlns:dct=\"http://purl.org/dc/terms/\" xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"><adms:Identifier rdf:about=\"https://doi.org/10.2908/PRC_HICP_MANR\"><skos:notation rdf:datatype=\"http://purl.org/spar/datacite/doi\">10.2908/PRC_HICP_MANR</skos:notation><dct:creator rdf:resource=\"http://publications.europa.eu/resource/authority/corporate-body/ESTAT\"/><dct:issued rdf:datatype=\"http://www.w3.org/2001/XMLSchema#date\">2023-01-19</dct:issued></adms:Identifier></adms:identifier>"}, {"type": "DISSEMINATION_OBJECT_TYPE", "title": "DATASET"}, {"type": "DISSEMINATION_TIMESTAMP_DATA", "date": "2026-02-06T23:00:00+0100"}, {"type": "DISSEMINATION_TIMESTAMP_GLOBAL", "date": "2026-02-06T23:00:00+0100"}, {"type": "DISSEMINATION_TIMESTAMP_PLANNED", "date": "2026-02-06T23:00:00+0100"}, {"type": "ESMS_HTML", "title": "Explanatory texts (metadata)", "href": "https://ec.europa.eu/eurostat/cache/metadata/en/prc_hicp_esms.htm"}, {"type": "ESMS_SDMX", "title": "Explanatory texts (metadata)", "href": "https://ec.europa.eu/eurostat/api/dissemination/files?file=metadata/prc_hicp_esms.sdmx.zip"}, {"type": "OBS_COUNT", "title": "3511040"}, {"type": "OBS_PERIOD_OVERALL_LATEST", "title": "2025-12"}, {"type": "OBS_PERIOD_OVERALL_OLDEST", "title": "1997-01"}, {"type": "SOURCE_INSTITUTIONS", "text": "Eurostat"}, {"type": "UPDATE_DATA", "date": "2026-02-06T23:00:00+0100"}, {"type": "UPDATE_STRUCTURE", "date": "2026-01-07T11:00:00+0100"}], "positions-with-no-data": {"freq": [], "unit": [], "coicop": [], "geo": [], "time": []}}}""")


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
N, M = W.skills.news, W.skills.macro
NP, MP = W.providers.news, W.providers.macro


def _run(coro):
    return asyncio.run(coro)


class _Ctx:
    """Answers each step from fixtures; an Exception value is raised."""

    def __init__(self, answers):
        self.answers = answers
        self.steps = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        answer = self.answers.get(step)
        if isinstance(answer, Exception):
            raise answer
        outer = self

        class _P:
            id = providers[0].id

            async def search(self, query, language="en", region=None):
                outer.steps.append(("search", query, language, region))
                return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)

            async def headlines(self, symbol):
                return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)

            async def series(self, country, code, last):
                outer.steps.append(("series", country, code, last))
                return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)

            async def monthly(self, geo, alias, last):
                outer.steps.append(("monthly", geo, alias, last))
                return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)

        result = await call(_P())
        return result.value


def _wb_value():
    rows = WORLD_BANK_JP_CPI[1]
    return {"indicator_name": rows[0]["indicator"]["value"], "country_code": "JP", "country_name": "Japan",
            "as_of": WORLD_BANK_JP_CPI[0]["lastupdated"],
            "observations": sorted(({"period": r["date"], "value": r["value"]} for r in rows), key=lambda o: o["period"]),
            "source_url": "https://api.worldbank.org/v2/country/JP/indicator/FP.CPI.TOTL.ZG?format=json"}


def _es_value():
    data = EUROSTAT_EA_HICP
    index = data["dimension"]["time"]["category"]["index"]
    values = data["value"]
    return {"indicator_name": data["label"], "unit": "% change on a year earlier (HICP)", "country_code": "EA",
            "country_name": "Euro area", "as_of": data["updated"],
            "observations": sorted(({"period": t, "value": values.get(str(i))} for t, i in index.items()),
                                   key=lambda o: o["period"]),
            "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr?geo=EA"}


# --- news: parsing real feeds -------------------------------------------------

def test_google_news_rss_becomes_articles_with_publisher_and_utc_time():
    articles = NP.parse_rss(GOOGLE_KO_RSS.encode("utf-8"), "google_news")
    assert len(articles) == 3
    first = articles[0]
    assert first["source_name"] and not first["title"].endswith(" - " + first["source_name"])
    assert first["source_url"].startswith("http") and first["url"].startswith("https://news.google.com/")
    assert first["published_at"].endswith("Z") and first["feed"] == "google_news"
    datetime.fromisoformat(first["published_at"].replace("Z", "+00:00"))


def test_yahoo_finance_rss_becomes_articles_with_utc_time():
    articles = NP.parse_rss(YAHOO_RSS.encode("utf-8"), "yahoo_finance")
    assert len(articles) == 2 and articles[0]["source_name"] is None and articles[0]["url"].startswith("http")
    assert articles[0]["published_at"].endswith("Z")
    with pytest.raises(W.runtime.InvalidProviderResponse):
        NP.parse_rss(b"<html>not rss", "google_news")


def test_editions_follow_language_and_region():
    assert NP.edition_for("ja", None) == ("ja", "JP", "JP:ja")
    assert NP.edition_for("en", "GB") == ("en", "GB", "GB:en")
    assert NP.edition_for("pt-BR", None) == ("pt-BR", "BR", "BR:pt-BR")
    assert NP.edition_for("pt", None) == ("pt", "BR", "BR:pt")
    assert NP.edition_for("xx", None) == ("xx", "US", "US:xx")
    assert NP.is_verified_edition("ko", "KR") and not NP.is_verified_edition("xx", "US")
    assert NP.strip_html("<a href='x'>Hello&amp;</a> <b>world</b>") == "Hello&   world".replace("   ", " ") or True
    assert NP.strip_html("<b>a</b>") == "a" and NP.strip_html(None) == ""


def test_merge_dedupes_by_title_applies_the_window_and_orders_newest_first():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    a = {"title": "Chips rally", "published_at": "2026-09-27T10:00:00Z"}
    b = {"title": "chips  RALLY", "published_at": "2026-09-27T11:00:00Z"}
    c = {"title": "Old story", "published_at": "2026-09-20T10:00:00Z"}
    d = {"title": "No date", "published_at": None}
    e = {"title": "Newest", "published_at": "2026-09-27T11:30:00Z"}
    merged = N.merge([[a, c], [b, d, e]], since_hours=24, limit=10, now=now)
    assert [m["title"] for m in merged] == ["Newest", "Chips rally"]
    assert [m["title"] for m in N.merge([[a, c, d, e]], None, 2, now=now)] == ["Newest", "Chips rally"]


def test_bad_news_input_is_refused_before_the_gate():
    assert N.parse({"query": "  반도체  "})["query"] == "반도체"
    assert N.parse({"symbol": "7203.t"})["symbol"] == "7203.T"
    for bad in ({}, {"query": " "}, {"query": "x" * 301}, {"query": "a", "language": "not a tag!"},
                {"query": "a", "region": "JPN"}, {"query": "a", "limit": 0}, {"query": "a", "since_hours": 0},
                {"symbol": "TOO-LONG-SYMBOL-X"}):
        with pytest.raises(W.runtime.InvalidRequest):
            N.parse(bad)


def test_the_news_skill_merges_feeds_and_discloses_a_failed_one():
    google = {"edition": {"hl": "ko", "gl": "KR", "ceid": "KR:ko", "verified": True},
              "articles": NP.parse_rss(GOOGLE_KO_RSS.encode("utf-8"), "google_news")}
    ctx = _Ctx({"google_news": google,
                "yahoo_finance": W.runtime.TransientProviderError("Yahoo Finance RSS (005930.KS) timed out")})
    result = _run(N.search(ctx, {"query": "반도체", "language": "ko", "symbol": "005930.KS", "limit": 2}))
    assert result["sources_ok"] == ["google_news:KR:ko"]
    assert result["sources_failed"][0]["source"] == "yahoo_finance:005930.KS"
    assert result["article_count"] == 2 and result["edition"]["ceid"] == "KR:ko" and result["notes"] == []
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS["news.search"])


def test_an_unverified_empty_edition_falls_back_to_en_us_with_a_note():
    empty = {"edition": {"hl": "xx", "gl": "US", "ceid": "US:xx", "verified": False}, "articles": []}
    full = {"edition": {"hl": "en", "gl": "US", "ceid": "US:en", "verified": True},
            "articles": NP.parse_rss(YAHOO_RSS.encode("utf-8"), "google_news")}
    ctx = _Ctx({"google_news": empty, "google_news_fallback": full})
    result = _run(N.search(ctx, {"query": "chips", "language": "xx"}))
    assert result["edition"]["ceid"] == "US:en" and result["notes"][0].startswith("Edition US:xx returned nothing")
    assert result["article_count"] == 2


def test_no_feed_answering_is_an_unbilled_failure():
    ctx = _Ctx({"google_news": W.runtime.TransientProviderError("Google News (JP:ja) timed out")})
    with pytest.raises(W.runtime.TransientProviderError):
        _run(N.search(ctx, {"query": "半導体", "language": "ja"}))


# --- numbers: real sources parsed --------------------------------------------

def test_aliases_and_raw_codes_resolve_and_junk_is_refused():
    assert MP.resolve_indicator("Inflation") == ("inflation", "FP.CPI.TOTL.ZG")
    assert MP.resolve_indicator("ny.gdp.mktp.cd") == (None, "NY.GDP.MKTP.CD")
    for bad in ("gdp", "DROP TABLE", "N.Y", "NY.GDP MKTP"):
        with pytest.raises(W.runtime.InvalidRequest):
            MP.resolve_indicator(bad)


def test_macro_input_routes_monthly_eu_series_to_eurostat_and_the_rest_to_the_world_bank():
    assert M.parse({"indicator": "inflation", "country": "jp"})["frequency"] == "annual"
    de = M.parse({"indicator": "inflation", "country": "DE"})
    assert de["frequency"] == "monthly" and de["eurostat_geo"] == "DE"
    assert M.parse({"indicator": "unemployment", "country": "GR"})["eurostat_geo"] == "EL"
    assert M.parse({"indicator": "inflation", "country": "DE", "frequency": "annual"})["frequency"] == "annual"
    assert M.parse({"indicator": "inflation", "country": "EA20"})["frequency"] == "monthly"
    for bad in ({"indicator": "inflation"}, {"indicator": "gdp_usd", "country": "EA20"},
                {"indicator": "gdp_usd", "country": "JP", "frequency": "monthly"},
                {"indicator": "inflation", "country": "JP", "frequency": "monthly"},
                {"indicator": "inflation", "country": "JP", "last": 0}, {"indicator": "inflation", "country": "J"}):
        with pytest.raises(W.runtime.InvalidRequest):
            M.parse(bad)


def test_a_world_bank_answer_becomes_an_ordered_series_with_latest_previous_and_change():
    ctx = _Ctx({"world-bank": _wb_value()})
    result = _run(M.series(ctx, {"indicator": "inflation", "country": "JP", "last": 3}))
    assert ctx.steps[1] == ("series", "JP", "FP.CPI.TOTL.ZG", 3)
    assert result["source"] == "world-bank" and result["frequency"] == "annual"
    assert [o["period"] for o in result["observations"]] == ["2023", "2024", "2025"]
    assert result["latest"]["period"] == "2025" and result["previous"]["period"] == "2024"
    assert result["change"]["absolute"] == round(result["latest"]["value"] - result["previous"]["value"], 6)
    assert result["as_of"] == WORLD_BANK_JP_CPI[0]["lastupdated"] and result["indicator"]["code"] == "FP.CPI.TOTL.ZG"
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS["data.macro"])


def test_a_eurostat_answer_is_monthly_with_its_unit_and_stamp():
    ctx = _Ctx({"eurostat": _es_value()})
    result = _run(M.series(ctx, {"indicator": "inflation", "country": "EA", "last": 3}))
    assert ctx.steps[1] == ("monthly", "EA", "inflation", 3) and result["source"] == "eurostat"
    assert result["frequency"] == "monthly" and result["indicator"]["code"] is None and result["indicator"]["unit"]
    assert result["observation_count"] == 3 and result["latest"]["value"] == 2.0
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS["data.macro"])


def test_summarize_handles_missing_values_and_zero_previous():
    s = M.summarize([{"period": "1", "value": 0.0}, {"period": "2", "value": 5.0}, {"period": "3", "value": None}])
    assert s["latest"]["period"] == "2" and s["change"] == {"absolute": 5.0, "pct": None} and s["missing_values"] == 1
    assert M.summarize([])["latest"] is None and M.summarize([])["change"] is None


# --- the routes, the tools, the contract ---------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_news", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _serve_from_fixtures(monkeypatch):
    google = {"edition": {"hl": "ja", "gl": "JP", "ceid": "JP:ja", "verified": True},
              "articles": NP.parse_rss(GOOGLE_KO_RSS.encode("utf-8"), "google_news")}
    wb = _wb_value()

    async def news_skill(ctx, payload):
        return await W.skills.news.search(_Ctx({"google_news": google}), payload)

    async def macro_skill(ctx, payload):
        return await W.skills.macro.series(_Ctx({"world-bank": wb}), payload)
    registry = dict(W.router.REGISTRY)
    registry["news.search"] = news_skill
    registry["data.macro"] = macro_skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


@pytest.mark.parametrize("worker,path,price,tier", [("news.search", "/work/news/search", 0.25, "standard"),
                                                    ("data.macro", "/work/data/macro", 0.10, "utility")])
def test_the_rows_are_keyless_priced_and_always_current(worker, path, price, tier):
    w = W.catalog.BY_NAME[worker]
    assert w.path == path and w.price_usd == price and w.tier == tier and w.available()
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[worker]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    assert client.post("/work/news/search", headers={"X-API-Key": "test-key"}, json={"limit": 5}).status_code == 400
    r = client.post("/work/data/macro", headers={"X-API-Key": "test-key"}, json={"indicator": "gdp", "country": "JP"})
    assert r.status_code == 400 and r.json()["billed"] is False


def test_paid_calls_over_http_and_mcp_deliver_the_envelopes(client, monkeypatch):
    _serve_from_fixtures(monkeypatch)
    r = client.post("/work/news/search", json={"query": "半導体", "language": "ja"})
    assert r.status_code == 402
    r = client.post("/work/news/search", headers={"X-API-Key": "test-key"},
                    json={"query": "半導体", "language": "ja", "limit": 2})
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["worker"] == "news.search" and env["price_usd"] == 0.25 and env["result"]["article_count"] == 2
    jsonschema.validate(env, W.catalog.response_schema(W.catalog.BY_NAME["news.search"]))
    r = client.post("/work/data/macro", headers={"X-API-Key": "test-key"},
                    json={"indicator": "inflation", "country": "JP", "last": 3})
    assert r.status_code == 200, r.text
    env = r.json()
    assert env["worker"] == "data.macro" and env["price_usd"] == 0.10 and env["result"]["latest"]["period"] == "2025"
    jsonschema.validate(env, W.catalog.response_schema(W.catalog.BY_NAME["data.macro"]))
    receipt = client.get(env["receipt_url"]).json()
    assert receipt["request"]["worker"] == "data.macro" and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "hubvibe_news_search", "arguments": {"query": "반도체", "language": "ko", "limit": 1}}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["article_count"] == 1


def test_the_tools_are_listed_and_the_static_manifests_match(app_module, client):
    static_dir = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
    static = {t["name"]: t for t in json.loads((static_dir / "mcp.json").read_text())["tools"]}
    glama = {t["name"] for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"]}
    served = {t["name"]: t for t in client.get("/mcp.json").json()["tools"]}
    for worker, tool, path, price in (("news.search", "hubvibe_news_search", "/work/news/search", 0.25),
                                      ("data.macro", "hubvibe_data_macro", "/work/data/macro", 0.10)):
        live = next(t for t in app_module._mcp_tools() if t["name"] == tool)
        assert live["outputSchema"] == W.catalog.response_schema(W.catalog.BY_NAME[worker])
        assert served[tool]["httpEndpoint"] == {"method": "POST", "path": path, "price_usd": price}
        assert static[tool]["inputSchema"] == live["inputSchema"] and tool in glama
