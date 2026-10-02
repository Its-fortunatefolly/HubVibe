"""Deliver later: a job still running at the caller's limit is a job to
collect, never a timeout -- and it is charged only when it delivers.

Routers' clients give up at 30 s (Mercator's gateway, 2026-10-01) and a Veo
video takes 30-45 s. The rules under test are the ones that cost money if
they are wrong:
  * a slow job answers 202 with a job to collect, and nothing is charged yet
  * it is charged exactly once, when the result is ready
  * a slow job that fails is never charged
  * a fast job is untouched (delivered inline, as before)
  * a job a restart interrupted is closed unbilled, its MPP payment released
  * MCP hands the 202 to the agent as a result, not as "nothing was charged"
"""

import asyncio
import importlib.util
import sys
import time
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
    spec = importlib.util.spec_from_file_location("wcag_audit_main_deliver_later", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    yield module
    W.ledger.reset_for_tests()


def _valid(name, **extra):
    return {**W.catalog.output_example(W.catalog.BY_NAME[name]), **extra}


def _wire(app_module, monkeypatch, skill, billed):
    registry = dict(W.router.REGISTRY)
    registry["market.quote"] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)

    def counting_bill(auth, price_usd):
        billed.append(price_usd)
        return app_module._bill(auth, price_usd)

    W.router.configure(
        authorize_and_rate_limit=app_module._authorize_and_rate_limit,
        bill=counting_bill, deliver=app_module._deliver,
        failed_response=app_module._failed_audit_response)


def _collect(client, job_id, until_not=202, seconds=5.0):
    deadline = time.monotonic() + seconds
    while True:
        response = client.get(f"/work/jobs/{job_id}")
        if response.status_code != until_not or time.monotonic() > deadline:
            return response
        time.sleep(0.05)


def test_a_slow_job_is_handed_back_and_charged_only_when_it_delivers(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    billed = []

    async def slow(ctx, payload):
        await asyncio.sleep(0.6)
        return _valid("market.quote", marker="slow-one")

    _wire(app_module, monkeypatch, slow, billed)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                            json={"product_id": "BTC-USD"})
        assert first.status_code == 202
        body = first.json()
        assert body["status"] == "processing" and body["billed"] is False
        assert body["collect_url"] == f"/work/jobs/{body['job_id']}"
        assert first.headers["Location"] == body["collect_url"]
        assert billed == [], "nothing may be charged before the result exists"

        running = client.get(body["collect_url"])
        assert running.status_code == 202 and running.json()["billed"] is False

        done = _collect(client, body["job_id"])
        assert done.status_code == 200
        delivered = done.json()
        assert delivered["result"]["marker"] == "slow-one"
        assert delivered["receipt_id"] == body["receipt_id"]
        assert billed == [W.catalog.BY_NAME["market.quote"].price_usd], "charged exactly once"

        again = client.get(body["collect_url"])
        assert again.status_code == 200 and billed == [billed[0]], "collecting is free"

    call = W.ledger.get_call(W.ledger.call_id_for(body["receipt_id"]))
    assert call["status"] == "ok"


def test_a_slow_job_that_fails_is_never_charged(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    billed = []

    async def slow_then_broken(ctx, payload):
        await asyncio.sleep(0.5)
        raise W.runtime.PermanentProviderError("upstream said no")

    _wire(app_module, monkeypatch, slow_then_broken, billed)
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                            json={"product_id": "BTC-USD"})
        assert first.status_code == 202
        done = _collect(client, first.json()["job_id"])
        assert done.status_code == 502
        assert done.json()["billed"] is False
    assert billed == []


def test_a_fast_job_is_delivered_inline_as_before(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "5")
    billed = []

    async def fast(ctx, payload):
        return _valid("market.quote", marker="fast")

    _wire(app_module, monkeypatch, fast, billed)
    with TestClient(app_module.app) as client:
        response = client.post("/work/market/quote", headers={"X-API-Key": "test-key"},
                               json={"product_id": "BTC-USD"})
    assert response.status_code == 200
    assert response.json()["result"]["marker"] == "fast"
    assert len(billed) == 1


def test_an_idempotent_retry_waits_for_the_handed_back_job_then_replays_it_free(
        app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    billed = []

    async def slow(ctx, payload):
        await asyncio.sleep(0.6)
        return _valid("market.quote", marker="once")

    _wire(app_module, monkeypatch, slow, billed)
    headers = {"X-API-Key": "test-key", "Idempotency-Key": "deliver-later-1"}
    with TestClient(app_module.app) as client:
        first = client.post("/work/market/quote", headers=headers, json={"product_id": "BTC-USD"})
        assert first.status_code == 202
        busy = client.post("/work/market/quote", headers=headers, json={"product_id": "BTC-USD"})
        assert busy.status_code == 409 and busy.json()["billed"] is False
        _collect(client, first.json()["job_id"])
        replay = client.post("/work/market/quote", headers=headers, json={"product_id": "BTC-USD"})
    assert replay.status_code == 200
    assert replay.json()["result"]["marker"] == "once" and replay.json()["billed"] is False
    assert len(billed) == 1


def test_a_job_a_restart_interrupted_is_closed_unbilled_and_its_mpp_payment_released(
        app_module, monkeypatch):
    W.ledger.open_call(call_id="c-lost", worker="video.generate", path="/work/video/generate",
                       price_usd=10.0, idempotency_key="lost-key")
    W.ledger.claim_idempotency("lost-key", "c-lost", "video.generate")
    W.ledger.open_deferred("job-lost", "c-lost", "video.generate", rail="mpp", mpp_tx="0xabc")
    released = []

    assert W.router.reconcile_interrupted(released.append) == 1
    assert released == ["0xabc"]
    row = W.ledger.get_deferred("job-lost")
    assert row["state"] == "done" and row["http_status"] == 502
    assert '"billed": false' in row["body"]
    assert W.ledger.get_call("c-lost")["status"] == "failed"
    assert W.ledger.claim_idempotency("lost-key", "c-retry", "video.generate")[0] == "claimed"


def test_an_unknown_job_is_a_free_404(app_module):
    from fastapi.testclient import TestClient

    response = TestClient(app_module.app).get("/work/jobs/no-such-job")
    assert response.status_code == 404 and response.json()["billed"] is False


def test_mcp_hands_the_job_to_the_agent_as_a_result_not_an_error(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_DELIVER_LATER_AFTER_SECONDS", "0.2")
    billed = []

    async def slow(ctx, payload):
        await asyncio.sleep(0.6)
        return _valid("market.quote")

    _wire(app_module, monkeypatch, slow, billed)
    tool = next(name for name, worker in app_module._mcp_worker_tools().items()
                if worker.name == "market.quote")
    with TestClient(app_module.app) as client:
        response = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": tool, "arguments": {"product_id": "BTC-USD"}}})
        result = response.json()["result"]
        assert result["isError"] is False, result
        assert result["structuredContent"]["status"] == "processing"
        assert "Nothing was charged" not in result["content"][0]["text"]
        _collect(client, result["structuredContent"]["job_id"])
    assert len(billed) == 1


# --- generated media by link --------------------------------------------------

def test_a_video_is_delivered_as_a_link_not_megabytes_of_base64(app_module, monkeypatch, tmp_path):
    """Several megabytes of base64 inside the JSON body is of no use to an
    agent; the clip is stored and handed out as a 24-hour link."""
    import base64

    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_MEDIA_DIR", str(tmp_path / "media"))
    clip = b"\x00\x00\x00\x20ftypisom-fake-mp4-bytes"

    class _FakeVeo:
        async def generate(self, prompt, **kwargs):
            return W.runtime.ProviderResult(
                value={"video_base64": base64.b64encode(clip).decode(), "gcs_uri": None,
                       "mime_type": "video/mp4", "model": "veo-3.1-fast-generate-001",
                       "duration_seconds": 4}, cost_micros=400000, cost_measured=True)

    media = W.skills.media

    class _Ctx:
        def remaining(self):
            return 200

        async def run(self, step, providers, call, **kwargs):
            return (await call(_FakeVeo())).value

    out = asyncio.run(media.generate_video(_Ctx(), {"prompt": "a bee", "duration_seconds": 4}))
    assert out["video_base64"] is None
    assert out["video_url"].startswith("https://hubvibe-io.com/work/media/")
    assert out["video_url_expires_at"].endswith("Z")
    assert W.catalog.contract.check(W.catalog.BY_NAME["video.generate"].output_schema, out) is None

    name = out["video_url"].rsplit("/", 1)[-1]
    response = TestClient(app_module.app).get(f"/work/media/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == clip

    inline = asyncio.run(media.generate_video(_Ctx(), {"prompt": "a bee", "inline": True}))
    assert base64.b64decode(inline["video_base64"]) == clip and inline["video_url"]


def test_a_clip_that_cannot_be_stored_is_delivered_inline_rather_than_lost(app_module, monkeypatch):
    import base64

    media = W.skills.media

    def _disk_full(data, extension):
        raise OSError("No space left on device")

    monkeypatch.setattr(W.media_store, "save", _disk_full)

    class _FakeVeo:
        async def generate(self, prompt, **kwargs):
            return W.runtime.ProviderResult(
                value={"video_base64": base64.b64encode(b"clip").decode(), "gcs_uri": None,
                       "mime_type": "video/mp4", "model": "m", "duration_seconds": 4},
                cost_micros=0, cost_measured=True)

    class _Ctx:
        def remaining(self):
            return 200

        async def run(self, step, providers, call, **kwargs):
            return (await call(_FakeVeo())).value

    out = asyncio.run(media.generate_video(_Ctx(), {"prompt": "a bee"}))
    assert out["video_url"] is None and base64.b64decode(out["video_base64"]) == b"clip"


def test_media_links_refuse_malformed_unknown_and_expired_names(app_module, monkeypatch, tmp_path):
    import os

    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_MEDIA_DIR", str(tmp_path))
    client = TestClient(app_module.app)
    assert client.get("/work/media/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/work/media/notahexname.mp4").status_code == 404
    assert client.get(f"/work/media/{'a' * 32}.mp4").status_code == 404
    stored = W.media_store.save(b"old", "mp4")
    name = stored["url"].rsplit("/", 1)[-1]
    old = os.path.getmtime(tmp_path / name) - W.media_store.KEEP_SECONDS - 10
    os.utime(tmp_path / name, (old, old))
    assert client.get(f"/work/media/{name}").status_code == 404
