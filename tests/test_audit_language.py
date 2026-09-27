"""No language barrier on the site audits, and Cloud Translation first.

Pinned: every audit route and the MCP audit tools accept `language`; a
malformed tag is a free 400 before the payment gate; the findings' `detail`
and the violations' `help` come back translated while ids, severities,
URLs and numbers stay as they are; no `language` means no translation step;
a translation that cannot be delivered fails the audit unbilled; the
translation layer uses Cloud Translation first and the model only when
Translation is unavailable or refuses the target; the published schemas
(openapi, MCP) carry the field.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"

SEO = {"status": "ok", "pass": False, "checks": "title, meta-description",
       "findings": [{"id": "missing-meta-description", "severity": "critical", "detail": "No meta description found"},
                    {"id": "title-too-long", "severity": "moderate", "detail": "Title is 72 characters (recommended <= 60)"}]}
AXE = {"violations": [{"id": "image-alt", "impact": "critical", "help": "Images must have alternate text",
                       "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/image-alt", "nodes": [{}, {}]}]}
PERF = {"status": "ok", "pass": True, "findings": [], "metrics": {}}


TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


def _drop_sibling_cache():
    """main.py caches its sibling modules (payments, billing...) in
    sys.modules, and some read their configuration at import. Loading main
    here with this file's environment must not leave that cache configured
    for the next test file, and must not inherit the previous file's."""
    for name in [n for n in sys.modules if n.startswith("wcag_audit_engine_") and n != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _isolated_siblings():
    _drop_sibling_cache()
    yield
    _drop_sibling_cache()


def _load(monkeypatch):
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_METERED_PRICE_ID", "STRIPE_FLAT_SUBSCRIPTION_PRICE_ID",
                "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    spec = importlib.util.spec_from_file_location("wcag_audit_main_lang", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    return module


def _fake_translate(calls):
    async def translate(ctx, strings, language):
        calls.append((tuple(strings), language))
        return [f"[{language}] {s}" for s in strings]
    return translate


def _patched(module):
    return (patch.object(module, "_run_axe", return_value=AXE),
            patch.object(module, "_run_axe_and_performance", return_value=(AXE, PERF)),
            patch.object(module.audits, "fetch_once", return_value=object()),
            patch.object(module.audits, "run_seo_audit", return_value=json.loads(json.dumps(SEO))),
            patch.object(module.audits, "run_security_audit", return_value={"status": "ok", "pass": True, "findings": []}),
            patch.object(module.audits, "run_performance_audit", return_value=PERF))


def test_seo_findings_come_back_translated_and_data_untouched(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", _fake_translate(calls))
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        r = client.post("/audit/seo", json={"url": "https://example.com", "language": "ja"}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 200, r.text
    findings = r.json()["findings"]
    assert findings[0] == {"id": "missing-meta-description", "severity": "critical", "detail": "[ja] No meta description found"}
    assert findings[1]["detail"].startswith("[ja] Title is 72") and r.json()["checks"] == "title, meta-description"
    assert calls == [(("No meta description found", "Title is 72 characters (recommended <= 60)"), "ja")]


def test_wcag_help_is_translated_and_the_rule_link_is_not(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", _fake_translate(calls))
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        r = client.post("/audit/wcag", json={"html": "<img src=x>", "language": "ko"}, headers={"X-API-Key": "test-key"})
        legacy = client.post("/audit", json={"html": "<img src=x>", "language": "ko"}, headers={"X-API-Key": "test-key"})
    v = r.json()["violations"][0]
    assert v["help"] == "[ko] Images must have alternate text" and v["help_url"].startswith("https://dequeuniversity.com/")
    assert v["id"] == "image-alt" and v["impact"] == "critical" and v["nodes_affected"] == 2
    assert legacy.status_code == 200 and legacy.json()["violations"][0]["help"].startswith("[ko] ")


def test_the_bundle_translates_every_dimension(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", _fake_translate(calls))
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        r = client.post("/audit/bundle", json={"url": "https://example.com", "language": "de"}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wcag"]["violations"][0]["help"].startswith("[de] ") and body["seo"]["findings"][0]["detail"].startswith("[de] ")
    assert len(calls) == 1 and len(calls[0][0]) == 3


def test_no_language_means_no_translation_step(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", _fake_translate(calls))
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        r = client.post("/audit/seo", json={"url": "https://example.com"}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 200 and r.json()["findings"][0]["detail"] == "No meta description found" and calls == []


def test_a_malformed_language_is_a_free_400_before_the_gate(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)

    def refuse(*a, **k):
        raise AssertionError("the payment gate ran for a request the language check refuses")
    monkeypatch.setattr(module, "_authorize_and_rate_limit", refuse)
    client = TestClient(module.app)
    for path, body in (("/audit/seo", {"url": "https://example.com", "language": "not a tag!"}),
                       ("/audit/security", {"url": "https://example.com", "language": "x"}),
                       ("/audit/bundle", {"url": "https://example.com", "language": "12"})):
        r = client.post(path, json=body, headers={"X-API-Key": "test-key"})
        assert r.status_code == 400 and r.json()["billed"] is False, (path, r.text)


def test_a_translation_that_cannot_be_delivered_fails_the_audit_unbilled(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)

    async def broken(ctx, strings, language):
        raise module.workers.runtime.TransientProviderError("both translation engines timed out")
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", broken)
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        r = client.post("/audit/seo", json={"url": "https://example.com", "language": "ja"}, headers={"X-API-Key": "test-key"})
    assert r.status_code == 502 and r.json()["billed"] is False and "could not be delivered in ja" in r.json()["detail"]


def test_the_mcp_audit_tool_takes_language_and_validates_it(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.workers.skills.localize, "translate_strings", _fake_translate(calls))
    p = _patched(module)
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        client = TestClient(module.app)
        ok = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "audit_seo", "arguments": {"url": "https://example.com", "language": "pt-BR"}}})
        bad = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "audit_seo", "arguments": {"url": "https://example.com", "language": "not a tag!"}}})
    result = ok.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["findings"][0]["detail"].startswith("[pt-BR] ")
    assert bad.json()["error"]["code"] == -32602 and "BCP-47" in bad.json()["error"]["message"]


def test_the_published_schemas_carry_language(monkeypatch):
    from fastapi.testclient import TestClient
    module = _load(monkeypatch)
    client = TestClient(module.app)
    openapi = client.get("/openapi.json").json()
    schemas = openapi["components"]["schemas"]
    assert "language" in schemas["AuditRequest"]["properties"] and "language" in schemas["UrlAuditRequest"]["properties"]
    tools = {t["name"]: t for t in module._mcp_tools()}
    for name in ("audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"):
        assert "language" in tools[name]["inputSchema"]["properties"], name
    static = json.loads((REPO_ROOT / "wcag-audit-engine" / "app" / "static" / "mcp.json").read_text())
    for t in static["tools"]:
        if t["name"].startswith("audit_"):
            assert "language" in t["inputSchema"]["properties"], t["name"]


# --- the engine order ----------------------------------------------------------

class _EngineCtx:
    def __init__(self, translate_answer):
        self.translate_answer = translate_answer
        self.steps = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append((step, providers[0].id))
        outer = self

        class _P:
            id = providers[0].id

            async def translate(self, strings, language):
                if isinstance(outer.translate_answer, Exception):
                    raise outer.translate_answer
                return outer.translate_answer_type(strings, language)

            async def generate_json(self, prompt, system=None, temperature=0.2):
                strings = json.loads(prompt)["strings"]
                return type("R", (), {"value": {"json": {"translations": ["<model> " + x for x in strings]}}})()

        result = await call(_P())
        return result.value if hasattr(result, "value") else result

    @staticmethod
    def translate_answer_type(strings, language):
        return type("R", (), {"value": [f"<mt:{language}> " + x for x in strings]})()


def _workers():
    for name, mod in sys.modules.items():
        if name.endswith("workers") and hasattr(mod, "skills") and hasattr(mod.skills, "localize"):
            return mod
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", REPO_ROOT / "wcag-audit-engine" / "app" / "workers" / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT / "wcag-audit-engine" / "app" / "workers")])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


def test_cloud_translation_runs_first_and_the_model_only_when_it_cannot():
    W = _workers()
    L = W.skills.localize
    ctx = _EngineCtx(translate_answer="ok")
    out = asyncio.run(L.translate_strings(ctx, ["A sentence here.", "Another one."], "ja"))
    assert out == ["<mt:ja> A sentence here.", "<mt:ja> Another one."] and [s for s, _ in ctx.steps] == ["translate"]
    assert ctx.steps[0][1] == "google-translate-llm"
    ctx = _EngineCtx(translate_answer=W.runtime.PermanentProviderError("Cloud Translation returned 400: Target language is invalid"))
    out = asyncio.run(L.translate_strings(ctx, ["A sentence here."], "tlh"))
    assert out == ["<model> A sentence here."] and [s for s, _ in ctx.steps] == ["translate", "localize"]


def test_target_codes_map_script_tags_to_the_models_regions():
    W = _workers()
    T = W.providers.translate
    assert T.target_code("zh-Hant") == "zh-TW" and T.target_code("zh-Hans") == "zh-CN" and T.target_code("ja") == "ja"
    assert T.PROVIDERS[0].id == "google-translate-llm" and T.MODEL == "general/translation-llm"
