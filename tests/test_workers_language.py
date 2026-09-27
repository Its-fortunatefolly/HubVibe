"""Language is not a barrier: every worker whose answer contains prose takes
an optional `language` (BCP-47) and answers in it.

Pinned: the tag is validated once (skills/llm.py) and a bad one is refused
before the payment gate; the three inference skills put the rule into the
model's system instruction; the composites, monitor.check, verify.claims,
data.question and search.web pass it through; and every such catalog row
declares the property, since the schemas forbid unknown fields.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"

PROSE_WORKERS = [
    "llm.analyze", "llm.extract", "llm.generate", "search.web", "data.question",
    "research.brief", "research.page_facts", "market.intel", "research.web",
    "research.company", "verify.claims", "monitor.check", "commerce.availability",
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

@pytest.fixture(autouse=True)
def _target_guard(monkeypatch):
    """main.py injects the audit engine's SSRF guard into the web provider at
    startup; these unit tests run without main, so give the provider a guard
    with the same shape (refuse link-local/private, allow the public web)."""
    def guard(url):
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if host.startswith(("169.254.", "10.", "127.", "localhost", "192.168.")):
            return "must not point at a private, loopback or link-local address"
        return None
    monkeypatch.setattr(W.providers.web, "_blocked_target_reason", guard)


class _Ctx:
    """Runs the step against one stub provider that records the system
    instruction it was given."""

    def __init__(self, text="answer", as_json=None):
        self.systems, self.prompts = [], []
        self.text, self.as_json = text, as_json

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        outer = self

        class _Provider:
            id = "fake"
            provider_name = "gemini"
            models = ["fake-model"]

            def available(self):
                return True

            def matches(self, provider, model):
                return True

            async def generate(self, prompt, system=None, *args, **kwargs):
                outer.prompts.append(prompt)
                outer.systems.append(system)
                return W.runtime.ProviderResult(
                    value={"text": outer.text, "model": "fake-model", "provider": "gemini",
                           "prompt_tokens": 1, "output_tokens": 1, "finish_reason": "STOP"},
                    cost_micros=0, cost_measured=True)

            async def generate_json(self, prompt, system=None, temperature=0.2):
                outer.prompts.append(prompt)
                outer.systems.append(system)
                return W.runtime.ProviderResult(
                    value={"json": outer.as_json, "model": "fake-model",
                           "prompt_tokens": 1, "output_tokens": 1, "text": "{}"},
                    cost_micros=0, cost_measured=True)

            async def search(self, query, language=None):
                outer.prompts.append(W.providers.search_grounding.prompt_for(query, language))
                return W.runtime.ProviderResult(
                    value={"answer": outer.text, "sources": [], "model": "fake", "queries": []},
                    cost_micros=0, cost_measured=True)

        result = await call(_Provider())
        return result.value


# --- the tag ----------------------------------------------------------------------

def test_the_language_tag_is_validated_once():
    validate = W.skills.llm.validate_language
    assert validate({}) is None and validate({"language": None}) is None
    for ok in ("en", "ja", "pt-BR", "zh-Hant", "sr-Latn-RS", " fr "):
        assert validate({"language": ok}) == ok.strip()
    for bad in ("english", "", "e", "en_US", 12, "a" * 40, "en-"):
        with pytest.raises(W.runtime.InvalidRequest):
            validate({"language": bad})


def test_the_rule_is_appended_to_the_system_instruction_only_when_asked():
    in_language = W.skills.llm.in_language
    assert in_language("Be precise.", None) == "Be precise."
    assert in_language(None, None) is None
    with_rule = in_language("Be precise.", "ja")
    assert with_rule.startswith("Be precise.") and "'ja'" in with_rule
    assert "exactly as they appear" in with_rule
    assert "'pt-BR'" in in_language(None, "pt-BR")


# --- the inference skills ----------------------------------------------------------

def test_analyze_extract_and_generate_tell_the_model_the_language():
    llm = W.skills.llm
    ctx = _Ctx()
    asyncio.run(llm.analyze(ctx, {"text": "material", "question": "q", "language": "ja"}))
    assert "'ja'" in ctx.systems[-1] and ctx.systems[-1].startswith(llm._ANALYST)

    ctx = _Ctx(as_json={"title": "x"})
    asyncio.run(llm.extract_structured(ctx, {"text": "material", "fields": ["title"], "language": "de"}))
    assert "'de'" in ctx.systems[-1]

    ctx = _Ctx()
    asyncio.run(llm.generate(ctx, {"prompt": "hi", "system": "Be brief.", "language": "es"}))
    assert ctx.systems[-1].startswith("Be brief.") and "'es'" in ctx.systems[-1]

    ctx = _Ctx()
    asyncio.run(llm.analyze(ctx, {"text": "material"}))
    assert ctx.systems[-1] == llm._ANALYST  # nothing appended when not asked


def test_a_bad_tag_is_refused_by_the_skill_itself():
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.skills.llm.analyze(_Ctx(), {"text": "m", "language": "klingon-speak"}))


# --- pass-through ------------------------------------------------------------------

def test_the_composites_and_monitor_pass_the_language_to_the_inference_step(monkeypatch):
    captured = []

    async def fake_analyze(ctx, payload):
        captured.append(payload.get("language"))
        return {"answer": "a", "model": "m", "question": payload.get("question"),
                "input_chars": 1, "tokens": {"prompt": 1, "output": 1}}

    async def fake_extract_structured(ctx, payload):
        captured.append(payload.get("language"))
        return {"fields": {f: None for f in payload["fields"]}, "model": "m",
                "tokens": {"prompt": 1, "output": 1}}

    async def fake_page(ctx, payload):
        return {"url": payload["url"], "final_url": payload["url"], "title": "T", "text": "body",
                "text_chars": 4, "truncated": False, "links": [], "javascript_rendered": False}

    async def fake_quote(ctx, payload):
        return {"product_id": "BTC-USD", "price": "1", "quote_currency": "USD",
                "price_change_24h_pct": "0", "volume_24h": "0"}

    async def fake_markets(ctx, payload):
        return {"count": 0, "markets": []}

    async def fake_search(ctx, payload):
        return {"answer": "a", "sources": [{"url": "https://s.example/a", "title": "A"}]}

    monkeypatch.setattr(W.skills.llm, "analyze", fake_analyze)
    monkeypatch.setattr(W.skills.llm, "extract_structured", fake_extract_structured)
    monkeypatch.setattr(W.skills.extract, "extract_page", fake_page)
    monkeypatch.setattr(W.skills.market, "quote", fake_quote)
    monkeypatch.setattr(W.skills.market, "prediction_markets", fake_markets)
    monkeypatch.setattr(W.skills.search, "web_search", fake_search)

    c = W.skills.composites
    asyncio.run(c.research_brief(_Ctx(), {"url": "https://example.com", "language": "ja"}))
    asyncio.run(c.page_facts(_Ctx(), {"url": "https://example.com", "fields": ["a"], "language": "ko"}))
    asyncio.run(c.market_intel(_Ctx(), {"language": "fr"}))
    asyncio.run(c.research_web(_Ctx(), {"question": "q", "language": "it"}))
    asyncio.run(c.research_company(_Ctx(), {"company": "Acme", "language": "nl"}))
    assert captured == ["ja", "ko", "fr", "it", "nl"]

    W.ledger.reset_for_tests()
    W.ledger.save_monitor_snapshot("https://example.com", "oldhash", "old text", "T")
    asyncio.run(W.skills.monitor.check(_Ctx(), {"url": "https://example.com", "language": "de"}))
    assert captured[-1] == "de"


def test_verify_claims_and_search_web_carry_the_language(monkeypatch):
    async def fake_page(ctx, payload):
        return {"url": payload["url"], "final_url": payload["url"], "title": "T", "text": "the sky is blue",
                "text_chars": 15, "truncated": False, "links": [], "javascript_rendered": False}

    monkeypatch.setattr(W.skills.extract, "extract_page", fake_page)
    ctx = _Ctx(as_json={"verdicts": [{"claim": "sky is blue", "verdict": "SUPPORTED",
                                      "quote": "the sky is blue", "source_n": 1}]})
    asyncio.run(W.skills.verify.verify_claims(ctx, {
        "claims": ["sky is blue"], "sources": ["https://example.com"], "language": "ja"}))
    assert "'ja'" in ctx.systems[-1] and ctx.systems[-1].startswith(W.skills.verify._VERIFIER)

    ctx = _Ctx()
    asyncio.run(W.skills.search.web_search(ctx, {"query": "x402", "language": "pt-BR"}))
    assert "'pt-BR'" in ctx.prompts[-1]
    assert W.providers.search_grounding.prompt_for("x402", None).endswith("x402")


# --- the catalog and the gate ---------------------------------------------------------

def test_every_prose_worker_declares_the_language_property():
    for name in PROSE_WORKERS:
        worker = W.catalog.BY_NAME[name]
        prop = worker.input_schema["properties"].get("language")
        assert prop and prop["type"] == "string" and "pattern" in prop, name
        assert "language" not in (worker.input_schema.get("required") or []), name
        assert W.skills.PRECHECKS.get(worker.skill) is not None, f"{name} has no free precheck"


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_language", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    monkeypatch.setattr(W.providers.google_auth, "configured", lambda: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


def test_a_bad_language_is_a_free_400_before_the_payment_gate(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    client = TestClient(app_module.app)
    for path, body in (("/work/llm/analyze", {"text": "m", "language": "english"}),
                       ("/work/research/brief", {"url": "https://example.com", "language": "en_US"}),
                       ("/work/search/web", {"query": "q", "language": ""})):
        response = client.post(path, headers={"X-API-Key": "test-key"}, json=body)
        assert response.status_code == 400, (path, response.text)
        assert response.json()["reason"] == "invalid_request"
        assert response.json()["billed"] is False
