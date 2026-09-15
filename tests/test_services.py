"""The machine-service catalog: gated like a rail, paid like an audit.

Everything the audits promise, the services must keep: advertised only when
they can actually run, priced from one catalog on every surface, validated
for free before any payment is read, never billed for a call that produced
no result, and delivered through the same receipt path. Plus the parts that
are theirs alone: provider retry/fallback/backoff, the circuit breaker, the
idempotency cache, the sandbox, and the usage/margin ledger.

No test here reaches a real provider, a real facilitator, or the network:
provider adapters are exercised against stubbed httpx responses, and the
engine against stub chains.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "wcag-audit-engine" / "app"
MAIN_PATH = APP_DIR / "main.py"

_PROVIDER_KEY_VARS = (
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
    "BRAVE_SEARCH_API_KEY", "SERPER_API_KEY",
)
_SVC_VARS = ("SVC_DISABLED", "SVC_CODE_EXEC", "SVC_LEDGER_PATH", "SVC_RPC_URLS")


def _forget_service_modules():
    for name in list(sys.modules):
        if name.startswith("wcag_audit_engine_"):
            sys.modules.pop(name)


def _clean_env(monkeypatch, tmp_path=None):
    for var in _PROVIDER_KEY_VARS + _SVC_VARS + (
        "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PRO",
        "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "KEY_STORE",
        "KEY_STORE_SQLITE_PATH", "AUDIT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    if tmp_path is not None:
        monkeypatch.setenv("SVC_LEDGER_PATH", str(tmp_path / "services.db"))


def _load_module(name: str, unique: str):
    _forget_service_modules()
    spec = importlib.util.spec_from_file_location(unique, APP_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_services(monkeypatch, tmp_path=None, **env):
    _clean_env(monkeypatch, tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = _load_module("services", "svc_under_test")
    # Retry backoff sleeps are pure waste in a test.
    monkeypatch.setattr(module, "_sleep", lambda *_: None)
    return module


def _load_main(monkeypatch, tmp_path=None, api_key=None, **env):
    _clean_env(monkeypatch, tmp_path)
    if api_key is not None:
        monkeypatch.setenv("AUDIT_API_KEY", api_key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module = _load_module("main", "svc_main_under_test")
    _forget_service_modules()
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda v, n: True)
    monkeypatch.setattr(module.services, "_sleep", lambda *_: None)
    return module


class _StubResponse:
    def __init__(self, status_code=200, payload=None, text=None, content=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif text is not None:
            self.content = text.encode("utf-8")
        else:
            self.content = json.dumps(payload or {}).encode("utf-8")
        self.text = text if text is not None else self.content.decode("utf-8", "replace")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# --------------------------------------------------------------------------
# Catalog gating: absent providers mean absent capabilities, everywhere.
# --------------------------------------------------------------------------


def test_keyless_services_are_live_and_key_gated_ones_are_absent_by_default(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    live = {spec["id"] for spec in svc.catalog()}
    assert live == {"fetch", "extract", "rpc", "market", "prediction"}, (
        "with no provider keys, exactly the self-contained/public-API services may sell"
    )
    for path in ("/svc/llm", "/svc/search", "/svc/image", "/svc/tts", "/svc/code",
                 "/svc/research"):
        assert svc.price_of_path(path) is None, f"{path} priced while unconfigured"


def test_a_provider_key_turns_its_capabilities_on(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path, ANTHROPIC_API_KEY="k",
                         BRAVE_SEARCH_API_KEY="k")
    live = {spec["id"] for spec in svc.catalog()}
    assert {"llm", "search", "research"} <= live
    assert "image" not in live, "Anthropic key must not advertise image generation"
    tool_names = {tool["name"] for tool in svc.mcp_tools()}
    assert {"llm_generate", "web_search", "web_research"} <= tool_names


def test_svc_disabled_switches_everything_off(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path, SVC_DISABLED="all",
                         ANTHROPIC_API_KEY="k")
    assert svc.catalog() == []
    assert svc.mcp_tools() == []
    assert svc.price_of_path("/svc/fetch") is None


def test_svc_disabled_can_switch_off_one_service(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path, SVC_DISABLED="market")
    live = {spec["id"] for spec in svc.catalog()}
    assert "market" not in live and "rpc" in live


def test_with_services_disabled_the_audit_surface_is_exactly_what_it_was(monkeypatch, tmp_path):
    """The rollback switch: SVC_DISABLED=all must leave every advertised
    surface byte-for-byte the audit product, so the addition can be turned
    off in production without a deploy."""
    module = _load_main(monkeypatch, tmp_path, SVC_DISABLED="all")
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()
    assert [e["path"] for e in manifest["endpoints"]] == [
        "/audit/wcag", "/audit/seo", "/audit/security", "/audit/performance",
        "/audit/bundle", "/audit",
    ]
    tools = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"
    }
    assert {t["name"] for t in client.get("/mcp.json").json()["tools"]} == {
        "audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"
    }


# --------------------------------------------------------------------------
# Free refusal before payment.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path,body,fragment", [
    ("/svc/rpc", {"method": "eth_sendRawTransaction"}, "not offered"),
    ("/svc/rpc", {"method": "eth_call", "params": "nope"}, "must be an array"),
    ("/svc/market", {"op": "margin-trade"}, "'op' must be one of"),
    ("/svc/market", {"op": "spot", "pair": "btc/usd"}, "BTC-USD"),
    ("/svc/prediction", {"op": "market"}, "slug"),
    ("/svc/fetch", {"url": "http://127.0.0.1/"}, "private"),
    ("/svc/fetch", {"url": "ftp://example.com/"}, "http"),
    ("/svc/extract", {}, "'url' is required"),
])
def test_invalid_requests_are_refused_for_free(monkeypatch, tmp_path, path, body, fragment):
    module = _load_main(monkeypatch, tmp_path, api_key="test-key")
    client = TestClient(module.app)
    response = client.post(path, json=body, headers={"X-API-Key": "test-key"})
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["billed"] is False
    assert "Nothing was charged" in payload["detail"]
    assert fragment in payload["detail"]


def test_unpaid_probes_on_service_routes_are_priced_not_executed(monkeypatch, tmp_path):
    """Same contract as the audits' probe test: a credential-less request on
    any live service path gets that path's 402, whatever else is wrong with
    it, and no provider is ever called for it."""
    module = _load_main(monkeypatch, tmp_path)
    called = []
    monkeypatch.setattr(
        module.services, "execute_route",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("ran")),
    )
    client = TestClient(module.app)
    for spec in module.services.catalog():
        path = spec["path"]
        for method, kwargs in (("POST", {"json": {}}), ("GET", {}), ("HEAD", {})):
            response = client.request(method, path, **kwargs)
            assert response.status_code == 402, (method, path, response.status_code)
            if method != "HEAD":
                assert response.json()["price_usd"] == spec["price_usd"], (method, path)
            if method != "POST":
                assert response.headers["allow"] == "POST"
    assert called == []


def test_every_service_402_carries_bazaar_discovery(monkeypatch, tmp_path):
    """The audits' invisible-but-payable guard, applied to /svc. Route list
    from app.routes, not the catalog, so a route missing its discovery data
    cannot hide by also being missing from the catalog."""
    module = _load_main(
        monkeypatch, tmp_path,
        X402_FACILITATOR_URL="https://facilitator.example",
        X402_PAY_TO_ADDRESS="0x1111111111111111111111111111111111111111",
    )
    client = TestClient(module.app)
    served = sorted({
        route.path
        for route in module.app.routes
        if "POST" in getattr(route, "methods", set()) and route.path.startswith("/svc")
    })
    assert served, "no service routes found -- this guard is checking nothing"
    missing = []
    for path in served:
        body = client.post(path, json={}).json()
        if "bazaar" not in body.get("extensions", {}):
            missing.append(path)
    assert not missing, f"service routes without Bazaar discovery: {missing}"


# --------------------------------------------------------------------------
# The engine: retry, fallback, breaker, deadline, response validation.
# --------------------------------------------------------------------------


def _stub_spec(svc, chain_fn, check=None):
    return {
        "id": "stub", "path": "/svc/stub", "price_usd": 0.01,
        "chain": chain_fn, "check_result": check, "deadline_seconds": 30,
    }


def test_transient_failures_are_retried_with_backoff_then_succeed(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    delays = []
    monkeypatch.setattr(svc, "_sleep", lambda s: delays.append(s))
    attempts = []

    def flaky(args, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise svc.ProviderError("timed out", transient=True)
        return {"answer": 42}

    chain = lambda args: [svc._Provider("p1", flaky, timeout=5.0, attempts=2)]
    result, provider, cost = svc._run_chain(_stub_spec(svc, chain), {}, time.time() + 30)
    assert result == {"answer": 42} and provider == "p1"
    assert len(attempts) == 2
    assert len(delays) == 1 and 0 < delays[0] <= 1.0, "one backoff sleep between attempts"


def test_permanent_failure_falls_through_to_the_next_provider(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    calls = []

    def dead(args, timeout):
        calls.append("dead")
        raise svc.ProviderError("invalid api key")  # permanent: no retry

    def alive(args, timeout):
        calls.append("alive")
        return {"answer": "ok"}

    chain = lambda args: [
        svc._Provider("dead", dead, timeout=5.0, attempts=3),
        svc._Provider("alive", alive, timeout=5.0, attempts=1),
    ]
    result, provider, _ = svc._run_chain(_stub_spec(svc, chain), {}, time.time() + 30)
    assert provider == "alive"
    assert calls == ["dead", "alive"], "a permanent refusal must not be retried"


def test_all_providers_failing_names_every_reason(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)

    def a(args, timeout):
        raise svc.ProviderError("upstream answered HTTP 500", transient=True)

    def b(args, timeout):
        raise svc.ProviderError("invalid api key")

    chain = lambda args: [
        svc._Provider("a", a, timeout=5.0, attempts=1),
        svc._Provider("b", b, timeout=5.0, attempts=1),
    ]
    with pytest.raises(svc.ServiceFailure) as excinfo:
        svc._run_chain(_stub_spec(svc, chain), {}, time.time() + 30)
    assert "a: upstream answered HTTP 500" in excinfo.value.detail
    assert "b: invalid api key" in excinfo.value.detail


def test_invalid_provider_response_is_a_failure_not_a_delivery(monkeypatch, tmp_path):
    """A provider that answers with the wrong shape must never be billed as
    a result -- the response contract is part of what is being sold."""
    svc = _load_services(monkeypatch, tmp_path)
    chain = lambda args: [
        svc._Provider("bad", lambda a, t: {"wrong": True}, timeout=5.0, attempts=1),
        svc._Provider("good", lambda a, t: {"answer": 1}, timeout=5.0, attempts=1),
    ]
    spec = _stub_spec(svc, chain, check=svc._require_keys("answer"))
    result, provider, _ = svc._run_chain(spec, {}, time.time() + 30)
    assert provider == "good"


def test_circuit_breaker_opens_after_consecutive_failures_and_recovers(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    breaker = svc._CircuitBreaker()
    for _ in range(3):
        breaker.record_failure("vendor")
    assert breaker.is_open("vendor")
    # After the cooldown it half-opens and a success closes it fully.
    future = time.time() + 61
    assert not breaker.is_open("vendor", now=future)
    breaker.record_success("vendor")
    assert breaker.state("vendor") == "closed"


def test_open_circuit_skips_the_provider_but_never_manufactures_an_outage(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    for _ in range(3):
        svc._breaker.record_failure("only")
    chain = lambda args: [svc._Provider("only", lambda a, t: {"v": 1}, timeout=5.0, attempts=1)]
    # 'only' is tripped AND the only provider: it must still be tried.
    result, provider, _ = svc._run_chain(_stub_spec(svc, chain), {}, time.time() + 30)
    assert provider == "only"
    svc._breaker.record_success("only")


def test_deadline_exhaustion_fails_closed_with_a_reason(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    chain = lambda args: [svc._Provider("slow", lambda a, t: {"v": 1}, timeout=5.0, attempts=1)]
    with pytest.raises(svc.ServiceFailure) as excinfo:
        svc._run_chain(_stub_spec(svc, chain), {}, time.time() - 1)
    assert "deadline" in excinfo.value.detail


# --------------------------------------------------------------------------
# Ledger: the margin numbers are recorded, finalized, and summarized.
# --------------------------------------------------------------------------


def test_ledger_records_execution_and_billing_outcome(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    monkeypatch.setattr(
        svc, "_market_chain",
        lambda args: [svc._Provider("market:stub",
                                    lambda a, t: {"op": "spot", "amount": "1",
                                                  "cost_microusd": 120},
                                    timeout=5.0, attempts=1)],
    )
    svc._SPEC_BY_PATH["/svc/market"]["chain"] = svc._market_chain
    result, call_id = svc.execute_route("/svc/market", {"op": "spot", "pair": "BTC-USD"})
    assert result["status"] == "ok" and result["provider"] == "market:stub"
    assert call_id is not None
    svc.finalize_call(call_id, "prepaid", billed=True)

    summary = svc.metrics_summary()
    market = summary["services"]["market"]
    assert market["calls"] == 1 and market["ok"] == 1
    assert market["revenue_usd"] == 0.01
    assert market["est_provider_cost_usd"] == pytest.approx(0.00012)
    # The summary rounds margins to whole hundredths of a cent.
    assert market["est_margin_usd"] == round(0.01 - 0.00012, 4)
    assert summary["providers"]["market:stub"]["calls"] == 1


def test_failed_calls_are_ledgered_as_failures_with_no_revenue(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    dead = lambda args: [svc._Provider(
        "market:dead",
        lambda a, t: (_ for _ in ()).throw(svc.ProviderError("down", transient=False)),
        timeout=5.0, attempts=1)]
    svc._SPEC_BY_PATH["/svc/market"]["chain"] = dead
    with pytest.raises(svc.ServiceFailure) as excinfo:
        svc.execute_route("/svc/market", {"op": "spot", "pair": "BTC-USD"})
    svc.finalize_call(excinfo.value.call_id, "prepaid", billed=False)
    summary = svc.metrics_summary()
    market = summary["services"]["market"]
    assert market["failed"] == 1 and market["revenue_usd"] == 0
    assert market["success_rate"] == 0


def test_a_broken_ledger_never_breaks_a_paid_call(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    monkeypatch.setenv("SVC_LEDGER_PATH", str(tmp_path / "no-such-dir" / "x.db"))
    svc._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        svc._Provider("m", lambda a, t: {"op": "spot", "amount": "1"}, timeout=5.0, attempts=1)
    ]
    result, call_id = svc.execute_route("/svc/market", {"op": "spot", "pair": "BTC-USD"})
    assert result["status"] == "ok"
    assert call_id is None
    svc.finalize_call(call_id, "prepaid", billed=True)  # must not raise
    assert "error" in svc.metrics_summary()


# --------------------------------------------------------------------------
# Idempotency: keyed callers can retry without re-buying.
# --------------------------------------------------------------------------


def test_idempotency_replays_only_for_the_same_key_scope(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    svc.idempotent_store("key-a", "req-1", "/svc/market", {"status": "ok", "amount": "5"})
    replay = svc.idempotent_replay("key-a", "req-1", "/svc/market")
    assert replay and replay["idempotent_replay"] is True and replay["amount"] == "5"
    assert svc.idempotent_replay("key-b", "req-1", "/svc/market") is None
    assert svc.idempotent_replay("key-a", "req-1", "/svc/rpc") is None
    assert svc.idempotent_replay(None, "req-1", "/svc/market") is None
    assert svc.idempotent_replay("key-a", None, "/svc/market") is None


def test_idempotent_retry_over_http_returns_the_first_delivery_without_recharging(
    monkeypatch, tmp_path
):
    module = _load_main(monkeypatch, tmp_path, api_key=None,
                        KEY_STORE="sqlite",
                        KEY_STORE_SQLITE_PATH=str(tmp_path / "keys.db"),
                        STRIPE_SECRET_KEY="sk_test_svc", STRIPE_WEBHOOK_SECRET="whsec_svc",
                        STRIPE_PRICE_PRO="price_svc")
    calls = []
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider(
            "m", lambda a, t: calls.append(1) or {"op": "spot", "amount": "9"},
            timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    key = module.billing.issue_prepaid_key(2)  # two calls' worth at $0.01

    headers = {"X-API-Key": key, "X-Idempotency-Key": "job-7"}
    first = client.post("/svc/market", json={"op": "spot", "pair": "BTC-USD"}, headers=headers)
    assert first.status_code == 200, first.text
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 1

    second = client.post("/svc/market", json={"op": "spot", "pair": "BTC-USD"}, headers=headers)
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["amount"] == "9"
    assert len(calls) == 1, "the provider must not run twice for one idempotency key"
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 1, (
        "the replay must not spend the key again"
    )


# --------------------------------------------------------------------------
# Payment integration on the REST routes: the audits' guarantees, kept.
# --------------------------------------------------------------------------


def _sqlite_main(monkeypatch, tmp_path):
    return _load_main(
        monkeypatch, tmp_path, api_key=None,
        KEY_STORE="sqlite", KEY_STORE_SQLITE_PATH=str(tmp_path / "keys.db"),
        STRIPE_SECRET_KEY="sk_test_svc", STRIPE_WEBHOOK_SECRET="whsec_svc",
        STRIPE_PRICE_PRO="price_svc",
    )


def test_a_prepaid_key_is_refunded_when_no_provider_could_answer(monkeypatch, tmp_path):
    module = _sqlite_main(monkeypatch, tmp_path)
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider(
            "market:dead",
            lambda a, t: (_ for _ in ()).throw(
                module.services.ProviderError("upstream answered HTTP 503", transient=True)
            ),
            timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    key = module.billing.issue_prepaid_key(1)
    response = client.post(
        "/svc/market", json={"op": "spot", "pair": "BTC-USD"}, headers={"X-API-Key": key}
    )
    assert response.status_code == 502, response.text
    body = response.json()
    assert body["billed"] is False
    assert "Nothing was charged" in body["detail"]
    assert body["failed_providers"] == ["market:dead: upstream answered HTTP 503"]
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 1, (
        "the debit taken at authentication was not handed back"
    )


def test_a_successful_service_call_spends_exactly_its_price(monkeypatch, tmp_path):
    module = _sqlite_main(monkeypatch, tmp_path)
    module.services._SPEC_BY_PATH["/svc/rpc"]["chain"] = lambda args: [
        module.services._Provider(
            "rpc:stub", lambda a, t: {"network": "base", "method": a["method"],
                                      "result": "0x1"},
            timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    key = module.billing.issue_prepaid_key(5)
    response = client.post(
        "/svc/rpc", json={"method": "eth_blockNumber"}, headers={"X-API-Key": key}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok" and body["result"] == "0x1"
    assert body["provider"] == "rpc:stub"
    assert module.billing.lookup_key(key)["prepaid_balance_cents"] == 4


class _FakePending:
    def __init__(self):
        self.settle_state = None
        self.settle_error = None
        self.price = "$0.01"


def test_x402_settlement_refused_withholds_the_service_result(monkeypatch, tmp_path):
    """The audits' rule at the service routes: verify grants access, the
    work runs, and a settle the facilitator refuses withholds the result
    and re-issues the payable 402 -- the work is never given away."""
    module = _load_main(monkeypatch, tmp_path)
    pending = _FakePending()
    monkeypatch.setattr(module.x402_payments, "verify_only_sync",
                        lambda *a, **k: pending)

    def refuse(p):
        p.settle_state = "refused"
        p.settle_error = "payer balance moved"
        return False

    monkeypatch.setattr(module.x402_payments, "settle_sync", refuse)
    ran = []
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider(
            "m", lambda a, t: ran.append(1) or {"op": "spot", "amount": "3"},
            timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    response = client.post(
        "/svc/market", json={"op": "spot", "pair": "BTC-USD"},
        headers={"X-PAYMENT": "sig"},
    )
    assert ran == [1], "the work should have run before settlement"
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["error"] == "settlement_refused"
    assert "amount" not in body, "the refused call must not leak the result"


def test_x402_settled_service_call_delivers_with_result_intact(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path)
    pending = _FakePending()
    monkeypatch.setattr(module.x402_payments, "verify_only_sync", lambda *a, **k: pending)
    monkeypatch.setattr(module.x402_payments, "settle_sync", lambda p: True)
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider("m", lambda a, t: {"op": "spot", "amount": "3"},
                                  timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    response = client.post(
        "/svc/market", json={"op": "spot", "pair": "BTC-USD"}, headers={"X-PAYMENT": "sig"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["amount"] == "3"


# --------------------------------------------------------------------------
# MCP transport: service tools, paid and unpaid.
# --------------------------------------------------------------------------


def test_mcp_service_tool_unpaid_call_returns_the_priced_challenge(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path)
    client = TestClient(module.app)
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "market_data", "arguments": {"op": "spot", "pair": "BTC-USD"}},
    })
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["price_usd"] == 0.01


def test_mcp_service_tool_paid_call_returns_structured_content(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, api_key="internal-key")
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider("m", lambda a, t: {"op": "spot", "amount": "7"},
                                  timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "market_data", "arguments": {"op": "spot", "pair": "BTC-USD"}},
    }, headers={"X-API-Key": "internal-key"})
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["amount"] == "7"
    assert "_ledger_call_id" not in result["structuredContent"], (
        "internal ledger plumbing must not leak to the payer"
    )


def test_mcp_service_tool_validation_failure_is_free_and_named(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, api_key="internal-key")
    client = TestClient(module.app)
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "chain_rpc", "arguments": {"method": "eth_sendRawTransaction"}},
    }, headers={"X-API-Key": "internal-key"})
    result = response.json()["result"]
    assert result["isError"] is True
    assert "not offered" in result["content"][0]["text"]


def test_mcp_service_failure_bills_nothing_and_names_providers(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, api_key="internal-key")
    module.services._SPEC_BY_PATH["/svc/market"]["chain"] = lambda args: [
        module.services._Provider(
            "m:down",
            lambda a, t: (_ for _ in ()).throw(
                module.services.ProviderError("timed out", transient=True)),
            timeout=5.0, attempts=1)
    ]
    client = TestClient(module.app)
    response = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "market_data", "arguments": {"op": "spot", "pair": "BTC-USD"}},
    }, headers={"X-API-Key": "internal-key"})
    result = response.json()["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["billed"] is False
    assert payload["failed_providers"] == ["m:down: timed out"]


# --------------------------------------------------------------------------
# Provider adapters against stubbed transports.
# --------------------------------------------------------------------------


def _load_providers(monkeypatch, tmp_path=None, **env):
    _clean_env(monkeypatch, tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return _load_module("service_providers", "svc_providers_under_test")


def test_anthropic_adapter_normalizes_text_usage_and_cost(monkeypatch):
    sp = _load_providers(monkeypatch, ANTHROPIC_API_KEY="k")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _StubResponse(payload={
            "content": [{"type": "text", "text": "hello"}],
            "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        })

    monkeypatch.setattr(sp.httpx, "post", fake_post)
    result = sp.call_anthropic({"prompt": "hi", "max_tokens": 100}, timeout=5)
    assert result["text"] == "hello"
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 500}
    # 1000 in at $1/M + 500 out at $5/M = $0.0035
    assert result["cost_microusd"] == 3500
    assert captured["headers"]["x-api-key"] == "k"
    assert "anthropic-version" in captured["headers"]


def test_llm_adapter_maps_http_status_to_transient_or_permanent(monkeypatch):
    sp = _load_providers(monkeypatch, ANTHROPIC_API_KEY="k")
    monkeypatch.setattr(sp.httpx, "post",
                        lambda *a, **k: _StubResponse(status_code=529, payload={}))
    with pytest.raises(sp.ProviderError) as transient:
        sp.call_anthropic({"prompt": "x", "max_tokens": 10}, timeout=5)
    assert transient.value.transient is True
    monkeypatch.setattr(sp.httpx, "post",
                        lambda *a, **k: _StubResponse(status_code=401,
                                                      payload={"error": "bad key"}))
    with pytest.raises(sp.ProviderError) as permanent:
        sp.call_anthropic({"prompt": "x", "max_tokens": 10}, timeout=5)
    assert permanent.value.transient is False


def test_gemini_key_travels_in_a_header_never_the_url(monkeypatch):
    sp = _load_providers(monkeypatch, GEMINI_API_KEY="sekret")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers)
        return _StubResponse(payload={
            "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        })

    monkeypatch.setattr(sp.httpx, "post", fake_post)
    sp.call_gemini({"prompt": "x", "max_tokens": 10}, timeout=5)
    assert "sekret" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "sekret"


def test_brave_and_serper_normalize_to_one_result_shape(monkeypatch):
    sp = _load_providers(monkeypatch, BRAVE_SEARCH_API_KEY="k", SERPER_API_KEY="k")
    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: _StubResponse(payload={
        "web": {"results": [{"title": "T", "url": "https://a", "description": "D"}]}
    }))
    brave = sp.call_brave_search({"query": "q", "count": 5}, timeout=5)
    monkeypatch.setattr(sp.httpx, "post", lambda *a, **k: _StubResponse(payload={
        "organic": [{"title": "T", "link": "https://a", "snippet": "D"}]
    }))
    serper = sp.call_serper_search({"query": "q", "count": 5}, timeout=5)
    assert brave["results"] == serper["results"] == [
        {"title": "T", "url": "https://a", "snippet": "D"}
    ]


def test_rpc_chain_error_object_is_the_result_but_rate_limits_fall_over(monkeypatch):
    sp = _load_providers(monkeypatch)
    monkeypatch.setattr(sp.httpx, "post", lambda *a, **k: _StubResponse(payload={
        "jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}
    }))
    result = sp.call_chain_rpc({"method": "eth_call", "params": []}, 5, "https://up.example")
    assert result["error"]["message"] == "execution reverted"
    monkeypatch.setattr(sp.httpx, "post", lambda *a, **k: _StubResponse(payload={
        "jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit exceeded"}
    }))
    with pytest.raises(sp.ProviderError) as excinfo:
        sp.call_chain_rpc({"method": "eth_call", "params": []}, 5, "https://up.example")
    assert excinfo.value.transient is True


def test_polymarket_stringified_outcome_fields_are_parsed(monkeypatch):
    sp = _load_providers(monkeypatch)
    monkeypatch.setattr(sp.httpx, "get", lambda *a, **k: _StubResponse(payload=[{
        "id": "1", "question": "Q?", "slug": "q",
        "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"0.62\", \"0.38\"]",
        "volume": "100", "endDate": "2027-01-01", "active": True, "closed": False,
    }]))
    result = sp.call_polymarket({"op": "markets", "limit": 5}, timeout=5)
    market = result["markets"][0]
    assert market["outcomes"] == ["Yes", "No"]
    assert market["outcome_prices"] == ["0.62", "0.38"]


def test_extraction_strips_scripts_and_collects_links(monkeypatch):
    sp = _load_providers(monkeypatch)
    html = (
        "<html lang='en'><head><title>Doc  Title</title>"
        "<meta name='description' content='About'>"
        "<script>var secret = 'nope';</script></head>"
        "<body><h1>Heading</h1><p>Body text here.</p>"
        "<a href='https://x.example/a'>A link</a>"
        "<style>.x{}</style></body></html>"
    )
    result = sp.extract_from_html(html, "https://x.example", "https://x.example/")
    assert result["title"] == "Doc Title"
    assert result["description"] == "About"
    assert result["lang"] == "en"
    assert "Body text here." in result["text"]
    assert "secret" not in result["text"], "script contents are not page text"
    assert {"href": "https://x.example/a", "text": "A link"} in result["links"]


def test_fetch_and_extract_refuse_targets_the_audit_gate_refuses(monkeypatch, tmp_path):
    """The SSRF rule has one implementation; the services must be behind it."""
    svc = _load_services(monkeypatch, tmp_path)
    for path in ("/svc/fetch", "/svc/extract"):
        problem = svc.validate_route(path, {"url": "http://169.254.169.254/latest"})
        assert problem is not None and "private" in problem


# --------------------------------------------------------------------------
# The sandbox. Exercised for real where this host can isolate; the refusal
# path is asserted everywhere else.
# --------------------------------------------------------------------------


def _sandbox_ready(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path, SVC_CODE_EXEC="1")
    return svc, svc.service_providers.sandbox_mode()


def test_code_service_is_absent_unless_the_operator_enables_it(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path)
    assert "code" not in {spec["id"] for spec in svc.catalog()}


def test_sandbox_runs_code_with_a_clean_environment(monkeypatch, tmp_path):
    svc, mode = _sandbox_ready(monkeypatch, tmp_path)
    if mode is None:
        pytest.skip("host cannot isolate untrusted code")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_leakcheck")
    result, _ = svc.execute_route("/svc/code", {
        "code": "import os; print(sorted(os.environ)); print(2**10)"
    })
    assert "1024" in result["stdout"]
    assert "STRIPE_SECRET_KEY" not in result["stdout"], "secrets leaked into the sandbox env"
    assert result["exit_code"] == 0
    assert result["sandbox"] in ("netns", "setuid")


def test_sandbox_timeout_and_nonzero_exit_are_results_not_failures(monkeypatch, tmp_path):
    svc, mode = _sandbox_ready(monkeypatch, tmp_path)
    if mode is None:
        pytest.skip("host cannot isolate untrusted code")
    result, _ = svc.execute_route("/svc/code", {
        "code": "while True: pass", "timeout_seconds": 1
    })
    assert result["timed_out"] is True
    failed, _ = svc.execute_route("/svc/code", {"code": "raise SystemExit(3)"})
    assert failed["exit_code"] == 3


# --------------------------------------------------------------------------
# Discovery integrity.
# --------------------------------------------------------------------------


def test_agent_manifest_service_entries_carry_schemas_prices_and_examples(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path)
    client = TestClient(module.app)
    manifest = client.get("/.well-known/agent.json").json()
    service_entries = [e for e in manifest["endpoints"] if e["path"].startswith("/svc/")]
    assert service_entries, "no service entries in the manifest"
    for entry in service_entries:
        assert entry["payment_required"] is True
        assert entry["price_usd"] > 0
        assert entry["input_schema"]["type"] == "object"
        assert entry["output_schema"]["type"] == "object"
        assert entry["example_request"]["body"], entry["path"]


def test_openapi_marks_service_routes_payable_with_examples(monkeypatch, tmp_path):
    module = _load_main(
        monkeypatch, tmp_path,
        X402_FACILITATOR_URL="https://facilitator.example",
        X402_PAY_TO_ADDRESS="0x1111111111111111111111111111111111111111",
    )
    client = TestClient(module.app)
    doc = client.get("/openapi.json").json()
    for spec in module.services.catalog():
        operation = doc["paths"][spec["path"]]["post"]
        info = operation.get("x-payment-info")
        assert info and info["offers"], f"{spec['path']} is not discoverable as payable"
        assert "402" in operation["responses"]
        example = operation["requestBody"]["content"]["application/json"]["example"]
        assert example == spec["input_example"]


def test_health_endpoint_is_free_and_leaks_no_configuration_values(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, ANTHROPIC_API_KEY="sk-ant-verysecret",
                        BRAVE_SEARCH_API_KEY="brv-verysecret")
    client = TestClient(module.app)
    response = client.get("/svc/health")
    assert response.status_code == 200
    text = response.text
    assert "verysecret" not in text
    body = response.json()
    assert body["services"]["llm"]["available"] is True
    assert body["services"]["search"]["available"] is True
    # An Anthropic key enables inference, not image generation.
    assert body["services"]["image"]["available"] is False
    assert body["services"]["code"]["available"] is False


def test_metrics_endpoint_is_invisible_without_the_internal_key(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, api_key="operator-key")
    client = TestClient(module.app)
    assert client.get("/svc/metrics").status_code == 404
    assert client.get("/svc/metrics", headers={"X-API-Key": "wrong"}).status_code == 404
    ok = client.get("/svc/metrics", headers={"X-API-Key": "operator-key"})
    assert ok.status_code == 200
    assert "services" in ok.json()


def test_metrics_is_a_404_not_a_501_when_no_internal_key_exists(monkeypatch, tmp_path):
    module = _load_main(monkeypatch, tmp_path, api_key=None)
    client = TestClient(module.app)
    assert client.get("/svc/metrics", headers={"X-API-Key": "guess"}).status_code == 404


def test_every_service_route_source_validates_then_authorizes_then_delivers():
    """Static guard, in the spirit of the audits' own: the one service-route
    factory must gate for free before authorising, and must end in
    _deliver -- the receipt path."""
    text = MAIN_PATH.read_text()
    factory = text.split("def _make_service_route", 1)[1].split("\ndef ", 1)[0]
    validate = factory.index("services.validate_route(")
    authorize = factory.index("_authorize_and_rate_limit(")
    deliver = factory.index("return _deliver(result, auth)")
    assert validate < authorize < deliver


def test_service_prices_are_two_decimal_safe():
    """The 402 builders format prices as $X.XX and the meters count whole
    cents; a sub-cent catalog price would be quoted as $0.00 and metered as
    zero. Enforced here so a future price edit cannot reintroduce it."""
    svc = _load_module("services", "svc_price_check")
    for spec in svc.CATALOG:
        cents = spec["price_usd"] * 100
        assert abs(cents - round(cents)) < 1e-9, f"{spec['id']} price is not whole cents"
        assert round(cents) >= 1, f"{spec['id']} is priced below one cent"


def test_research_composes_search_extract_and_llm_with_citations(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path,
                         ANTHROPIC_API_KEY="k", BRAVE_SEARCH_API_KEY="k")
    monkeypatch.setattr(
        svc, "_search_chain",
        lambda args: [svc._Provider("search:stub", lambda a, t: {
            "query": a["query"],
            "results": [
                {"title": "One", "url": "https://one.example/a", "snippet": "s"},
                {"title": "Dup", "url": "https://one.example/b", "snippet": "s"},
                {"title": "Two", "url": "https://two.example/", "snippet": "s"},
            ],
            "cost_microusd": 5000,
        }, timeout=5.0, attempts=1)],
    )
    svc._SPEC_BY_ID["search"]["chain"] = svc._search_chain
    extracted = []
    monkeypatch.setattr(
        svc.service_providers, "call_web_extract",
        lambda args, timeout: extracted.append(args["url"]) or {
            "url": args["url"], "final_url": args["url"], "title": "Page",
            "text": "Useful source text.",
        },
    )
    monkeypatch.setattr(svc.audits, "blocked_target_reason", lambda url: None)
    monkeypatch.setattr(
        svc, "_llm_chain",
        lambda args: [svc._Provider("llm:stub", lambda a, t: {
            "text": "Answer with a claim [1] and another [2].",
            "model": "stub-model", "cost_microusd": 900,
        }, timeout=5.0, attempts=1)],
    )
    svc._SPEC_BY_ID["llm"]["chain"] = svc._llm_chain

    result, call_id = svc.execute_route("/svc/research", {"query": "what is hubvibe"})
    assert result["provider"] == "composite"
    assert "[1]" in result["answer"]
    assert [s["url"] for s in result["sources"]] == [
        "https://one.example/a", "https://two.example/",
    ], "one source per distinct host, in rank order"
    assert result["providers_used"] == {"search": "search:stub", "llm": "llm:stub"}
    summary = svc.metrics_summary()
    assert summary["services"]["research"]["est_provider_cost_usd"] == pytest.approx(0.0059)


def test_research_with_no_grounded_answer_bills_nothing(monkeypatch, tmp_path):
    svc = _load_services(monkeypatch, tmp_path,
                         ANTHROPIC_API_KEY="k", BRAVE_SEARCH_API_KEY="k")
    svc._SPEC_BY_ID["search"]["chain"] = lambda args: [
        svc._Provider("search:stub", lambda a, t: {"query": a["query"], "results": []},
                      timeout=5.0, attempts=1)
    ]
    with pytest.raises(svc.ServiceFailure) as excinfo:
        svc.execute_route("/svc/research", {"query": "anything"})
    assert "no results" in excinfo.value.detail
