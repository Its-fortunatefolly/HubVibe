"""phone.parse, ip.lookup, domain.dns and identity.check.

phone.parse runs on libphonenumber's real metadata (offline, deterministic).
ip.lookup and domain.dns are pinned on fakes of their providers shaped like
the answers DB-IP and DNS gave on 2026-09-28 (8.8.8.8 -> Mountain View,
AS15169 Google; hubvibe-io.com -> Hostinger MX, no DMARC). identity.check is
pinned on its flag and risk rules, partial failure and input refusal, then a
paid call over HTTP and MCP; the four tools are in the static manifests.
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
TOOLS = {"phone.parse": ("/work/phone/parse", 0.02), "ip.lookup": ("/work/ip/lookup", 0.02),
         "domain.dns": ("/work/domain/dns", 0.05), "identity.check": ("/work/identity/check", 0.10)}


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


class _Ctx:
    """Runs the step against the fake passed in, or the real provider."""

    def __init__(self, fake=None):
        self.fake = fake

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        return (await call(self.fake or providers[0])).value


def _schema(name):
    return W.catalog.contract.OUTPUT_SCHEMAS[name]


# --- phone.parse ------------------------------------------------------------------------

def test_phone_numbers_are_described_from_the_numbering_plan():
    out = asyncio.run(W.skills.osint.phone_parse(_Ctx(), {"number": "+44 20 7031 3000"}))
    assert out["valid"] and out["e164"] == "+442070313000" and out["region"] == "GB"
    assert out["line_type"] == "fixed_line" and out["country"] == "United Kingdom"
    us = asyncio.run(W.skills.osint.phone_parse(_Ctx(), {"number": "(415) 555-2671", "region": "us"}))
    assert us["e164"] == "+14155552671" and us["input"]["region"] == "US"
    jsonschema.validate(out, _schema("phone.parse"))
    for bad in ({}, {"number": "call me"}, {"number": "12345", "region": "USA"}, {"number": "x" * 41}):
        with pytest.raises(W.runtime.InvalidRequest):
            W.skills.osint.parse_phone(bad)


# --- ip.lookup ---------------------------------------------------------------------------

class _FakeDbIp:
    async def locate(self, address):
        return W.runtime.ProviderResult(value={
            "location": {"city": "Mountain View", "region": "California", "country": "United States",
                         "country_code": "US", "continent": "North America", "latitude": 37.422, "longitude": -122.085},
            "network": {"asn": 15169, "organization": "Google LLC"},
            "database_month": "2026-09", "asn_database_month": "2026-09"}, cost_micros=0, cost_measured=True)


def test_a_public_ip_is_located_and_credited(monkeypatch):
    async def rdns(address):
        return ["dns.google"]
    monkeypatch.setattr(W.providers.iplookup, "reverse_dns", rdns)
    out = asyncio.run(W.skills.osint.ip_lookup(_Ctx(_FakeDbIp()), {"ip": "8.8.8.8"}))
    assert out["scope"] == "public" and out["location"]["city"] == "Mountain View"
    assert out["network"]["asn"] == 15169 and out["reverse_dns"] == ["dns.google"]
    assert out["attribution"]["text"] == "IP Geolocation by DB-IP"
    jsonschema.validate(out, _schema("ip.lookup"))


def test_a_private_ip_is_answered_without_any_lookup():
    class _Refuse:
        async def locate(self, address):
            raise AssertionError("a private address was looked up")
    out = asyncio.run(W.skills.osint.ip_lookup(_Ctx(_Refuse()), {"ip": "10.0.0.1"}))
    assert out["scope"] == "private" and out["location"] is None and "not routed" in out["notes"][0]
    jsonschema.validate(out, _schema("ip.lookup"))
    for bad in ({}, {"ip": ""}, {"ip": "999.1.1.1"}, {"ip": "example.com"}):
        with pytest.raises(W.runtime.InvalidRequest):
            W.skills.osint.parse_ip(bad)


# --- domain.dns ----------------------------------------------------------------------------

class _FakeDns:
    async def inspect(self, domain_ascii, check_tls=True):
        records = {"a": ["2.25.172.160"], "aaaa": [], "cname": [], "mx": ["5 mx1.hostinger.com", "10 mx2.hostinger.com"],
                   "ns": ["byte.dns-parking.com"], "txt": ["v=spf1 include:_spf.mail.hostinger.com ~all"],
                   "caa": [], "soa": ["byte.dns-parking.com. dns.hostinger.com. 2026092801 10800 3600 604800 3600"]}
        return W.runtime.ProviderResult(value={
            "exists": True, "records": records, "dmarc_records": [], "lookup_errors": [],
            "tls": {"reachable": True, "valid": True, "error": None, "ip": "2.25.172.160", "protocol": "TLSv1.3",
                    "subject": "hubvibe-io.com", "issuer": "Let's Encrypt", "names": ["hubvibe-io.com"],
                    "not_before": "2026-08-01T00:00:00Z", "not_after": "2026-10-05T00:00:00Z", "days_remaining": 7}},
            cost_micros=0, cost_measured=True)


def test_a_domain_is_read_with_its_mail_policy_and_certificate():
    out = asyncio.run(W.skills.osint.domain_dns(_Ctx(_FakeDns()), {"domain": "https://HubVibe-io.com/pricing"}))
    assert out["domain"] == "hubvibe-io.com" and out["email_security"]["spf"]["all"] == "softfail"
    assert out["email_security"]["dmarc"] is None and any("No DMARC" in n for n in out["notes"])
    assert any("expires in 7 days" in n for n in out["notes"])
    jsonschema.validate(out, _schema("domain.dns"))
    for bad in ({}, {"domain": "localhost"}, {"domain": "8.8.8.8"}, {"domain": "-bad-.com"}, {"domain": "x.com", "tls": "yes"}):
        with pytest.raises(W.runtime.InvalidRequest):
            W.skills.osint.parse_domain(bad)


# --- identity.check ----------------------------------------------------------------------------

SANCTIONS_HIT = {"verdict": "potential_match", "match_count": 1, "lists": [],
                 "matches": [{"name": "ROSNEFT", "list_name": "OFAC SDN", "score": 1.0}]}
EMAIL_DISPOSABLE = {"normalized": "a@mailinator.com", "verdict": "risky", "reason": "disposable_domain",
                    "disposable": True, "role_account": False, "free_provider": False, "did_you_mean": None,
                    "accepts_mail": True, "mail_provider": None}
PHONE_US = {"valid": True, "e164": "+14155552671", "country": "United States", "region": "US", "location": "San Francisco, CA",
            "carrier": None, "line_type": "fixed_line_or_mobile"}
IP_DE = {"scope": "public", "location": {"country": "Germany", "country_code": "DE"}, "network": None,
         "reverse_dns": [], "attribution": {"text": "IP Geolocation by DB-IP"}}


def _stub(monkeypatch, sanctions=None, email=None, phone=None, ip=None):
    def make(value):
        async def fn(ctx, payload):
            if isinstance(value, Exception):
                raise value
            return value
        return fn
    for module, attr, value in ((W.skills.sanctions, "screen", sanctions), (W.skills.email, "verify", email),
                                (W.skills.osint, "phone_parse", phone), (W.skills.osint, "ip_lookup", ip)):
        if value is not None:
            monkeypatch.setattr(module, attr, make(value))


def test_identity_flags_and_risk(monkeypatch):
    _stub(monkeypatch, sanctions=SANCTIONS_HIT, email=EMAIL_DISPOSABLE, phone=PHONE_US, ip=IP_DE)
    out = asyncio.run(W.skills.identity.check(_Ctx(), {"name": "Rosneft", "email": "a@mailinator.com",
                                                        "phone": "+1 415 555 2671", "ip": "8.8.8.8", "country": "US"}))
    codes = [f["code"] for f in out["flags"]]
    assert out["risk"] == "high" and codes[0] == "sanctions_potential_match"
    assert "email_disposable" in codes and "country_mismatch_ip" in codes and "country_mismatch_phone_ip" in codes
    assert "country_mismatch_phone" not in codes and out["checks_run"] == ["sanctions", "email", "phone", "ip"]
    jsonschema.validate(out, _schema("identity.check"))


def test_identity_ships_what_completed_and_fails_only_when_nothing_did(monkeypatch):
    _stub(monkeypatch, email=W.runtime.TransientProviderError("SMTP timed out"), phone=PHONE_US)
    out = asyncio.run(W.skills.identity.check(_Ctx(), {"email": "a@b.com", "phone": "+1 415 555 2671"}))
    assert out["checks_failed"] == ["email"] and out["checks"]["email"] is None and out["risk"] == "low"
    jsonschema.validate(out, _schema("identity.check"))
    _stub(monkeypatch, email=W.runtime.TransientProviderError("SMTP timed out"))
    with pytest.raises(W.runtime.TransientProviderError):
        asyncio.run(W.skills.identity.check(_Ctx(), {"email": "a@b.com"}))


def test_identity_input_is_checked_before_the_gate():
    for bad in ({}, {"country": "US"}, {"name": "x"}, {"email": "not-an-email"}, {"phone": "call me"},
                {"ip": "999.1.1.1"}, {"email": "a@b.com", "birth_year": 1980}, {"email": "a@b.com", "phone_region": "US"}):
        with pytest.raises(W.runtime.InvalidRequest):
            W.skills.identity.precheck(bad)
    W.skills.identity.precheck({"name": "Jane Smith", "birth_year": 1980, "phone": "020 7031 3000", "phone_region": "GB"})


def test_the_rows_are_priced_listed_and_always_current():
    for name, (path, price) in TOOLS.items():
        w = W.catalog.BY_NAME[name]
        assert w.path == path and w.price_usd == price and w.tier == "utility", name
        assert "checked_at" in _schema(name)["required"] and len(w.description) <= 500
        jsonschema.validate(W.catalog.example_for(w), w.input_schema)
        assert W.skills.PRECHECKS.get(w.skill) is not None and W.catalog.contract.REPRESENTATIVE_QUERIES[name]


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_osint", MAIN_PATH)
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


def test_identity_bad_input_over_http_is_a_free_400(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post("/work/identity/check", headers={"X-API-Key": "test-key"}, json={"country": "US"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_identity_check_over_http_and_mcp(client, monkeypatch):
    _stub(monkeypatch, sanctions=SANCTIONS_HIT, ip=IP_DE)
    body = {"name": "Rosneft", "ip": "8.8.8.8"}
    response = client.post("/work/identity/check", json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 100_000
    response = client.post("/work/identity/check", headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == "identity.check" and envelope["result"]["risk"] == "high"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME["identity.check"]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "hubvibe_identity_check", "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["checks_run"] == ["sanctions", "ip"]


def test_the_tools_are_listed_and_the_static_manifests_match(app_module, client):
    served = {t["name"]: t for t in client.get("/mcp.json").json()["tools"]}
    static = {t["name"]: t for t in json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())["tools"]}
    glama = {t["name"] for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"]}
    live = {t["name"]: t for t in app_module._mcp_tools()}
    for name, (path, price) in TOOLS.items():
        tool = "hubvibe_" + name.replace(".", "_")
        assert served[tool]["httpEndpoint"] == {"method": "POST", "path": path, "price_usd": price}
        assert static[tool]["inputSchema"] == live[tool]["inputSchema"] and tool in glama
