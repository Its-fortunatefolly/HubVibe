"""Every 402 fits the header limits common buyer clients enforce, and the
Bazaar still gets the full record.

Measured live 2026-09-29: Python's aiohttp refused the PAYMENT-REQUIRED
header on 60 of 68 routes ("Got more than 8190 bytes when reading") and
Node's fetch refused 12 (UND_ERR_HEADERS_OVERFLOW) -- those buyers never saw
the price. The header now carries a slimmed Bazaar record under
HEADER_BUDGET; the full one is put back on the payment before the
facilitator (which catalogues from the payment's `extensions`) sees it.
"""

import base64
import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
AIOHTTP_MAX_LINE = 8190
NODE_MAX_BLOCK = 16384


_SIBLINGS = ("billing", "x402_payments", "mpp_payments", "audits")


def _drop_sibling_cache():
    import sys

    for name in _SIBLINGS:
        sys.modules.pop(f"wcag_audit_engine_{name}", None)


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    # main.py caches x402_payments process-wide and reads its env once; load
    # a fresh copy under this test's env and leave none behind for the next.
    _drop_sibling_cache()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://hubvibe-io.com")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd")
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    spec = importlib.util.spec_from_file_location("wcag_audit_main_header_budget", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    monkeypatch.setattr(module.workers.catalog.Worker, "available", lambda self: True)
    yield module
    _drop_sibling_cache()


def _challenge(response) -> dict:
    return json.loads(base64.b64decode(response.headers["payment-required"] + "==="))


def test_every_route_402_fits_aiohttp_and_node_limits(app_module):
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    paths = [w.path for w in app_module.workers.catalog.CATALOG] + [
        "/audit/wcag", "/audit/bundle", "/audit/seo", "/audit/security", "/audit/performance"]
    over = []
    for path in paths:
        response = client.post(path, json={})
        assert response.status_code == 402, path
        assert "payment-required" in response.headers, path
        line = max(len(k) + 2 + len(v) for k, v in response.headers.items())
        block = sum(len(k) + len(v) + 4 for k, v in response.headers.items())
        if line > AIOHTTP_MAX_LINE or block > NODE_MAX_BLOCK:
            over.append((path, line, block))
        challenge = _challenge(response)
        assert challenge["accepts"] and challenge["resource"]["url"].endswith(path), path
        info = ((challenge.get("extensions") or {}).get("bazaar") or {}).get("info") or {}
        assert info.get("input", {}).get("method") == "POST", path  # the record still says how to call it
    assert over == [], over


def test_the_full_record_is_restored_on_the_payment(app_module):
    from fastapi.testclient import TestClient
    from x402.schemas import PaymentPayload

    X = app_module.x402_payments
    response = TestClient(app_module.app).post("/work/stats/probability", json={})
    slim = _challenge(response)
    url = slim["resource"]["url"]
    full = X._FULL_DISCOVERY[url]
    assert len(json.dumps(full)) > len(json.dumps(slim["extensions"]["bazaar"]))
    payload = PaymentPayload.model_validate({
        "x402Version": 2, "payload": {"signature": "0x00", "authorization": {}},
        "accepted": slim["accepts"][0], "resource": slim["resource"], "extensions": slim["extensions"]})
    X.restore_full_discovery(payload)
    assert payload.extensions["bazaar"] == full
    # The facilitator's own extractor now sees the full, valid record.
    from x402.extensions.bazaar.facilitator import extract_discovery_info
    found = extract_discovery_info(payload, slim["accepts"][0])
    assert found is not None and found.resource_url == url


def test_restore_leaves_other_payments_alone(app_module):
    from x402.schemas import PaymentPayload

    X = app_module.x402_payments
    base = {"x402Version": 2, "payload": {"signature": "0x00"},
            "accepted": {"scheme": "exact", "network": "eip155:8453", "asset": "0x0", "amount": "1",
                         "payTo": "0x0", "maxTimeoutSeconds": 60}}
    unknown = PaymentPayload.model_validate(dict(base, resource={"url": "https://elsewhere.example/x"},
                                                 extensions={"bazaar": {"info": {}}}))
    X.restore_full_discovery(unknown)
    assert unknown.extensions == {"bazaar": {"info": {}}}
    bare = PaymentPayload.model_validate(dict(base, resource={"url": "https://hubvibe-io.com/work/stats/probability"}))
    X.restore_full_discovery(bare)
    assert not bare.extensions


def test_slimming_steps_stop_as_soon_as_it_fits(app_module):
    X = app_module.x402_payments
    record = {"bazaar": {"info": {"input": {"type": "http", "method": "POST"}, "output": {"example": {"a": 1}}},
                         "schema": {"type": "object", "properties": {"output": {"type": "object", "description": "x" * 5000,
                                                                               "properties": {"a": {"type": "integer",
                                                                                                    "description": "y" * 5000}}}}}}}
    slim = X.slim_discovery(record, lambda ext: len(json.dumps(ext)) < 1000)
    assert slim["bazaar"]["info"]["output"] == {"example": {"a": 1}}  # descriptions went, the example stayed
    assert "description" not in json.dumps(slim)
    assert X.slim_discovery(record, lambda ext: True) is record
