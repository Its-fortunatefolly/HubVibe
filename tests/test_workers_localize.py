"""No language barrier: every worker takes `language`; the ones that do not
write prose natively get their notes, details and reasons translated on
the job, before the delivery contract, with cost on the same receipt.

Pinned: which strings count as prose (never URLs, ids, dates, numbers);
nested paths and lists; chunking by size; a count mismatch from the model
refused; the router translating a data bee's notes and leaving data alone;
a malformed tag refused free on any route; every catalog row advertising
`language`; the static manifests carrying it.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


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
L = W.skills.localize


class _Ctx:
    """A model that 'translates' by tagging, or answers with the wrong count."""

    def __init__(self, wrong_count=False):
        self.wrong_count = wrong_count
        self.calls = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        outer = self

        class _P:
            id = "fake-gemini"

            async def generate_json(self, prompt, system=None, temperature=0.2):
                strings = json.loads(prompt)["strings"]
                outer.calls.append((step, len(strings), system))
                out = ["<ja>" + s for s in strings]
                if outer.wrong_count:
                    out = out[:-1]
                return W.runtime.ProviderResult(value={"json": {"translations": out}, "model": "fake"}, cost_micros=7, cost_measured=True)

        return (await call(_P())).value


RESULT = {
    "indicator": {"alias": "inflation", "code": "FP.CPI.TOTL.ZG", "name": "Inflation, consumer prices (annual %)"},
    "observations": [{"period": "2025", "value": 3.17}],
    "notes": ["1 of 5 periods have no published value yet.", "https://example.com/x", "2026-09-27", "ok"],
    "nested": {"reason": "The portal timed out after 20 seconds.", "detail": None, "id": "abc123"},
    "portals_failed": [{"portal": "data.gov.hk", "reason": "data.gov.hk timed out"}],
    "source_url": "https://api.worldbank.org/v2/country/JP",
    "checked_at": "2026-09-27T12:00:00Z",
}


def test_only_prose_under_prose_keys_is_collected():
    found = L.collect(RESULT)
    assert [p for p, _ in found] == [("notes", 0), ("nested", "reason"), ("portals_failed", 0, "reason")]
    assert not L._is_prose("https://x") and not L._is_prose("0x9e61e3fce") and not L._is_prose("2026-09-27T12:00:00Z")
    assert not L._is_prose("12,345.6") and not L._is_prose("ok") and L._is_prose("The sky is blue.")


def test_apply_translates_prose_in_place_and_leaves_data_alone():
    ctx = _Ctx()
    out = asyncio.run(L.apply(ctx, RESULT, "ja"))
    assert out["notes"] == ["<ja>1 of 5 periods have no published value yet.", "https://example.com/x", "2026-09-27", "ok"]
    assert out["nested"]["reason"].startswith("<ja>") and out["nested"]["id"] == "abc123"
    assert out["portals_failed"][0]["reason"] == "<ja>data.gov.hk timed out"
    assert out["indicator"] == RESULT["indicator"] and out["observations"] == RESULT["observations"]
    assert RESULT["notes"][0].startswith("1 of 5"), "the input is not mutated"
    assert ctx.calls[0][0] == "localize" and ctx.calls[0][1] == 3 and "ja" in ctx.calls[0][2]
    assert asyncio.run(L.apply(ctx, {"a": 1}, "ja")) == {"a": 1} and asyncio.run(L.apply(ctx, RESULT, None)) is RESULT


def test_batches_are_chunked_by_size_and_a_wrong_count_is_refused():
    strings = ["x" * 5000, "y" * 5000, "z" * 5000]
    assert [len(c) for c in L.chunks(strings, limit=12000)] == [2, 1]
    ctx = _Ctx()
    out = asyncio.run(L.translate_strings(ctx, strings, "de"))
    assert len(out) == 3 and len(ctx.calls) == 2
    with pytest.raises(W.runtime.InvalidProviderResponse):
        asyncio.run(L.translate_strings(_Ctx(wrong_count=True), ["a sentence here", "another one"], "de"))


def test_every_worker_advertises_language_and_the_split_is_explicit():
    for w in W.catalog.CATALOG:
        assert "language" in w.input_schema["properties"], w.name
    assert W.catalog.NATIVE_LANGUAGE_WORKERS <= {w.name for w in W.catalog.CATALOG}
    assert "data.macro" in W.catalog.LOCALIZED_WORKERS and "llm.analyze" not in W.catalog.LOCALIZED_WORKERS
    assert "translated" in W.catalog.BY_NAME["data.macro"].input_schema["properties"]["language"]["description"]


# --- through the router --------------------------------------------------------

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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_localize", MAIN_PATH)
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


def test_a_data_bee_called_in_japanese_has_its_notes_translated_and_its_data_untouched(client, monkeypatch):
    macro_result = {
        "indicator": {"alias": "inflation", "code": "FP.CPI.TOTL.ZG", "name": "Inflation, consumer prices (annual %)", "unit": None},
        "country": {"code": "JP", "name": "Japan"}, "frequency": "annual", "source": "world-bank",
        "observations": [{"period": "2024", "value": 2.74}, {"period": "2025", "value": None}], "observation_count": 2,
        "latest": {"period": "2024", "value": 2.74}, "previous": None, "change": None, "as_of": "2026-07-13",
        "source_url": "https://api.worldbank.org/v2/country/JP/indicator/FP.CPI.TOTL.ZG?format=json",
        "notes": ["1 of 2 periods have no published value yet."], "checked_at": "2026-09-27T12:00:00Z"}

    async def skill(ctx, payload):
        return json.loads(json.dumps(macro_result))

    async def fake_translate(ctx, strings, language):
        return [f"[{language}] " + s for s in strings]

    registry = dict(W.router.REGISTRY)
    registry["data.macro"] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)
    monkeypatch.setattr(W.skills.localize, "translate_strings", fake_translate)
    r = client.post("/work/data/macro", headers={"X-API-Key": "test-key"}, json={"indicator": "inflation", "country": "JP", "language": "ja"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["notes"] == ["[ja] 1 of 2 periods have no published value yet."]
    assert result["latest"] == {"period": "2024", "value": 2.74} and result["indicator"]["name"].startswith("Inflation")
    r = client.post("/work/data/macro", headers={"X-API-Key": "test-key"}, json={"indicator": "inflation", "country": "JP"})
    assert r.status_code == 200 and r.json()["result"]["notes"] == ["1 of 2 periods have no published value yet."]


def test_a_malformed_language_tag_is_refused_free_on_any_route(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the language check refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    r = client.post("/work/market/quote", headers={"X-API-Key": "test-key"}, json={"product_id": "BTC-USD", "language": "not a tag!"})
    assert r.status_code == 400 and r.json()["billed"] is False and "BCP-47" in r.json()["detail"]


def test_the_static_manifests_advertise_language_on_every_tool(app_module):
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    worker_tools = [t for t in static["tools"] if t["name"].startswith("hubvibe_") and t.get("httpEndpoint", {}).get("path", "").startswith("/work/")]
    assert worker_tools and all("language" in t["inputSchema"]["properties"] for t in worker_tools)
