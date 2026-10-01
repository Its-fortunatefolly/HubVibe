"""news.search and data.macro -- keyless news in any language, official numbers.

Pinned: real GDELT rows read from BigQuery on 2026-09-28 (Japanese and
Korean titles stored as HTML entities, decoded; language from
TranslationInfo); the search SQL (terms entity-encoded, language filter,
partition window); merge = de-dup by title, window, newest first; a failed
search disclosed; a US ticker searched by the company's name, a non-US one
disclosed; World Bank and Eurostat payloads parsed into ordered
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

WORLD_BANK_JP_CPI = json.loads(r"""[{"page": 1, "pages": 1, "per_page": 50, "total": 9, "sourceid": "2", "lastupdated": "2026-07-13"}, [{"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2025", "value": 3.17253034260248, "unit": "", "obs_status": "", "decimal": 1}, {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2024", "value": 2.73853681635241, "unit": "", "obs_status": "", "decimal": 1}, {"indicator": {"id": "FP.CPI.TOTL.ZG", "value": "Inflation, consumer prices (annual %)"}, "country": {"id": "JP", "value": "Japan"}, "countryiso3code": "JPN", "date": "2023", "value": 3.26813365933163, "unit": "", "obs_status": "", "decimal": 1}]]""")
EUROSTAT_EA_HICP = json.loads(r"""{"version": "2.0", "class": "dataset", "label": "HICP - monthly data (annual rate of change)", "source": "ESTAT", "updated": "2026-02-06T23:00:00+0100", "value": {"0": 2.1, "1": 2.1, "2": 2.0}, "id": ["freq", "unit", "coicop", "geo", "time"], "size": [1, 1, 1, 1, 3], "dimension": {"freq": {"label": "Time frequency", "category": {"index": {"M": 0}, "label": {"M": "Monthly"}}}, "unit": {"label": "Unit of measure", "category": {"index": {"RCH_A": 0}, "label": {"RCH_A": "Annual rate of change"}}}, "coicop": {"label": "Classification of individual consumption by purpose (COICOP)", "category": {"index": {"CP00": 0}, "label": {"CP00": "All-items HICP"}}}, "geo": {"label": "Geopolitical entity (reporting)", "category": {"index": {"EA": 0}, "label": {"EA": "Euro area (EA11-1999, EA12-2001, EA13-2007, EA15-2008, EA16-2009, EA17-2011, EA18-2014, EA19-2015, EA20-2023, EA21-2026)"}}}, "time": {"label": "Time", "category": {"index": {"2025-10": 0, "2025-11": 1, "2025-12": 2}, "label": {"2025-10": "2025-10", "2025-11": "2025-11", "2025-12": "2025-12"}}}}, "extension": {"lang": "EN", "description": "<p>The Harmonised Index of Consumer Prices (HICP)\u202fmeasures price changes\u202fof\u202fvarious consumer goods and services\u202fover time\u202f(inflation).\u202fThe annual rate of change is the percentage change in one month compared to the same month of the previous year,\u202falso called annual inflation.\u202fIt is calculated from the monthly indices and rounded to one decimal place.&nbsp;</p>", "id": "PRC_HICP_MANR", "agencyId": "ESTAT", "version": "1.0", "datastructure": {"id": "PRC_HICP_MANR", "agencyId": "ESTAT", "version": "108.0"}, "annotation": [{"type": "CREATED", "date": "2015-11-16T10:18:53+0100"}, {"type": "DISSEMINATION_DOI_XML", "title": "<adms:identifier xmlns:adms=\"http://www.w3.org/ns/adms#\" xmlns:skos=\"http://www.w3.org/2004/02/skos/core.html\" xmlns:dct=\"http://purl.org/dc/terms/\" xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"><adms:Identifier rdf:about=\"https://doi.org/10.2908/PRC_HICP_MANR\"><skos:notation rdf:datatype=\"http://purl.org/spar/datacite/doi\">10.2908/PRC_HICP_MANR</skos:notation><dct:creator rdf:resource=\"http://publications.europa.eu/resource/authority/corporate-body/ESTAT\"/><dct:issued rdf:datatype=\"http://www.w3.org/2001/XMLSchema#date\">2023-01-19</dct:issued></adms:Identifier></adms:identifier>"}, {"type": "DISSEMINATION_OBJECT_TYPE", "title": "DATASET"}, {"type": "DISSEMINATION_TIMESTAMP_DATA", "date": "2026-02-06T23:00:00+0100"}, {"type": "DISSEMINATION_TIMESTAMP_GLOBAL", "date": "2026-02-06T23:00:00+0100"}, {"type": "DISSEMINATION_TIMESTAMP_PLANNED", "date": "2026-02-06T23:00:00+0100"}, {"type": "ESMS_HTML", "title": "Explanatory texts (metadata)", "href": "https://ec.europa.eu/eurostat/cache/metadata/en/prc_hicp_esms.htm"}, {"type": "ESMS_SDMX", "title": "Explanatory texts (metadata)", "href": "https://ec.europa.eu/eurostat/api/dissemination/files?file=metadata/prc_hicp_esms.sdmx.zip"}, {"type": "OBS_COUNT", "title": "3511040"}, {"type": "OBS_PERIOD_OVERALL_LATEST", "title": "2025-12"}, {"type": "OBS_PERIOD_OVERALL_OLDEST", "title": "1997-01"}, {"type": "SOURCE_INSTITUTIONS", "text": "Eurostat"}, {"type": "UPDATE_DATA", "date": "2026-02-06T23:00:00+0100"}, {"type": "UPDATE_STRUCTURE", "date": "2026-01-07T11:00:00+0100"}], "positions-with-no-data": {"freq": [], "unit": [], "coicop": [], "geo": [], "time": []}}}""")

GDELT_ROWS = [
    {"DATE": "20260928101500", "SourceCommonName": "nikkeibp.co.jp",
     "DocumentIdentifier": "https://special.nikkeibp.co.jp/atclh/ONB/26/tte_net0928/", "TranslationInfo": "srclc:jpn;eng:GT-JPN 1.0",
     "title": "&#x534A;&#x5C0E;&#x4F53;&#x5DE5;&#x5834;&#x306E;&#x5927;&#x898F;&#x6A21;&#x5316;&#x306B;&#x5BFE;&#x5FDC;"},
    {"DATE": "20260927224500", "SourceCommonName": "nikkei.com",
     "DocumentIdentifier": "https://www.nikkei.com/article/DGXZQOUB249M30U6A920C2000000/", "TranslationInfo": "srclc:jpn;eng:GT-JPN 1.0",
     "title": "&#x534A;&#x5C0E;&#x4F53;&#x5E02;&#x5834;&#x306E;&#x300C;&#x5DE8;&#x4EBA;&#x300D;"},
    {"DATE": "20260928153000", "SourceCommonName": "hankookilbo.com",
     "DocumentIdentifier": "https://www.hankookilbo.com/news/article/A2026092809290003106", "TranslationInfo": "srclc:kor;eng:GT-KOR 1.0",
     "title": "&#xBC18;&#xB3C4;&#xCCB4;&#xB294; &#xB0A0;&#xC544;&#xAC00;&#xB294;&#xB370;"},
    {"DATE": "20260928161500", "SourceCommonName": "channel3000.com",
     "DocumentIdentifier": "https://www.channel3000.com/news/us-and-china-agree-to-cut-tariffs", "TranslationInfo": None,
     "title": "US and China agree to cut tariffs on $60 billion worth of goods"},
]


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


@pytest.fixture(autouse=True)
def _bigquery_available(monkeypatch):
    """news.search reads GDELT through BigQuery; this machine has no Google
    credentials, so the node would (rightly) list it as unavailable."""
    monkeypatch.setattr(W.providers.news._Gdelt, "available", lambda self: True)


@pytest.fixture(autouse=True)
def _clock_at_capture(monkeypatch):
    """The GDELT rows were read on 2026-09-28; the skill keeps only the last
    72 hours, so the tests read them at that moment, not today."""
    _pin_clock(monkeypatch, N)


def _pin_clock(monkeypatch, news_module):
    monkeypatch.setattr(news_module, "_now_dt", lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))


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

            async def search(self, terms, since_hours, language, limit):
                outer.steps.append(("search", tuple(terms), since_hours, language))
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

def _gdelt(rows=GDELT_ROWS):
    return {"articles": [NP.article(r) for r in rows], "gib_processed": 0.1}


def test_gdelt_rows_become_articles_in_their_own_language():
    first = NP.article(GDELT_ROWS[0])
    assert first["title"] == "半導体工場の大規模化に対応" and first["language"] == "ja" and first["feed"] == "gdelt"
    assert first["published_at"] == "2026-09-28T10:15:00Z" and first["source_url"] == "https://nikkeibp.co.jp"
    assert NP.article(GDELT_ROWS[2])["language"] == "ko" and NP.article(GDELT_ROWS[3])["language"] == "en"


def test_the_search_sql_encodes_terms_like_gdelt_and_filters_language():
    assert NP.encode_term("半導体") == "&#x534a;&#x5c0e;&#x4f53;" and NP.encode_term("Tariffs") == "tariffs"
    since = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)
    sql = NP.build_sql(["半導体", "10%_off'"], since, "ja", 10)
    assert "LOWER(title) LIKE '%&#x534a;&#x5c0e;&#x4f53;%'" in sql and "\\%\\_off\\'" in sql
    assert "TranslationInfo LIKE 'srclc:jpn%'" in sql and "DATE >= 20260927160000" in sql and "LIMIT 10" in sql
    assert "TranslationInfo IS NULL" in NP.build_sql(["x"], since, "en", 5) and "AND TRUE)" in NP.build_sql(["x"], since, None, 5)


def test_merge_dedupes_by_title_applies_the_window_and_orders_newest_first():
    now = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)
    a = {"title": "Chips rally", "published_at": "2026-09-28T10:00:00Z"}
    b = {"title": "chips  rally", "published_at": "2026-09-28T11:00:00Z"}
    c = {"title": "Old", "published_at": "2026-09-20T10:00:00Z"}
    d = {"title": "Newest", "published_at": "2026-09-28T16:00:00Z"}
    merged = N.merge([[a, c], [b, d]], since_hours=24, limit=10, now=now)
    assert [m["title"] for m in merged] == ["Newest", "Chips rally"]


def test_bad_news_input_is_refused_before_the_gate():
    assert N.parse({"query": "  반도체  "})["query"] == "반도체"
    assert N.parse({"symbol": "aapl"})["symbol"] == "AAPL" and N.parse({"query": "x"})["since_hours"] == 72
    for bad in ({}, {"query": ""}, {"query": "x", "limit": 0}, {"query": "x", "since_hours": 721},
                {"symbol": "A B"}, {"query": "x", "region": "JPN"}):
        with pytest.raises(W.runtime.InvalidRequest):
            N.parse(bad)


def test_the_news_skill_searches_gdelt_with_the_language_filter_and_cites_it():
    ctx = _Ctx({"gdelt_query": _gdelt(GDELT_ROWS[:2])})
    result = _run(N.search(ctx, {"query": "半導体", "language": "ja", "limit": 5}))
    assert ("search", ("半導体",), 72, "ja") in ctx.steps
    assert result["article_count"] == 2 and result["language_filter"] == "ja"
    assert result["attribution"] == {"text": "Source: The GDELT Project", "url": "https://www.gdeltproject.org/"}
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS["news.search"])


def test_a_us_symbol_searches_the_company_name_and_a_foreign_one_is_disclosed(monkeypatch):
    async def cik_for(symbol):
        return {"cik": 320193, "title": "Apple Inc."} if symbol == "AAPL" else None
    monkeypatch.setattr(W.providers.sec_edgar.PROVIDERS[0], "cik_for", cik_for)
    ctx = _Ctx({"gdelt_symbol_AAPL": _gdelt(GDELT_ROWS[3:])})
    result = _run(N.search(ctx, {"symbol": "AAPL"}))
    assert ("search", ("Apple",), 72, None) in ctx.steps and result["sources_ok"] == ["gdelt:symbol:AAPL"]
    ctx = _Ctx({"gdelt_query": _gdelt(GDELT_ROWS[3:])})
    result = _run(N.search(ctx, {"query": "tariffs", "symbol": "7203.T"}))
    assert result["sources_failed"][0]["source"] == "gdelt:symbol:7203.T" and result["article_count"] == 1
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS["news.search"])


def test_no_search_answering_is_an_unbilled_failure():
    ctx = _Ctx({"gdelt_query": W.runtime.TransientProviderError("BigQuery did not finish")})
    with pytest.raises(W.runtime.TransientProviderError):
        _run(N.search(ctx, {"query": "半導体", "language": "ja"}))


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
    monkeypatch.setattr(W.providers.news._Gdelt, "available", lambda self: True)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _serve_from_fixtures(monkeypatch):
    # app_module may have rebound W to the workers package main.py loaded:
    # a different module object from N whenever an earlier test file cleared
    # the cached wcag_audit_engine_* modules (test_keystore_sqlite does), and
    # then the autouse clock pin above does not reach it. Pin this one too.
    _pin_clock(monkeypatch, W.skills.news)
    gdelt = _gdelt()
    wb = _wb_value()

    async def news_skill(ctx, payload):
        return await W.skills.news.search(_Ctx({"gdelt_query": gdelt}), payload)

    async def macro_skill(ctx, payload):
        return await W.skills.macro.series(_Ctx({"world-bank": wb}), payload)
    registry = dict(W.router.REGISTRY)
    registry["news.search"] = news_skill
    registry["data.macro"] = macro_skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


@pytest.mark.parametrize("worker,path,price,tier", [("news.search", "/work/news/search", 0.25, "standard"),
                                                    ("data.macro", "/work/data/macro", 0.10, "utility")])
def test_the_rows_are_priced_and_always_current(worker, path, price, tier):
    w = W.catalog.BY_NAME[worker]
    assert w.path == path and w.price_usd == price and w.tier == tier
    # data.macro is keyless; news.search reads GDELT through BigQuery, so it is
    # live exactly where Google credentials resolve (the box), which is its gate.
    assert w.requires == (["news"] if worker == "news.search" else ["macro"])
    if worker == "data.macro":
        assert w.available()
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
