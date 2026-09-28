"""sanctions.screen -- one name against the OFAC, UK and EU sanctions lists.

Pinned on the shapes the three governments published on 2026-09-21/23:
OFAC SDN.XML (namespaced, firstName/lastName, akaList), the UK list XML
(Name1..Name6, NameType), the EU FSF CSV (one row per alias/address/date).
Matching: word order free, aliases and extra words ("Al-Tikriti") found,
one-letter typos found, a different surname not; filters; a list that fails
to refresh keeps its last good copy; the disk cache round-trips; input
refused before the gate; paid HTTP + MCP; manifests.
"""

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "sanctions.screen", "/work/sanctions/screen", "hubvibe_sanctions_screen"

OFAC_XML = b"""<?xml version="1.0" standalone="yes"?>
<sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
  <publshInformation><Publish_Date>09/23/2026</Publish_Date><Record_Count>3</Record_Count></publshInformation>
  <sdnEntry><uid>7800</uid><firstName>Vladimir</firstName><lastName>PUTIN</lastName><sdnType>Individual</sdnType>
    <programList><program>RUSSIA-EO14024</program></programList>
    <akaList><aka><uid>1</uid><type>a.k.a.</type><category>strong</category><firstName>Vladimir Vladimirovich</firstName><lastName>PUTIN</lastName></aka></akaList>
    <nationalityList><nationality><uid>2</uid><country>Russia</country><mainEntry>true</mainEntry></nationality></nationalityList>
    <dateOfBirthList><dateOfBirthItem><uid>3</uid><dateOfBirth>07 Oct 1952</dateOfBirth><mainEntry>true</mainEntry></dateOfBirthItem></dateOfBirthList>
  </sdnEntry>
  <sdnEntry><uid>9000</uid><lastName>ROSNEFT</lastName><sdnType>Entity</sdnType>
    <programList><program>UKRAINE-EO13662</program></programList>
    <addressList><address><uid>4</uid><city>Moscow</city><country>Russia</country></address></addressList>
  </sdnEntry>
  <sdnEntry><uid>306</uid><lastName>BANCO NACIONAL DE CUBA</lastName><sdnType>Entity</sdnType>
    <programList><program>CUBA</program></programList>
    <akaList><aka><uid>220</uid><type>a.k.a.</type><category>strong</category><lastName>NATIONAL BANK OF CUBA</lastName></aka></akaList>
  </sdnEntry>
</sdnList>"""

UK_XML = """<?xml version="1.0" encoding="utf-8"?>
<Designations><DateGenerated>21/09/2026</DateGenerated>
  <Designation><DateDesignated>25/01/2001</DateDesignated><UniqueID>IRQ0001</UniqueID><UNReferenceNumber>IQi.001</UNReferenceNumber>
    <Names><Name><Name1>SADDAM</Name1><Name6>HUSSEIN AL-TIKRITI</Name6><NameType>Primary Name</NameType></Name></Names>
    <RegimeName>The Iraq (Sanctions) Regulations 2020</RegimeName><IndividualEntityShip>Individual</IndividualEntityShip>
    <IndividualDetails><Individual><DOBs><DOB>28/04/1937</DOB></DOBs><Nationalities><Nationality>Iraq</Nationality></Nationalities></Individual></IndividualDetails>
  </Designation>
</Designations>""".encode()

EU_CSV = ("﻿fileGenerationDate;Entity_LogicalId;Entity_EU_ReferenceNumber;Entity_UnitedNationId;Entity_DesignationDate;"
          "Entity_SubjectType_ClassificationCode;Entity_Regulation_PublicationDate;Entity_Regulation_Programme;NameAlias_WholeName;"
          "Address_CountryDescription;Citizenship_CountryDescription;BirthDate_BirthDate;BirthDate_Year\n"
          "22/09/2026;13;EU.27.28;;;person;2003-07-08;IRQ;Saddam Hussein Al-Tikriti;;;1937-04-28;1937\n"
          "22/09/2026;13;EU.27.28;;;person;2003-07-08;IRQ;Abu Ali;;IRAQ;;\n"
          "22/09/2026;99;EU.99.1;;;enterprise;2022-02-23;RUS;Joint Stock Company Sovcomflot;RUSSIAN FEDERATION;;;\n").encode()


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
S = W.providers.sanctions
SK = W.skills.sanctions


def _run(coro):
    return asyncio.run(coro)


def _loaded():
    """A fresh provider holding the three fixture lists (no network)."""
    p = S._Sanctions()
    p._loaded_disk = True
    for lid, raw in (("ofac_sdn", OFAC_XML), ("uk", UK_XML), ("eu", EU_CSV)):
        entries, as_of = S._PARSERS[lid](raw)
        p._index.lists[lid] = {"entries": entries, "as_of": as_of, "fetched": time.time()}
    p._index.lists["ofac_consolidated"] = {"entries": [], "as_of": "2026-09-14", "fetched": time.time()}
    p._index.rebuild()
    return p


class _Ctx:
    def __init__(self, provider):
        self.provider = provider

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        assert [p.id for p in providers] == ["sanctions-lists"]
        return (await call(self.provider)).value


# --- parsing -------------------------------------------------------------------------

def test_each_governments_file_parses_to_the_same_entry_shape():
    ofac, ofac_date = S.parse_ofac(OFAC_XML, "ofac_sdn")
    assert ofac_date == "2026-09-23" and [e["primary_name"] for e in ofac] == ["Vladimir PUTIN", "ROSNEFT", "BANCO NACIONAL DE CUBA"]
    putin = ofac[0]
    assert putin["type"] == "person" and putin["names"] == ["Vladimir PUTIN", "Vladimir Vladimirovich PUTIN"]
    assert putin["countries"] == ["Russia"] and putin["birth_dates"] == ["07 Oct 1952"] and putin["programs"] == ["RUSSIA-EO14024"]
    uk, uk_date = S.parse_uk(UK_XML)
    assert uk_date == "2026-09-21" and uk[0]["primary_name"] == "SADDAM HUSSEIN AL-TIKRITI"
    assert uk[0]["type"] == "person" and uk[0]["un_reference"] == "IQi.001" and uk[0]["listed_on"] == "2001-01-25"
    assert uk[0]["countries"] == ["Iraq"] and uk[0]["birth_dates"] == ["28/04/1937"]
    eu, eu_date = S.parse_eu(EU_CSV)
    assert eu_date == "2026-09-22" and len(eu) == 2
    saddam = next(e for e in eu if e["id"] == "EU.27.28")
    assert saddam["names"] == ["Saddam Hussein Al-Tikriti", "Abu Ali"] and saddam["countries"] == ["IRAQ"]
    assert saddam["birth_dates"] == ["1937-04-28"] and saddam["type"] == "person"
    assert next(e for e in eu if e["id"] == "EU.99.1")["type"] == "entity"


# --- matching ------------------------------------------------------------------------

def _names(result):
    return [(m["entry"]["list"], m["matched_name"]) for m in result["matches"]]


def test_word_order_aliases_and_extra_words_all_match():
    p = _loaded()
    hits = _run(p.screen("Saddam Hussein")).value
    assert {("uk", "SADDAM HUSSEIN AL-TIKRITI"), ("eu", "Saddam Hussein Al-Tikriti")} <= set(_names(hits))
    assert all(m["score"] >= 0.9 for m in hits["matches"])
    reordered = _run(p.screen("PUTIN, Vladimir Vladimirovich")).value
    assert reordered["matches"][0]["score"] == 1.0 and reordered["matches"][0]["entry"]["id"] == "7800"
    alias = _run(p.screen("National Bank of Cuba")).value
    assert alias["matches"][0]["entry"]["primary_name"] == "BANCO NACIONAL DE CUBA"


def test_a_typo_matches_and_a_different_surname_does_not():
    p = _loaded()
    typo = _run(p.screen("Vladimir Puttin")).value
    assert typo["matches"] and typo["matches"][0]["entry"]["id"] == "7800" and 0.85 <= typo["matches"][0]["score"] < 1
    assert _run(p.screen("Vladimir Turin")).value["matches"] == []
    assert _run(p.screen("Jane Doe")).value["matches"] == []
    assert S.similarity(S._tokens("vladimir putin"), S._tokens("vladimir turin")) < 0.85


def test_type_and_list_filters():
    p = _loaded()
    assert _run(p.screen("Rosneft", kind="person")).value["matches"] == []
    assert _run(p.screen("Rosneft", kind="entity")).value["matches"][0]["entry"]["list"] == "ofac_sdn"
    only_eu = _run(p.screen("Saddam Hussein", lists=["eu"])).value
    assert {m["entry"]["list"] for m in only_eu["matches"]} == {"eu"} and [x["list"] for x in only_eu["lists"]] == ["eu"]


# --- freshness and the disk cache ------------------------------------------------------

def test_a_list_that_fails_to_refresh_keeps_its_last_good_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("SANCTIONS_CACHE_PATH", str(tmp_path / "s.json.gz"))
    p = _loaded()
    for data in p._index.lists.values():
        data["fetched"] = 0  # everything stale

    async def fetch(client, list_id):
        return None if list_id == "uk" else S._PARSERS[list_id]({"ofac_sdn": OFAC_XML, "eu": EU_CSV}.get(list_id, OFAC_XML))
    monkeypatch.setattr(p, "_fetch", fetch)
    refreshed = _run(p.ensure_fresh())
    assert refreshed["uk"] is False and refreshed["eu"] is True
    assert p._index.lists["uk"]["as_of"] == "2026-09-21" and p._index.lists["uk"]["entries"]
    assert _run(p.screen("Saddam Hussein", lists=["uk"])).value["matches"]
    # The good lists were written to disk and a fresh process reads them back.
    q = S._Sanctions()
    q._load_disk()
    assert set(q._index.lists) == set(p._index.lists) and q._index.names


def test_no_list_at_all_is_a_transient_failure_not_an_empty_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("SANCTIONS_CACHE_PATH", str(tmp_path / "none.json.gz"))
    p = S._Sanctions()

    async def fetch(client, list_id):
        return None
    monkeypatch.setattr(p, "_fetch", fetch)
    with pytest.raises(W.runtime.TransientProviderError):
        _run(p.screen("anyone"))


# --- the skill -----------------------------------------------------------------------

def test_the_skill_filters_by_country_and_birth_year_and_says_so():
    p = _loaded()
    out = _run(SK.screen(_Ctx(p), {"name": "Saddam Hussein", "country": "Iraq", "birth_year": 1937}))
    assert out["verdict"] == "potential_match" and {m["list"] for m in out["matches"]} == {"uk", "eu"}
    assert any("birth year" in n for n in out["notes"]) and any("lead to review" in n for n in out["notes"])
    assert _run(SK.screen(_Ctx(p), {"name": "Saddam Hussein", "birth_year": 1960}))["matches"] == []
    clean = _run(SK.screen(_Ctx(p), {"name": "Jane Doe"}))
    assert clean["verdict"] == "no_match" and clean["match_count"] == 0 and len(clean["lists"]) == 4
    del p._index.lists["eu"]
    p._refresh_in_background = lambda: None  # no download in a test
    partial = _run(SK.screen(_Ctx(p), {"name": "Jane Doe"}))
    assert partial["verdict"] == "incomplete" and any("Not screened" in n for n in partial["notes"])
    jsonschema.validate(partial, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])
    jsonschema.validate(clean, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_the_row_is_priced_on_the_ladder_and_bad_input_is_refused():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.05 and w.tier == "utility" and w.requires == ["sanctions"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None and w.available()
    for bad in ({}, {"name": "x"}, {"name": "a" * 201}, {"name": "Rosneft", "type": "ship"},
                {"name": "Rosneft", "threshold": 0.2}, {"name": "Rosneft", "limit": 0},
                {"name": "Rosneft", "lists": ["un"]}, {"name": "Rosneft", "birth_year": "1952"}):
        with pytest.raises(W.runtime.InvalidRequest):
            SK.precheck(bad)


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_sanctions", MAIN_PATH)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"name": "x"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    provider = _loaded()

    async def skill(ctx, payload):
        return await W.skills.sanctions.screen(_Ctx(provider), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"name": "Rosneft", "type": "entity"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 50_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.05 and envelope["result"]["verdict"] == "potential_match"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["matches"][0]["name"] == "ROSNEFT"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.05}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])


def test_a_stale_list_is_answered_from_the_copy_held_and_refreshed_behind(monkeypatch, tmp_path):
    monkeypatch.setenv("SANCTIONS_CACHE_PATH", str(tmp_path / "s.json.gz"))
    p = _loaded()
    for data in p._index.lists.values():
        data["fetched"] = 0
    started = []

    async def slow_refresh():
        started.append(True)
    monkeypatch.setattr(p, "ensure_fresh", slow_refresh)

    async def go():
        result = (await p.screen("Rosneft")).value
        await asyncio.sleep(0)
        return result
    result = _run(go())
    assert result["matches"] and started == [True]  # answered now, refresh started, not awaited first
