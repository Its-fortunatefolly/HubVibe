"""company.enrich -- a company profile from a domain or a name.

Pinned: the Stripe facts Wikidata, the LEI register and SEC returned on
2026-09-28 merged with a source per field; the latest dated headcount wins
at its own precision; an item that is not a company (Apple's apps share
apple.com, Toyota's marques share toyota.co.jp) is never used; a site with
no structured data still gives its self-declared name and socials; nothing
found is refused unbilled; input refused before the gate; paid HTTP + MCP;
manifests.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "company.enrich", "/work/company/enrich", "hubvibe_company_enrich"


def _claim(value, rank="normal", qualifiers=None):
    return {"rank": rank, "mainsnak": {"datavalue": {"value": value}}, "qualifiers": qualifiers or {}}


def _q(qid):
    return {"id": qid}


STRIPE = {"labels": {"en": {"value": "Stripe"}}, "descriptions": {"en": {"value": "Irish-American payment technology company"}},
          "claims": {
              "P31": [_claim(_q("Q4830453"))], "P856": [_claim("https://stripe.com/")],
              "P571": [_claim({"time": "+2010-00-00T00:00:00Z", "precision": 9})],
              "P1128": [_claim({"amount": "+2500"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2020-00-00T00:00:00Z", "precision": 9}}}]}),
                        _claim({"amount": "+8000"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2022-00-00T00:00:00Z", "precision": 9}}}]})],
              "P452": [_claim(_q("Q837171"))], "P159": [_claim(_q("Q62"))], "P17": [_claim(_q("Q30"))],
              "P1278": [_claim("549300CLHGIPTCYHQ143")], "P5531": [_claim("0001691342")],
              "P2002": [_claim("stripe")], "P4264": [_claim("stripe")], "P2037": [_claim("stripe")],
              "P112": [_claim(_q("Q7146120"))]}}
APP = {"labels": {"en": {"value": "iTunes Remote"}}, "claims": {"P31": [_claim(_q("Q166142"))], "P856": [_claim("https://apple.com/")]}}
LABELS = {"Q837171": {"label": "financial services", "iso2": None}, "Q62": {"label": "San Francisco", "iso2": None},
          "Q30": {"label": "United States", "iso2": "US"}, "Q7146120": {"label": "Patrick Collison", "iso2": None}}
LEI_REC = {"attributes": {"entity": {"legalName": {"name": "STRIPE, LLC"}, "jurisdiction": "US-DE", "status": "ACTIVE",
                                     "legalForm": {"id": "HZEH", "other": None}, "registeredAs": "4675506",
                                     "registeredAt": {"id": "RA000602"}, "creationDate": "2009-04-13T00:00:00Z",
                                     "headquartersAddress": {"addressLines": ["354 Oyster Point Boulevard"],
                                                             "city": "South San Francisco", "postalCode": "94080", "country": "US"}}}}
FILER = {"name": "Stripe, Inc.", "tickers": [], "exchanges": [], "sic": "7374", "sicDescription": "Services-Computer Processing",
         "stateOfIncorporation": "DE"}
LINEAR_HTML = ('<html><head><title>Linear – Plan and build products</title><meta property="og:site_name" content="Linear">'
               '<meta name="description" content="Linear is a purpose-built tool for planning."></head><body>'
               '<a href="https://x.com/linear">x</a><a href="https://www.linkedin.com/company/linearapp">in</a></body></html>')


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py", submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()
CO = W.skills.company


class _Ctx:
    def __init__(self, found=("Q7624104",), entities=None, lei=LEI_REC, filer=FILER, pages=None, search=()):
        self.found, self.lei, self.filer, self.search_hits = list(found), lei, filer, list(search)
        self.entities = entities if entities is not None else {"Q7624104": STRIPE}
        self.pages = pages if pages is not None else {}
        self.steps = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def by_domain(self, domain):
                return W.runtime.ProviderResult(value=outer.found)

            async def search(self, name):
                return W.runtime.ProviderResult(value=outer.search_hits)

            async def entity(self, qid):
                return W.runtime.ProviderResult(value=outer.entities.get(qid))

            async def labels(self, qids):
                return W.runtime.ProviderResult(value={q: LABELS[q] for q in qids if q in LABELS})

            async def by_lei(self, lei):
                return W.runtime.ProviderResult(value=outer.lei)

            async def legal_form(self, code):
                return W.runtime.ProviderResult(value="Limited Liability Company")

            async def submissions(self, cik):
                return W.runtime.ProviderResult(value=outer.filer)

            async def fetch(self, url, max_chars=None):
                text = outer.pages.get(url)
                return W.runtime.ProviderResult(value={"status": 200 if text else 404, "text": text, "final_url": url})
        return (await call(_P())).value


def _enrich(body, **kw):
    ctx = _Ctx(**kw)
    return asyncio.run(CO.enrich(ctx, body)), ctx


def test_stripe_merges_wikidata_the_lei_register_and_sec_with_a_source_per_field():
    out, ctx = _enrich({"domain": "https://www.stripe.com/pricing"})
    c, i = out["company"], out["identifiers"]
    assert out["query"]["domain"] == "stripe.com"
    assert c["name"] == "Stripe" and c["legal_name"] == "STRIPE, LLC" and c["founded"] == "2010"
    assert c["employees"] == {"count": 8000, "as_of": "2022", "source": "wikidata"}
    assert c["industries"] == ["financial services"] and c["headquarters"]["city"] == "San Francisco"
    assert c["headquarters"]["country_code"] == "US" and c["founders"] == ["Patrick Collison"]
    assert c["legal_form"] == "Limited Liability Company" and c["jurisdiction"] == "US-DE" and c["status"] == "ACTIVE"
    assert i["lei"] == "549300CLHGIPTCYHQ143" and i["cik"] == "1691342" and i["registration_number"] == "4675506"
    assert out["socials"]["x"] == "https://x.com/stripe" and out["socials"]["linkedin"] == "https://www.linkedin.com/company/stripe"
    assert out["field_sources"]["legal_name"] == "lei_register" and out["field_sources"]["name"] == "wikidata"
    assert "Wikidata (CC0)" in out["sources"] and "Global LEI register (CC0)" in out["sources"]
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_an_item_that_is_not_a_company_is_never_used():
    out, _ = _enrich({"domain": "apple.com"}, found=("Q_APP",), entities={"Q_APP": APP}, lei=None, filer=None,
                     pages={"https://apple.com/": "<title>Apple</title>"})
    assert out["identifiers"]["wikidata"] is None and out["company"]["name"] == "Apple"
    assert out["sources"] == ["Company website (self-declared)"]


def test_a_site_without_structured_data_gives_its_self_declared_basics():
    out, _ = _enrich({"domain": "linear.app"}, found=(), entities={}, lei=None, filer=None,
                     pages={"https://linear.app/": LINEAR_HTML})
    assert out["company"]["name"] == "Linear" and out["company"]["description"].startswith("Linear is")
    assert out["socials"]["x"] == "https://x.com/linear" and out["socials"]["linkedin"] == "https://www.linkedin.com/company/linearapp"
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_nothing_found_is_refused_and_input_is_checked():
    with pytest.raises(W.runtime.InvalidRequest):
        _enrich({"domain": "no-such-company-hv88.com"}, found=(), entities={}, lei=None, filer=None)
    for bad in ({}, {"domain": "not a domain"}, {"name": "x"}, {"domain": "localhost"}):
        with pytest.raises(W.runtime.InvalidRequest):
            CO.precheck(bad)
    assert CO.normalize_domain("HTTPS://WWW.Stripe.com/docs") == "stripe.com"


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.05 and w.tier == "utility" and w.requires == ["company_data"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None and w.available()


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_company", MAIN_PATH)
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


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"domain": "not a domain"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.company.enrich(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"domain": "stripe.com"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 50_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["result"]["identifiers"]["lei"] == "549300CLHGIPTCYHQ143"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["company"]["name"] == "Stripe"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.05}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
