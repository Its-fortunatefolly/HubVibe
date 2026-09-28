"""email.verify -- syntax, the domain's DNS and its own mail server, no message sent.

Pinned: the verdict logic on the replies real servers gave on 2026-09-28
(Microsoft 365 "250 2.1.5 Recipient OK" with the decoy refused, Gmail
"550 5.1.1 ... does not exist", Mailinator accepting everything); reply
classification (no mailbox vs a policy block vs try-later); null MX and
NXDOMAIN; the disposable list's parent-domain match; typo suggestions;
private MX addresses never dialled; input refused before the gate; paid
HTTP + MCP; manifests.
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
WORKER, PATH, TOOL = "email.verify", "/work/email/verify", "hubvibe_email_verify"

MS365_MX = {"domain_exists": True, "null_mx": False, "implicit": False,
            "mx": [{"host": "github-com.mail.protection.outlook.com", "priority": 0}]}
GMAIL_MX = {"domain_exists": True, "null_mx": False, "implicit": False,
            "mx": [{"host": "gmail-smtp-in.l.google.com", "priority": 5},
                   {"host": "alt1.gmail-smtp-in.l.google.com", "priority": 10}]}
MS365_OK = {"stage": "rcpt", "code": 250, "message": "2.1.5 Recipient OK", "decoy_code": 550,
            "host": "github-com.mail.protection.outlook.com", "tried": []}
GMAIL_NO = {"stage": "rcpt", "code": 550, "decoy_code": None, "host": "gmail-smtp-in.l.google.com", "tried": [],
            "message": ("5.1.1 The email account that you tried to reach does not exist. Please try "
                        "double-checking the recipient's email address for typos or unnecessary spaces.")}
CATCH_ALL = {"stage": "rcpt", "code": 250, "message": "Ok", "decoy_code": 250, "host": "mail.example.net", "tried": []}


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
E = W.skills.email
M = W.providers.mailcheck


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fixed_disposable_list(monkeypatch):
    async def lists():
        return {"mailinator.com", "gmial.com"}, "2026-09-28", "live"
    monkeypatch.setattr(W.providers.mailcheck, "disposable_domains", lists)


class _Ctx:
    def __init__(self, hosts=None, reply=None):
        self.hosts = hosts if hosts is not None else MS365_MX
        self.reply = reply if reply is not None else MS365_OK
        self.steps = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def mail_hosts(self, domain):
                return W.runtime.ProviderResult(value=outer.hosts, cost_micros=0, cost_measured=True)

            async def probe(self, hosts, target, decoy):
                assert decoy.endswith("@" + target.split("@")[1]) and decoy != target
                return W.runtime.ProviderResult(value=outer.reply, cost_micros=0, cost_measured=True)
        value = await call(_P())
        return value.value


def _verify(email, **kw):
    ctx = _Ctx(**kw)
    return _run(E.verify(ctx, {"email": email})), ctx


def test_a_confirmed_mailbox_on_microsoft_365_is_deliverable_and_the_role_flag_is_set():
    out, ctx = _verify("support@github.com")
    assert (out["verdict"], out["reason"]) == ("deliverable", "mailbox_exists")
    assert out["mailbox"] == {"checked": True, "exists": True, "catch_all": False, "smtp_code": 250,
                              "smtp_message": "2.1.5 Recipient OK", "mx_host": "github-com.mail.protection.outlook.com"}
    assert out["mail_provider"] == "microsoft" and out["role_account"] and not out["free_provider"]
    assert ctx.steps == ["dns", "smtp"] and any("Role address" in n for n in out["notes"])
    jsonschema.validate(out, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_gmails_5_1_1_is_undeliverable_and_a_catch_all_server_is_risky_not_deliverable():
    out, _ = _verify("hv-no-such-user-8812@gmail.com", hosts=GMAIL_MX, reply=GMAIL_NO)
    assert (out["verdict"], out["reason"]) == ("undeliverable", "mailbox_not_found")
    assert out["mailbox"]["exists"] is False and out["mail_provider"] == "google" and out["free_provider"]
    out, _ = _verify("anyone@example.net", reply=CATCH_ALL)
    assert (out["verdict"], out["reason"]) == ("risky", "accept_all_domain")
    assert out["mailbox"]["exists"] is None and out["mailbox"]["catch_all"] is True


def test_a_refusal_a_try_later_and_no_server_are_unknown_never_deliverable():
    cases = [({"stage": "rcpt", "code": 550, "message": "5.7.1 Service unavailable; client host blocked using Spamhaus",
               "decoy_code": None, "host": "mx.example.net", "tried": []}, "smtp_refused"),
             ({"stage": "rcpt", "code": 451, "message": "4.7.1 Greylisted, try again later", "decoy_code": None,
               "host": "mx.example.net", "tried": []}, "smtp_try_later"),
             ({"stage": "connect", "code": 554, "message": "No SMTP service here", "decoy_code": None,
               "host": "mx.example.net", "tried": []}, "smtp_refused"),
             ({"stage": "unreachable", "code": None, "message": None, "decoy_code": None, "host": None,
               "tried": [{"host": "mx.example.net", "outcome": "unreachable: TimeoutError"}]}, "smtp_unreachable")]
    for reply, reason in cases:
        out, _ = _verify("jane@example.net", reply=reply)
        assert (out["verdict"], out["reason"]) == ("unknown", reason), reply
        assert out["mailbox"]["checked"] is False and out["mailbox"]["exists"] is None


def test_dns_answers_end_the_job_before_any_smtp():
    out, ctx = _verify("jane@nowhere.invalid", hosts={"domain_exists": False, "null_mx": False, "implicit": False, "mx": []})
    assert (out["verdict"], out["reason"], out["domain_exists"]) == ("undeliverable", "domain_not_found", False)
    assert ctx.steps == ["dns"]
    out, ctx = _verify("jane@example.com", hosts={"domain_exists": True, "null_mx": True, "implicit": False, "mx": []})
    assert (out["verdict"], out["reason"], out["accepts_mail"]) == ("undeliverable", "domain_accepts_no_mail", False)
    assert ctx.steps == ["dns"]


def test_bad_syntax_is_a_paid_verdict_and_typos_and_disposables_are_flagged():
    out, ctx = _verify("bad..dots@example.com")
    assert (out["verdict"], out["reason"], out["syntax_valid"]) == ("undeliverable", "invalid_syntax", False)
    assert ctx.steps == [] and out["normalized"] is None
    out, _ = _verify("jane@gmial.com", reply=CATCH_ALL)
    assert out["did_you_mean"] == "jane@gmail.com" and out["disposable"] and out["verdict"] == "risky"
    assert M.is_disposable("inbox.mailinator.com", {"mailinator.com"}) and not M.is_disposable("mailinator.com.au", {"mailinator.com"})
    assert E.suggest("jane", "gmail.com") is None and E.suggest("jane", "outlok.com") == "jane@outlook.com"
    assert E.syntax("jane", "Bücher.example")[1] == "xn--bcher-kva.example"


def test_reply_classification():
    assert M.classify(250, "OK") == "accepted"
    assert M.classify(550, "5.1.1 <x@y.z>: Recipient address rejected: User unknown") == "no_mailbox"
    assert M.classify(550, "Requested action not taken: mailbox unavailable") == "no_mailbox"
    assert M.classify(554, "5.7.1 rejected because of policy") == "blocked"
    assert M.classify(421, "Too many connections") == "temporary"
    assert M.classify(None, "") == "unknown"


def test_private_mail_server_addresses_are_never_dialled(monkeypatch):
    monkeypatch.setattr(M.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 25)),
                                                                  (2, 1, 6, "", ("127.0.0.1", 25))])

    def dial(*a, **k):
        raise AssertionError("a private address was dialled")
    monkeypatch.setattr(M, "_converse", dial)
    value = _run(M.SMTP.probe(["mx.internal.example"], "a@example.com", "b@example.com")).value
    assert value["stage"] == "unreachable" and value["tried"] == [{"host": "mx.internal.example", "outcome": "no_public_address"}]


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.02 and w.tier == "utility" and w.requires == ["mailcheck"]
    assert "checked_at" in W.catalog.contract.OUTPUT_SCHEMAS[WORKER]["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None
    assert WORKER in W.catalog.LOCALIZED_WORKERS and w.available()
    for bad in ({}, {"email": "no-at-sign"}, {"email": "a@" + "b" * 330}, {"email": "a@[192.0.2.1]"}):
        with pytest.raises(W.runtime.InvalidRequest):
            E.precheck(bad)


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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_email", MAIN_PATH)
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
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json={"email": "not-an-address"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.email.verify(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    body = {"email": "support@github.com"}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 20_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.02 and envelope["result"]["verdict"] == "deliverable"
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": TOOL, "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["mail_provider"] == "microsoft"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.02}
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    tool = next(t for t in static["tools"] if t["name"] == TOOL)
    assert tool["inputSchema"] == live["inputSchema"]
    assert any(t["name"] == TOOL for t in json.loads((REPO_ROOT / "glama.json").read_text())["tools"])
