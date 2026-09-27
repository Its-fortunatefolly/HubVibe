"""The four zero-sign-up bees on the node's surfaces: traffic.route,
video.youtube, social.bluesky, social.mastodon.

Each is priced as designed, refuses bad input before the payment gate,
quotes a 402 at its catalog price with a runnable example, delivers the
route's envelope with a receipt over HTTP and over MCP (skill served from a
fixture), and is described identically by the served tool, /mcp.json and
the static manifests on disk. The credentialed two are advertised only
when their credential is present.
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
STATIC = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
BEES = {"traffic.route": ("/work/traffic/route", "hubvibe_traffic_route", 0.10, "google_routes"),
        "video.youtube": ("/work/video/youtube", "hubvibe_video_youtube", 0.05, "youtube"),
        "social.bluesky": ("/work/social/bluesky", "hubvibe_social_bluesky", 0.05, "bluesky"),
        "social.mastodon": ("/work/social/mastodon", "hubvibe_social_mastodon", 0.05, "mastodon")}
BAD = {"traffic.route": {"origin": "A", "destination": "B", "travel_mode": "TELEPORT"},
       "video.youtube": {"query": "x", "video_ids": ["dQw4w9WgXcQ"]},
       "social.bluesky": {"mode": "profile", "actor": "not a handle"},
       "social.mastodon": {"mode": "hashtag", "tag": "x", "instance": "localhost"}}


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


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("WORKER_YOUTUBE_API_KEY", "test-only-key")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_zero_signup", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports",
                        lambda version, network: True)
    # The Routes bee needs the Google credential; make it deliverable here.
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


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_the_rows_are_priced_keyless_where_promised_and_always_current():
    for name, (path, tool, price, provider) in BEES.items():
        w = W.catalog.BY_NAME[name]
        assert w.path == path and w.price_usd == price and w.tier == "utility"
        assert list(w.requires) == [provider]
        schema = W.catalog.contract.OUTPUT_SCHEMAS[name]
        assert "checked_at" in schema["required"] and "as_of" in schema["required"], name
        assert W.skills.PRECHECKS.get(w.skill) is not None
        jsonschema.validate(W.catalog.example_for(w), w.input_schema)
        assert len(w.description) <= 500
    assert W.catalog.BY_NAME["social.bluesky"].available() and W.catalog.BY_NAME["social.mastodon"].available()


def test_credentialed_bees_are_advertised_only_with_their_credential(monkeypatch):
    monkeypatch.delenv("WORKER_YOUTUBE_API_KEY", raising=False)
    assert W.catalog.BY_NAME["video.youtube"].available() is False
    monkeypatch.setenv("WORKER_YOUTUBE_API_KEY", "k")
    assert W.catalog.BY_NAME["video.youtube"].available() is True
    monkeypatch.setattr(W.providers.google_auth, "configured", lambda: False)
    assert W.catalog.BY_NAME["traffic.route"].available() is False


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    for name, (path, tool, price, provider) in BEES.items():
        response = client.post(path, headers={"X-API-Key": "test-key"}, json=BAD[name])
        assert response.status_code == 400, (path, response.text)
        assert response.json()["reason"] == "invalid_request" and response.json()["billed"] is False


def test_unpaid_calls_are_402s_at_the_catalog_prices_with_runnable_examples(client):
    for name, (path, tool, price, provider) in BEES.items():
        response = client.post(path, json=W.catalog.example_for(W.catalog.BY_NAME[name]))
        assert response.status_code == 402, (path, response.text)
        body = response.json()
        accepts = body["accepts"][0]
        assert int(accepts.get("maxAmountRequired") or accepts.get("amount")) == round(price * 1_000_000)
        example = body["extensions"]["bazaar"]["info"]["input"]["body"]
        jsonschema.validate(example, W.catalog.BY_NAME[name].input_schema)


class _Ctx:
    def __init__(self, answers):
        self.answers = answers
        self.steps, self.providers_used, self.attempts = [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            def __getattr__(self, name):
                async def method(*args, **kw):
                    return W.runtime.ProviderResult(value=outer.answers[name], cost_micros=0, cost_measured=True)
                return method

        result = await call(_P())
        self.providers_used.append(providers[0].id)
        return result.value


def _serve_bluesky_from_fixture(monkeypatch):
    feed = {"feed": [{"post": {"uri": "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3abc", "cid": "c",
                               "author": {"did": "did:plc:z72i7hdynmk6r22z27h6tvur", "handle": "bsky.app"},
                               "record": {"text": "hi", "createdAt": "2026-09-26T20:00:00.000Z"},
                               "likeCount": 1, "indexedAt": "2026-09-26T20:00:00.000Z"}}]}
    profile = {"did": "did:plc:z72i7hdynmk6r22z27h6tvur", "handle": "bsky.app", "displayName": "Bluesky"}

    async def skill(ctx, payload):
        return await W.skills.bluesky.social(_Ctx({"profile": profile, "author_feed": feed}), payload)
    registry = dict(W.router.REGISTRY)
    registry["social.bluesky"] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


def test_a_paid_bluesky_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    _serve_bluesky_from_fixture(monkeypatch)
    body = {"mode": "profile", "actor": "bsky.app", "posts": 1}
    response = client.post("/work/social/bluesky", headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == "social.bluesky" and envelope["price_usd"] == 0.05
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME["social.bluesky"]))
    assert envelope["result"]["posts"][0]["url"] == "https://bsky.app/profile/bsky.app/post/3abc"
    receipt = client.get(envelope["receipt_url"]).json()
    assert receipt["request"]["worker"] == "social.bluesky" and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "hubvibe_social_bluesky", "arguments": body}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["profile"]["handle"] == "bsky.app"


def test_the_tools_are_listed_and_the_static_manifests_match(app_module, client):
    live = {t["name"]: t for t in app_module._mcp_tools()}
    manifest = {t["name"]: t for t in client.get("/mcp.json").json()["tools"]}
    for name, (path, tool, price, provider) in BEES.items():
        worker = W.catalog.BY_NAME[name]
        assert live[tool]["title"] == worker.title and f"${price:.2f} per call" in live[tool]["description"]
        assert live[tool]["outputSchema"] == W.catalog.response_schema(worker)
        assert manifest[tool]["httpEndpoint"] == {"method": "POST", "path": path, "price_usd": price}
        for static in (STATIC / "mcp.json", REPO_ROOT / "glama.json"):
            on_disk = next(t for t in json.loads(static.read_text())["tools"] if t["name"] == tool)
            for field in ("title", "description", "inputSchema", "outputSchema", "annotations"):
                assert on_disk[field] == live[tool][field], f"{static.name} {tool} {field} is stale"
    card = client.get("/.well-known/agent-card.json").json()
    names = {s.get("id") or s.get("name") for s in card["skills"]}
    assert {"hubvibe_traffic_route", "hubvibe_video_youtube", "hubvibe_social_bluesky", "hubvibe_social_mastodon"} <= names | {s.get("name") for s in card["skills"]}
