"""The buyer-side router: clears a 402 with the local wallet, caches
analytical results for 24 hours, and refuses to sign past its caps.

Driven against a fake node in-process (a real HTTP server on 127.0.0.1)
that answers 402 until it sees a PAYMENT-SIGNATURE header, so no wallet,
no chain and no money are involved. Signing itself is stubbed at the one
seam where the x402 library is called.
"""

import base64
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = REPO_ROOT / "integrations" / "hubvibe_router.py"


def _load_router():
    spec = importlib.util.spec_from_file_location("hubvibe_router", ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hubvibe_router"] = module
    spec.loader.exec_module(module)
    return module


R = _load_router()


def test_public_router_version_is_1_3_0():
    assert R.__version__ == "1.3.0"


def _process_buy(url, home, start, results, number):
    """A real child process sharing only the on-disk buyer ledger."""
    buyer = R.Router(url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=10, daily_cap_usd=0.10, home=home)
    buyer._sign = lambda response, target: {"PAYMENT-SIGNATURE": "child-signature"}
    start.wait()
    try:
        buyer.call("/work/market/quote", {"request": number})
        results.put("ok")
    except R.RouterError as exc:
        results.put(exc.reason)


def _process_retry_same_identity(url, home, start, results):
    """Two spawned buyers contend for one durable purchase identity."""
    buyer = R.Router(url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=1, home=home)
    buyer._sign = lambda response, target: {"PAYMENT-SIGNATURE": "child-signature"}
    start.wait()
    try:
        buyer.call("/work/market/quote", {"product_id": "BTC-USD"},
                   idempotency_key="shared-process-identity")
        results.put("ok")
    except R.RouterError as exc:
        results.put(exc.reason)


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeNode:
    """A HubVibe-shaped node: 402 (with the v2 header and a JSON body) until
    paid, then a 200 envelope with a PAYMENT-RESPONSE receipt. Counts every
    execution so cache hits are provable."""

    def __init__(self, prices: dict):
        self.prices = prices
        self.executions = []
        self.refuse_payment = False
        node = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, extra=None):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/work":
                    return self._send(200, {"count": len(node.prices), "workers": node.workers})
                if self.path in node.jobs:
                    envelope = node.jobs.pop(self.path)
                    return self._send(200, envelope, {"PAYMENT-RESPONSE": _b64({
                        "success": True, "payer": "0xPAYER", "transaction": "0xTX",
                        "network": "eip155:8453"})})
                if self.path.startswith("/work/receipts/"):
                    receipt = node.receipts.get(self.path)
                    return self._send(200, receipt) if receipt else self._send(404, {"reason": "unknown_receipt"})
                if self.path.startswith("/contracts/"):
                    route = self.path[len("/contracts"):]
                    if route not in node.prices or route in node.no_contract:
                        return self._send(404, {"status": "error", "reason": "unknown_capability"})
                    return self._send(200, node.contract_for(route))
                return self._send(404, {"detail": "Not Found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/a2a":
                    return self._a2a(body)
                if self.path.startswith("/contracts/") and self.path.endswith("/verify"):
                    route = self.path[len("/contracts"):-len("/verify")]
                    if route not in node.prices:
                        return self._send(404, {"status": "error", "reason": "unknown_capability"})
                    node.verify_requests.append((route, body))
                    mismatches = []
                    if body.get("input") is not None and node.reject_input:
                        mismatches.append({"field": "input", "actual": "rejected by the fake"})
                    return self._send(200, {"capability": route, "contract_hash": node.contract_for(route)["contract_hash"],
                                            "verified": not mismatches, "mismatches": mismatches, "checks": []})
                price = node.prices.get(self.path)
                if price is None:
                    return self._send(404, {"detail": "Not Found"})
                paid = self.headers.get("PAYMENT-SIGNATURE")
                prepaid = node.accept_api_key and self.headers.get("X-API-Key") == "test-prepaid-key"
                idempotency_key = self.headers.get("Idempotency-Key")
                if paid:
                    node.paid_headers.append(dict(self.headers))
                    bound = self.headers.get("X-HubVibe-Contract")
                    if bound and bound != node.contract_for(self.path)["contract_hash"]:
                        return self._send(402, {"error": "contract_mismatch", "price_usd": price, "billed": False})
                if price > 0 and (not paid and not prepaid or node.refuse_payment):
                    quoted = price + 0.01 if node.tamper_price else price
                    challenge = {"x402Version": 2, "accepts": [
                        {"scheme": "exact", "network": "eip155:8453", "amount": str(int(quoted * 1_000_000)),
                         "payTo": node.tamper_payto or "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", "asset": "0x8335"},
                        {"scheme": "exact", "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                         "amount": str(int(price * 1_000_000)), "payTo": "J1K4", "asset": "EPjF"}]}
                    return self._send(402, {"error": "payment_required", "price_usd": quoted,
                                            "detail": "facilitator refused" if paid else "pay first"},
                                      {"PAYMENT-REQUIRED": _b64(challenge)})
                if paid and node.unbilled_failure:
                    return self._send(503, {"status": "error", "reason": "provider_unavailable",
                                            "billed": False, "detail": "no source data"})
                if paid and idempotency_key in node.idempotent:
                    return self._send(200, {**node.idempotent[idempotency_key],
                                            "idempotent_replay": True, "billed": False})
                if paid and node.unknown_after_payment:
                    return self._send(500, {"status": "error", "reason": "settlement_unknown"})
                if paid and node.paid_delay:
                    time.sleep(node.paid_delay)
                envelope = node.deliver(self.path, body, paid=not prepaid)
                if self.path.startswith("/audit/") and node.audit_header_receipt_only:
                    envelope.pop("receipt_id", None)
                    envelope.pop("receipt_url", None)
                if idempotency_key:
                    node.idempotent[idempotency_key] = envelope
                if node.defer_next and paid:
                    node.defer_next = False
                    collect = "/work/jobs/job-1"
                    node.jobs[collect] = envelope
                    return self._send(202, {"status": "processing", "collect_url": collect,
                                            "retry_after_seconds": 0, "billed": False},
                                      {"Retry-After": "0"})
                extra = {"PAYMENT-RESPONSE": _b64({"success": True, "payer": "0xPAYER", "transaction": "0xTX",
                                                   "network": "eip155:8453"})} \
                    if price > 0 and not prepaid and not node.omit_settlement else None
                return self._send(200, envelope, extra)

            def _a2a(self, rpc):
                """The node's A2A shape: a task in input-required carrying the
                v2 challenge until the message metadata carries a payload."""
                node.a2a_requests.append((dict(self.headers), rpc))
                if rpc.get("method") != "SendMessage" or self.headers.get("A2A-Version") != "1.0":
                    return self._send(200, {"jsonrpc": "2.0", "id": rpc.get("id"),
                                            "error": {"code": -32601, "message": "Method not found"}})
                message = rpc["params"]["message"]
                data = message["parts"][0]["data"]
                skill, arguments = data["skill"], data.get("arguments") or {}
                route = R.Router.route_for(skill)  # the node takes worker and MCP tool names alike
                price = node.prices.get(route)
                task_id = message.get("taskId") or "task-%d" % (len(node.a2a_requests))
                if price is None:
                    return self._send(200, {"jsonrpc": "2.0", "id": rpc["id"],
                                            "error": {"code": -32602, "message": f"Unknown skill {skill!r}"}})
                meta = message.get("metadata") or {}
                paid = "x402.payment.payload" in meta
                if price > 0 and (not paid or node.refuse_payment):
                    md = {"x402.payment.status": "payment-failed" if paid else "payment-required",
                          "x402.payment.required": {"x402Version": 2, "price_usd": price, "accepts": [
                              {"scheme": "exact", "network": "eip155:8453", "amount": str(int(price * 1_000_000)),
                               "payTo": node.tamper_payto or "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
                               "asset": "0x8335"}]}}
                    if paid:
                        md["x402.payment.error"] = "SETTLEMENT_FAILED"
                    return self._send(200, {"jsonrpc": "2.0", "id": rpc["id"], "result": {"task": {
                        "id": task_id, "contextId": "ctx", "status": {
                            "state": "TASK_STATE_INPUT_REQUIRED",
                            "message": {"messageId": "a", "role": "ROLE_AGENT",
                                        "parts": [{"text": "Payment is required: $%.2f." % price}],
                                        "metadata": md}}}}})
                if paid and node.unknown_after_payment:
                    return self._send(500, {"status": "error", "reason": "settlement_unknown"})
                envelope = node.deliver(route, arguments, worker=skill)
                md = {"x402.payment.status": "payment-completed",
                      "x402.payment.receipts": [{"success": True, "payer": "0xPAYER", "transaction": "0xTX",
                                                 "network": "eip155:8453"}]} if price > 0 else {}
                return self._send(200, {"jsonrpc": "2.0", "id": rpc["id"], "result": {"task": {
                    "id": task_id, "contextId": "ctx",
                    "status": {"state": "TASK_STATE_COMPLETED",
                               "message": {"messageId": "b", "role": "ROLE_AGENT",
                                           "parts": [{"text": "Done."}], "metadata": md}},
                    "artifacts": [{"artifactId": task_id + "-result", "name": skill,
                                   "parts": [{"data": envelope, "mediaType": "application/json"}]}]}}})

            def log_message(self, *a):
                pass

        self.a2a_requests = []
        self.verify_requests = []
        self.paid_headers = []
        self.receipts = {}
        self.idempotent = {}
        self.jobs = {}
        self.defer_next = False
        self.unbilled_failure = False
        self.unknown_after_payment = False
        self.paid_delay = 0.0
        self.accept_api_key = False
        self.receipt_tamper = None
        self.omit_settlement = False
        self.audit_header_receipt_only = True
        self._delivery_lock = threading.Lock()
        self.tamper_payto = None
        self.tamper_price = False
        self.reject_input = False
        self.no_contract = set()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.workers = [
            {"name": route.replace("/work/", "").replace("/", "."), "path": route,
             "price_usd": price, "composes": [], "input_schema": {"type": "object"},
             "output_schema": {"type": "object"}}
            for route, price in prices.items() if route.startswith("/work/")
        ]

    def deliver(self, route: str, body: dict, *, worker=None, paid=True) -> dict:
        with self._delivery_lock:
            self.executions.append((route, body))
            number = len(self.executions)
            result = {"echo": body, "n": number}
            receipt_url = "/work/receipts/rcpt_%d" % number
            envelope = {
                "status": "ok", "worker": worker or route.replace("/work/", "").replace("/", "."),
                "price_usd": self.prices[route], "result": result,
                "provenance": {"steps": [], "providers_used": [], "attempts": 1, "elapsed_ms": 1},
                "receipt_id": "rcpt_%d" % number, "receipt_url": receipt_url,
            }
            result_hash = "sha256:" + __import__("hashlib").sha256(
                json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            receipt = {
                "receipt_version": 1, "receipt_id": "rcpt_%d" % number,
                "outcome": "paid_delivered" if paid else "delivered_not_settled",
                "paid": paid, "delivered": True,
                "payment": {"amount_atomic": int(self.prices[route] * 1_000_000) if paid else None,
                            "tx_hash": "0xTX" if paid else None, "settled": paid},
                "request": {"path": route, "price_usd": self.prices[route]},
                "delivery": {"delivered": True, "result_hash": result_hash},
            }
            if self.receipt_tamper == "amount":
                receipt["payment"]["amount_atomic"] += 1
            elif self.receipt_tamper == "hash":
                receipt["delivery"]["result_hash"] = "sha256:wrong"
            elif self.receipt_tamper == "path":
                receipt["request"]["path"] = "/work/other"
            self.receipts[receipt_url] = receipt
            return envelope

    def contract_for(self, route: str) -> dict:
        """The node's purchase contract for a route: the shape hubvibe-io.com
        serves at /contracts/{capability}, with the terms the fake's 402 offers."""
        price = self.prices[route]
        amount = str(int(price * 1_000_000))
        return {
            "contract_version": "1",
            "capability": {"id": route.replace("/work/", "").replace("/", "."), "path": route,
                           "url": self.url + route, "method": "POST"},
            "provider": {"name": "HubVibe", "base_url": self.url},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "price": {"usd": price, "currency": "USD"},
            "payment": {"rails": [
                {"protocol": "x402", "x402_version": 2, "network": "eip155:8453", "asset": "0x8335",
                 "pay_to": "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", "amount_atomic": amount},
                {"protocol": "x402", "x402_version": 2, "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                 "asset": "EPjF", "pay_to": "J1K4", "amount_atomic": amount},
            ]},
            "contract_hash": "sha256:fake-" + route.strip("/").replace("/", "-"),
        }

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def node():
    n = FakeNode({"/work/market/quote": 0.02, "/work/data/query": 0.50, "/work/research/brief": 5.00,
                  "/audit/wcag": 0.05, "/work/free/probe": 0.0})
    yield n
    n.close()


@pytest.fixture
def router(node, tmp_path, monkeypatch):
    r = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.00, budget_usd=2.00,
                 daily_cap_usd=50.00, cache_ttl=3600, home=str(tmp_path / "home"))
    signed = []

    def fake_sign(response, url):  # the one seam where the x402 library signs
        signed.append((url, R.Router._price_of(response)))
        return {"PAYMENT-SIGNATURE": "stub-signature"}

    monkeypatch.setattr(r, "_sign", fake_sign)

    def fake_sign_challenge(required):  # the A2A seam: a PaymentRequired dict in, a payload out
        signed.append(("a2a", R.Router._price_of_challenge(required)))
        return {"x402Version": 2, "scheme": "exact", "network": "eip155:8453", "payload": {"stub": True}}

    monkeypatch.setattr(r, "_sign_challenge", fake_sign_challenge)
    r.signed = signed
    return r


# --- clearing a 402 -------------------------------------------------------------

def test_a_402_is_cleared_with_the_local_wallet_and_the_result_delivered(router, node):
    out = router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "BTC-USD"}
    assert router.signed == [(node.url + "/work/market/quote", 0.02)]
    assert node.executions == [("/work/market/quote", {"product_id": "BTC-USD"})]
    assert router.spent_usd == 0.02
    row = [entry for entry in router.ledger() if entry.get("settled")][-1]
    assert row["path"] == "/work/market/quote" and row["settled"] is True
    assert row["tx"] == "0xTX" and row["payer"] == "0xPAYER" and row["rail"] == "base"


def test_a_free_route_is_delivered_without_signing_anything(router, node):
    out = router.call("/work/free/probe", {"a": 1})
    assert out["status"] == "ok" and router.signed == [] and router.spent_usd == 0.0


def test_audit_routes_verify_the_settlement_header_without_a_worker_receipt(router, node):
    out = router.call("/audit/wcag", {"url": "https://example.com"})
    assert out["status"] == "ok" and "receipt_url" not in out
    assert router.spent_usd == 0.05 and len(router.signed) == 1


def test_a_prepaid_delivery_is_receipt_verified_before_it_is_cached(node, tmp_path):
    node.accept_api_key = True
    buyer = R.Router(node.url, api_key="test-prepaid-key", home=str(tmp_path / "home"))
    first = buyer.call("/work/data/query", {"sql": "SELECT 1"})
    second = buyer.call("/work/data/query", {"sql": "SELECT 1"})
    assert first["router"]["cache"] == "miss" and second["router"]["cache"] == "hit"
    assert len(node.executions) == 1 and buyer.spent_usd == 0


def test_a_prepaid_delivery_with_a_bad_receipt_is_not_cached(node, tmp_path):
    node.accept_api_key = True
    node.receipt_tamper = "hash"
    buyer = R.Router(node.url, api_key="test-prepaid-key", home=str(tmp_path / "home"))
    with pytest.raises(R.ReceiptVerificationFailed):
        buyer.call("/work/data/query", {"sql": "SELECT 1"})
    assert list(buyer.cache_dir.glob("*.json")) == []


def test_quote_is_free_and_names_the_rails(router, node):
    q = router.quote("/work/research/brief", {"url": "https://example.com"})
    assert q["status"] == 402 and q["price_usd"] == 5.00
    assert q["rails"] == ["eip155:8453", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"]
    assert router.signed == [] and node.executions == []


def test_a_refused_settlement_is_reported_not_swallowed(router, node):
    node.refuse_payment = True
    with pytest.raises(R.PaymentRefused) as info:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert "facilitator refused" in str(info.value)
    assert router.spent_usd == 0.0
    assert router.ledger()[-1]["settled"] is False and router.ledger()[-1]["outcome"] == "refused"


# --- caps: refused BEFORE a signature exists -------------------------------------------

def test_a_price_above_the_per_call_cap_is_never_signed(router, node):
    with pytest.raises(R.CapExceeded) as info:
        router.call("/work/research/brief", {"url": "https://example.com"})
    assert info.value.extra["cap"] == "per_call" and router.signed == []
    assert node.executions == []


def test_the_process_budget_stops_a_loop(router, node):
    for i in range(4):  # budget 2.00 at 0.50 each: four fit
        router.call("/work/data/query", {"sql": f"select {i}"})
    with pytest.raises(R.CapExceeded) as info:
        router.call("/work/data/query", {"sql": "select 99"})
    assert info.value.extra["cap"] == "process_budget"
    assert len(router.signed) == 4 and router.spent_usd == 2.00


def test_the_daily_cap_reads_the_shared_ledger_across_processes(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    first = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.0, budget_usd=100.0,
                     daily_cap_usd=0.60, home=home)
    monkeypatch.setattr(first, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    first.call("/work/data/query", {"sql": "select 1"})  # 0.50 today
    second = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1.0, budget_usd=100.0,
                      daily_cap_usd=0.60, home=home)  # a new process on the same machine
    monkeypatch.setattr(second, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.CapExceeded) as info:
        second.call("/work/data/query", {"sql": "select 2"})  # would be 1.00 today
    assert info.value.extra["cap"] == "daily"


def test_caps_must_be_positive():
    with pytest.raises(R.CapExceeded):
        R.Router("http://x", evm_key="0x" + "1" * 64, max_price_usd=0)


def test_no_wallet_fails_closed_with_a_configuration_error(node, tmp_path):
    r = R.Router(node.url, home=str(tmp_path / "home"))
    with pytest.raises(R.NotConfigured):
        r.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert node.executions == []


# --- the 24-hour analytical cache -------------------------------------------------------

def test_a_repeated_analytical_query_is_served_from_cache_and_paid_once(router, node):
    body = {"sql": "select name from t limit 1"}
    first = router.call("/work/data/query", body)
    second = router.call("/work/data/query", body)
    assert first["router"]["cache"] == "miss" and second["router"]["cache"] == "hit"
    assert second["result"] == first["result"]
    assert len(node.executions) == 1 and len(router.signed) == 1 and router.spent_usd == 0.50


def test_the_cache_key_is_the_md5_of_route_and_body(router):
    import hashlib
    body = {"sql": "select 1", "max_scan_gib": 1}
    expected = hashlib.md5(json.dumps({"path": "/work/data/query", "body": body}, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    assert R.Router.cache_key("/work/data/query", body) == expected
    assert R.Router.cache_key("/work/data/query", {"max_scan_gib": 1, "sql": "select 1"}) == expected
    assert R.Router.cache_key("/work/data/query", {"sql": "select 2"}) != expected


def test_an_expired_cache_entry_is_bought_again(router, node):
    body = {"sql": "select 1"}
    router.call("/work/data/query", body)
    router.cache_ttl = 0
    time.sleep(0.01)
    again = router.call("/work/data/query", body)
    assert again["router"]["cache"] == "miss" and len(node.executions) == 2


def test_non_analytical_routes_are_never_cached(router, node):
    body = {"product_id": "BTC-USD"}
    out = router.call("/work/market/quote", body)
    router.call("/work/market/quote", body)
    assert len(node.executions) == 2 and "router" not in out


def test_cache_can_be_bypassed_per_call_and_cleared(router, node):
    body = {"sql": "select 1"}
    router.call("/work/data/query", body)
    router.call("/work/data/query", body, use_cache=False)
    assert len(node.executions) == 2
    assert router.cache_clear() == 1
    router.call("/work/data/query", body)
    assert len(node.executions) == 3


# --- the local proxy ----------------------------------------------------------------------

def test_the_proxy_clears_402s_for_an_agent_that_only_speaks_http(router, node):
    port = _free_port()
    threading.Thread(target=R.serve, args=(router, "127.0.0.1", port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            httpx.get(base + "/router/status", timeout=1)
            break
        except Exception:
            time.sleep(0.05)
    status = httpx.get(base + "/router/status").json()
    assert status["node"] == node.url and status["rail"] == "base"
    out = httpx.post(base + "/work/market/quote", json={"product_id": "BTC-USD"}, timeout=10).json()
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "BTC-USD"}
    over = httpx.post(base + "/work/research/brief", json={"url": "https://example.com"}, timeout=10)
    assert over.status_code == 402 and over.json()["reason"] == "spend_cap_exceeded"
    assert httpx.get(base + "/work", timeout=10).json()["count"] == 5
    rows = httpx.get(base + "/router/ledger", timeout=10).json()["rows"]
    assert [row for row in rows if row.get("settled")][-1]["path"] == "/work/market/quote"


# --- errors never leak a local path -------------------------------------------------------

def test_errors_are_json_and_carry_no_local_paths(router, node):
    node.refuse_payment = True
    with pytest.raises(R.RouterError) as info:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    body = info.value.as_json()
    assert body["status"] == "error" and body["reason"] == "payment_refused"
    assert "/home/" not in json.dumps(body) and "home" not in body


# --- the copyable shape: HubVibeRouter(endpoint=, wallet_type=).execute_task(tool=, payload=) ---

def test_tool_names_resolve_to_routes():
    f = R.Router.route_for
    assert f("stats.probability") == "/work/stats/probability"
    assert f("market.quote") == "/work/market/quote"
    assert f("research.page_facts") == "/work/research/page_facts"
    assert f("audit.wcag") == "/audit/wcag" and f("bundle") == "/audit/bundle"
    assert f("/work/llm/generate") == "/work/llm/generate" and f("/audit/seo") == "/audit/seo"
    # The MCP tool names, as tools/list and the A2A card spell them.
    assert f("hubvibe_market_quote") == "/work/market/quote"
    assert f("hubvibe_research_page_facts") == "/work/research/page_facts"
    assert f("hubvibe_security_mcp_inspect") == "/work/security/mcp_inspect"
    assert f("hubvibe_predictive_probability_engine") == "/work/stats/probability"
    assert f("audit_wcag") == "/audit/wcag" and f("audit_bundle") == "/audit/bundle"


# --- VERIFY before PAY ----------------------------------------------------------------


def test_a_purchase_is_verified_against_the_contract_before_anything_is_signed(router, node):
    out = router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert out["status"] == "ok"
    # The node checked the body against the input schema before the signature existed...
    assert node.verify_requests == [("/work/market/quote", {"input": {"product_id": "BTC-USD"}})]
    # ...and the paid request was bound to the contract that was verified.
    assert node.paid_headers[-1].get("X-HubVibe-Contract") == node.contract_for("/work/market/quote")["contract_hash"]
    assert router.signed == [(node.url + "/work/market/quote", 0.02)]


def test_a_402_whose_pay_to_differs_from_the_contract_is_never_signed(router, node):
    node.tamper_payto = "0x000000000000000000000000000000000000dEaD"
    with pytest.raises(R.VerificationFailed) as err:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert "payTo" in str(err.value)
    assert router.signed == [] and router.spent_usd == 0.0 and node.paid_headers == []


def test_a_402_whose_price_differs_from_the_contract_is_never_signed(router, node):
    node.tamper_price = True
    with pytest.raises(R.VerificationFailed) as err:
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert "quotes" in str(err.value)
    assert router.signed == [] and node.paid_headers == []


def test_a_body_the_node_rejects_against_the_input_schema_is_never_paid_for(router, node):
    node.reject_input = True
    with pytest.raises(R.VerificationFailed):
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert router.signed == [] and node.paid_headers == []


def test_a_capability_without_a_contract_is_never_paid_for(router, node):
    node.prices["/work/no/contract"] = 0.02
    node.no_contract.add("/work/no/contract")
    with pytest.raises(R.VerificationFailed) as err:
        router.call("/work/no/contract", {})
    assert "sells no capability" in str(err.value)
    assert router.signed == [] and node.paid_headers == []


def test_verify_alone_is_free_and_reports_the_contract(router, node):
    result = router.verify("market.quote", {"product_id": "BTC-USD"}, expect={"price_usd": 0.02})
    assert result["verification"]["verified"] is True
    assert result["contract"]["capability"]["path"] == "/work/market/quote"
    assert router.signed == [] and node.executions == []


def test_a2a_purchases_are_verified_the_same_way(router, node):
    node.tamper_payto = "0x000000000000000000000000000000000000dEaD"
    with pytest.raises(R.VerificationFailed):
        router.a2a("market.quote", {"product_id": "BTC-USD"})
    assert router.signed == [] and router.spent_usd == 0.0
    node.tamper_payto = None
    out = router.a2a("market.quote", {"product_id": "BTC-USD"})
    assert out["status"] == "ok" and router.signed == [("a2a", 0.02)]


def test_the_proxy_verifies_on_request_and_refuses_a_bad_purchase(router, node):
    import socket

    def free_port():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

    port = free_port()
    threading.Thread(target=R.serve, args=(router, "127.0.0.1", port), daemon=True).start()
    time.sleep(0.3)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as agent:
        ok = agent.post("/router/verify", json={"tool": "market.quote", "body": {"product_id": "BTC-USD"}})
        assert ok.status_code == 200 and ok.json()["verification"]["verified"] is True
        contract = agent.get("/contracts/work/market/quote")
        assert contract.status_code == 200 and contract.json()["capability"]["path"] == "/work/market/quote"
        node.tamper_payto = "0x000000000000000000000000000000000000dEaD"
        refused = agent.post("/work/market/quote", json={"product_id": "BTC-USD"})
        assert refused.status_code == 409 and refused.json()["reason"] == "verification_failed"
        assert node.paid_headers == []


# --- the same job over A2A --------------------------------------------------------


def test_a2a_clears_the_payment_task_and_delivers_the_artifact(router, node):
    out = router.a2a("market.quote", {"product_id": "BTC-USD"})
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "BTC-USD"}
    assert router.signed == [("a2a", 0.02)]
    assert router.spent_usd == 0.02
    first, second = node.a2a_requests[-2], node.a2a_requests[-1]
    assert first[0].get("A2A-Version") == "1.0" and "a2a-x402" in first[0].get("A2A-Extensions", "")
    paid_message = second[1]["params"]["message"]
    assert paid_message["taskId"] == "task-%d" % (len(node.a2a_requests) - 1)
    assert paid_message["metadata"]["x402.payment.status"] == "payment-submitted"
    assert paid_message["metadata"]["x402.payment.payload"]["payload"] == {"stub": True}
    row = [entry for entry in router.ledger() if entry.get("settled")][-1]
    assert row["settled"] is True and row["tx"] == "0xTX" and row["transport"] == "a2a"


def test_a2a_accepts_mcp_tool_names_and_caches_analytical_skills(router, node):
    router.a2a("hubvibe_data_query", {"sql": "SELECT 1"})
    again = router.a2a("data.query", {"sql": "SELECT 1"})
    assert again["router"]["cache"] == "hit"
    assert len([e for e in node.executions if e[0] == "/work/data/query"]) == 1
    assert router.spent_usd == 0.50


def test_a2a_refused_settlement_is_reported_and_nothing_is_spent(router, node):
    node.refuse_payment = True
    with pytest.raises(R.PaymentRefused):
        router.a2a("market.quote", {"product_id": "BTC-USD"})
    assert router.spent_usd == 0.0
    assert router.ledger()[-1]["outcome"] == "refused"


def test_a2a_unknown_settlement_stays_reserved_and_is_not_retried(node, tmp_path, monkeypatch):
    node.unknown_after_payment = True
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=0.02, home=str(tmp_path / "home"))
    signed = []
    monkeypatch.setattr(
        buyer, "_sign_challenge",
        lambda required: signed.append(required) or {"x402Version": 2, "payload": {"stub": True}})
    with pytest.raises(R.PaymentUncertain):
        buyer.a2a("market.quote", {"product_id": "BTC-USD"})
    with pytest.raises(R.CapExceeded):
        buyer.a2a("market.quote", {"product_id": "BTC-USD"})
    assert len(signed) == 1 and buyer.spent_usd == 0 and node.executions == []


def test_a2a_reuses_a_receipt_verified_local_result_for_the_same_identity(router, node):
    body = {"product_id": "BTC-USD"}
    first = router.a2a("market.quote", body, idempotency_key="a2a-quote-1")
    second = router.a2a("market.quote", body, idempotency_key="a2a-quote-1")
    assert first["result"] == second["result"]
    assert second["idempotent_replay"] is True and second["billed"] is False
    assert router.signed == [("a2a", 0.02)] and len(node.executions) == 1


def test_a2a_never_signs_above_the_per_call_cap(router, node):
    with pytest.raises(R.CapExceeded):
        router.a2a("research.brief", {"url": "https://example.com"})
    assert router.signed == []


def test_a2a_unknown_skill_is_an_upstream_error_with_the_a2a_code(router, node):
    with pytest.raises(R.Upstream) as err:
        router.a2a("no.such.skill", {})
    assert err.value.extra["a2a_code"] == -32602


def test_a2a_free_skill_completes_without_signing(router, node):
    out = router.a2a("free.probe", {})
    assert out["status"] == "ok" and router.signed == []


def test_the_proxy_sells_skills_over_a2a_too(router, node):
    import socket

    def free_port():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

    port = free_port()
    threading.Thread(target=R.serve, args=(router, "127.0.0.1", port), daemon=True).start()
    time.sleep(0.3)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as agent:
        bought = agent.post("/a2a", json={"skill": "market.quote", "arguments": {"product_id": "ETH-USD"}})
        assert bought.status_code == 200 and bought.json()["result"]["echo"] == {"product_id": "ETH-USD"}
        bad = agent.post("/a2a", json={"arguments": {}})
        assert bad.status_code == 400 and bad.json()["reason"] == "invalid_request"


def test_the_copyable_client_executes_a_task_by_tool_name(node, tmp_path, monkeypatch):
    monkeypatch.setenv("HUBVIBE_WALLET_KEY", "0x" + "2" * 64)
    monkeypatch.setenv("HUBVIBE_HOME", str(tmp_path / "home"))
    client = R.HubVibeRouter(endpoint=node.url, wallet_type="base")
    monkeypatch.setattr(client, "_sign", lambda resp, url: {"PAYMENT-SIGNATURE": "s"})
    response = client.execute_task(tool="market.quote", payload={"product_id": "BTC-USD"})
    assert response["status"] == "ok" and response["worker"] == "market.quote"
    assert response["result"]["echo"] == {"product_id": "BTC-USD"} and response["price_usd"] == 0.02
    assert client.rail == "base" and client.spent_usd == 0.02


def test_the_copyable_client_picks_the_solana_rail_by_wallet_type(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBVIBE_SOLANA_KEY", "not-a-real-key")
    monkeypatch.setenv("HUBVIBE_HOME", str(tmp_path / "home"))
    client = R.HubVibeRouter(endpoint="http://127.0.0.1:9", wallet_type="solana")
    assert client.rail == "solana"
    with pytest.raises(R.NotConfigured):
        R.HubVibeRouter(endpoint="http://127.0.0.1:9", wallet_type="ethereum")


# --- durable purchase identity, atomic budget and delivery proof -----------------


def test_concurrent_calls_cannot_reserve_more_than_the_process_budget(node, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    node.prices["/work/market/quote"] = 0.10
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=0.10, daily_cap_usd=10, home=str(tmp_path / "home"))
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    barrier = threading.Barrier(2)

    def buy(number):
        barrier.wait()
        try:
            return buyer.call("/work/market/quote", {"request": number})
        except R.RouterError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(buy, (1, 2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, R.CapExceeded) for result in results) == 1
    assert len(signed) == 1 and len(node.executions) == 1 and buyer.spent_usd == 0.10


def test_a_202_is_collected_without_a_second_payment(router, node):
    node.defer_next = True
    out = router.call("/work/market/quote", {"product_id": "ETH-USD"},
                      idempotency_key="deferred-quote-1")
    assert out["status"] == "ok" and out["result"]["echo"] == {"product_id": "ETH-USD"}
    assert len(router.signed) == 1 and len(node.executions) == 1 and router.spent_usd == 0.02


def test_retrying_one_idempotency_key_executes_and_settles_once(router, node):
    body = {"product_id": "BTC-USD"}
    first = router.call("/work/market/quote", body, idempotency_key="quote-retry-1")
    second = router.call("/work/market/quote", body, idempotency_key="quote-retry-1")
    assert first["result"] == second["result"]
    assert second["idempotent_replay"] is True and second["billed"] is False
    assert len(router.signed) == 1 and len(node.paid_headers) == 1
    assert len(node.executions) == 1 and router.spent_usd == 0.02
    assert len([row for row in router.ledger(100) if row.get("settled")]) == 1


def test_a_settled_identity_is_never_signed_again_if_local_result_is_lost(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    key = "settled-before-local-crash"
    first = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=1, home=home)
    monkeypatch.setattr(first, "_sign", lambda response, url: {"PAYMENT-SIGNATURE": "first"})
    first.call("/work/market/quote", {"product_id": "BTC-USD"}, idempotency_key=key)
    for file in first.idempotency_dir.glob("*.json"):
        file.unlink()
    node.idempotent.clear()

    restarted = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                         budget_usd=1, daily_cap_usd=1, home=home)
    signed = []
    monkeypatch.setattr(restarted, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "second"})
    with pytest.raises(R.PaymentUncertain):
        restarted.call("/work/market/quote", {"product_id": "BTC-USD"}, idempotency_key=key)
    assert signed == [] and len(node.executions) == 1 and len(node.paid_headers) == 1


def test_a_paid_worker_requires_a_payment_settlement_before_delivery_is_verified(router, node):
    node.omit_settlement = True
    with pytest.raises(R.ReceiptVerificationFailed):
        router.call("/work/market/quote", {"product_id": "BTC-USD"},
                    idempotency_key="missing-settlement")
    assert router.spent_usd == 0.02
    assert list(router.idempotency_dir.glob("*.json")) == []


@pytest.mark.parametrize("tamper", ["amount", "hash", "path"])
def test_a_paid_result_with_a_mismatched_receipt_is_rejected(router, node, tamper):
    node.receipt_tamper = tamper
    with pytest.raises(R.ReceiptVerificationFailed):
        router.call("/work/market/quote", {"product_id": "BTC-USD"})
    # The payment response said settlement succeeded, so local accounting
    # remains conservative even though the untrusted delivery is rejected.
    assert router.spent_usd == 0.02 and len(node.executions) == 1


def test_an_explicit_unbilled_provider_failure_releases_the_reservation(router, node):
    node.unbilled_failure = True
    with pytest.raises(R.Upstream) as err:
        router.call("/work/market/quote", {"product_id": "MISSING"})
    assert err.value.extra["billed"] is False
    assert router.spent_usd == 0 and node.executions == []
    node.unbilled_failure = False
    assert router.call("/work/market/quote", {"product_id": "BTC-USD"})["status"] == "ok"


def test_one_idempotency_key_cannot_authorize_different_input(router, node):
    router.call("/work/market/quote", {"product_id": "BTC-USD"}, idempotency_key="fixed-request-1")
    signed_before = len(router.signed)
    with pytest.raises(R.VerificationFailed):
        router.call("/work/market/quote", {"product_id": "ETH-USD"}, idempotency_key="fixed-request-1")
    assert len(router.signed) == signed_before and len(node.executions) == 1


def test_two_processes_cannot_exceed_the_shared_daily_authorization(node, tmp_path):
    import multiprocessing

    node.prices["/work/market/quote"] = 0.10
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    args = (node.url, str(tmp_path / "shared-home"), start, results)
    processes = [ctx.Process(target=_process_buy, args=(*args, number)) for number in (1, 2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    outcomes = sorted(results.get(timeout=2) for _ in processes)
    assert outcomes == ["ok", "spend_cap_exceeded"]
    assert len(node.executions) == 1 and len(node.paid_headers) == 1


def test_two_processes_cannot_sign_the_same_purchase_identity_twice(node, tmp_path):
    import multiprocessing

    node.paid_delay = 0.25
    ctx = multiprocessing.get_context("spawn")
    start, results = ctx.Event(), ctx.Queue()
    args = (node.url, str(tmp_path / "shared-home"), start, results)
    processes = [ctx.Process(target=_process_retry_same_identity, args=args) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    outcomes = sorted(results.get(timeout=2) for _ in processes)
    assert outcomes == ["ok", "payment_status_uncertain"]
    assert len(node.executions) == 1 and len(node.paid_headers) == 1


def test_a_crashed_process_reservation_survives_restart(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    crashed = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                       budget_usd=10, daily_cap_usd=0.10, home=home)
    crashed._reserve(0.10, "/work/market/quote", "crashed-request")
    restarted = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                         budget_usd=10, daily_cap_usd=0.10, home=home)
    signed = []
    monkeypatch.setattr(restarted, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.CapExceeded) as err:
        restarted.call("/work/market/quote", {"product_id": "BTC-USD"})
    assert err.value.extra["cap"] == "daily" and signed == [] and node.executions == []


def test_an_expired_crash_reservation_is_recovered(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    stale = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=10, daily_cap_usd=0.10, home=home)
    stale._record({"kind": "reservation", "reservation_id": "dead-process",
                   "idempotency_key": "old", "path": "/work/market/quote",
                   "price_usd": 0.10, "expires_at": time.time() - 1, "settled": False})
    recovered = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                         budget_usd=10, daily_cap_usd=0.10, home=home)
    monkeypatch.setattr(recovered, "_sign", lambda response, url: {"PAYMENT-SIGNATURE": "s"})
    node.prices["/work/market/quote"] = 0.10
    assert recovered.call("/work/market/quote", {"product_id": "BTC-USD"})["status"] == "ok"


def test_unknown_settlement_is_reserved_and_never_automatically_retried(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    node.prices["/work/market/quote"] = 0.10
    node.unknown_after_payment = True
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=10, daily_cap_usd=0.10, home=home)
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.PaymentUncertain):
        buyer.call("/work/market/quote", {"product_id": "BTC-USD"},
                   idempotency_key="uncertain-purchase-1")
    with pytest.raises(R.PaymentUncertain):
        buyer.call("/work/market/quote", {"product_id": "BTC-USD"},
                   idempotency_key="uncertain-purchase-1")
    assert len(signed) == 1 and len(node.paid_headers) == 1 and buyer.spent_usd == 0
    fresh = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=10, daily_cap_usd=0.10, home=home)
    monkeypatch.setattr(fresh, "_sign", lambda response, url: {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.CapExceeded):
        fresh.call("/work/market/quote", {"product_id": "ETH-USD"})


def test_an_unknown_identity_is_not_signed_again_after_its_reservation_expires(node, tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    key = "unknown-past-expiry"
    node.prices["/work/market/quote"] = 0.10
    node.unknown_after_payment = True
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=1, home=home)
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    with pytest.raises(R.PaymentUncertain):
        buyer.call("/work/market/quote", {"product_id": "BTC-USD"}, idempotency_key=key)

    rows = []
    for line in buyer.ledger_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("expires_at"):
            row["expires_at"] = time.time() - 1
        rows.append(row)
    buyer.ledger_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    restarted = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                         budget_usd=1, daily_cap_usd=1, home=home)
    monkeypatch.setattr(restarted, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s2"})
    with pytest.raises(R.PaymentUncertain):
        restarted.call("/work/market/quote", {"product_id": "BTC-USD"}, idempotency_key=key)
    assert len(signed) == 1 and len(node.paid_headers) == 1


def test_concurrent_identical_keys_produce_one_signature_and_execution(node, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    node.paid_delay = 0.25
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=1, home=str(tmp_path / "home"))
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    barrier = threading.Barrier(2)

    def retry():
        barrier.wait()
        try:
            return buyer.call("/work/market/quote", {"product_id": "BTC-USD"},
                              idempotency_key="same-concurrent-request")
        except R.RouterError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: retry(), range(2)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, R.PaymentUncertain) for outcome in outcomes) == 1
    assert len(signed) == 1 and len(node.executions) == 1 and buyer.spent_usd == 0.02


def test_composite_and_its_component_are_refused_before_authorization(node, router):
    node.workers.extend([
        {"name": "market.intel", "path": "/work/market/intel", "price_usd": 5.0,
         "composes": ["market.quote", "market.prediction", "news.search", "llm.analyze"]},
        {"name": "market.prediction", "path": "/work/market/prediction", "price_usd": 0.05,
         "composes": []},
    ])
    with pytest.raises(R.VerificationFailed) as err:
        router.prepare_plan([
            {"tool": "market.intel", "payload": {"product_id": "BTC-USD"}},
            {"tool": "market.quote", "payload": {"product_id": "BTC-USD"}},
        ])
    assert err.value.extra["duplicate_components"] == ["market.quote"]
    assert router.signed == [] and node.executions == []


def test_workflow_total_is_reserved_before_any_component_is_signed(node, tmp_path, monkeypatch):
    node.prices["/work/market/quote"] = 0.10
    next(worker for worker in node.workers if worker["name"] == "market.quote")["price_usd"] = 0.10
    node.prices["/work/market/prediction"] = 0.05
    node.workers.append({"name": "market.prediction", "path": "/work/market/prediction",
                         "price_usd": 0.05, "composes": []})
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=0.10,
                     budget_usd=0.10, daily_cap_usd=10, home=str(tmp_path / "home"))
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    plan = buyer.prepare_plan([
        {"tool": "market.quote", "payload": {"product_id": "BTC-USD"}},
        {"tool": "market.prediction", "payload": {"query": "bitcoin"}},
    ])
    authorization = buyer.authorize_plan(plan, max_total_usd=0.15)
    with pytest.raises(R.CapExceeded) as err:
        buyer.execute_plan(plan, authorization)
    assert err.value.extra["cap"] == "process_budget"
    assert signed == [] and node.executions == []


def test_an_authorized_nonduplicative_plan_preserves_existing_purchase_behavior(node, tmp_path, monkeypatch):
    node.prices["/work/market/prediction"] = 0.05
    node.workers.append({"name": "market.prediction", "path": "/work/market/prediction",
                         "price_usd": 0.05, "composes": []})
    buyer = R.Router(node.url, evm_key="0x" + "1" * 64, rail="base", max_price_usd=1,
                     budget_usd=1, daily_cap_usd=1, home=str(tmp_path / "home"))
    signed = []
    monkeypatch.setattr(buyer, "_sign", lambda response, url: signed.append(url) or {"PAYMENT-SIGNATURE": "s"})
    plan = buyer.prepare_plan([
        {"tool": "market.quote", "payload": {"product_id": "BTC-USD"}},
        {"tool": "market.prediction", "payload": {"query": "bitcoin"}},
    ])
    authorization = buyer.authorize_plan(plan, max_total_usd=0.15)
    outputs = buyer.execute_plan(plan, authorization)
    assert [result["worker"] for result in outputs] == ["market.quote", "market.prediction"]
    assert len(signed) == 2 and buyer.spent_usd == 0.07
    assert len(node.executions) == 2


def test_a_plan_changed_after_authorization_never_signs(router, node):
    plan = router.prepare_plan([{"tool": "market.quote", "payload": {"product_id": "BTC-USD"}}])
    authorization = router.authorize_plan(plan, max_total_usd=0.02)
    plan["steps"][0]["payload"] = {"product_id": "ETH-USD"}
    with pytest.raises(R.VerificationFailed):
        router.execute_plan(plan, authorization)
    assert router.signed == [] and node.executions == []
