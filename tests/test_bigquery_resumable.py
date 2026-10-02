"""The model jobs (AI.FORECAST, AI.DETECT_ANOMALIES) never hold a paid call
past the routers' 30 s: a job still running answers "still computing"
(503, Retry-After, unbilled), and the identical request resumes that same
BigQuery job instead of paying for a second run.

Measured on the box 2026-10-02: the same uncached 56-series anomaly query
took 28.7 s, then 72.8 s -- Google's queue, not our code.
"""

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "wcag-audit-engine" / "app" / "workers"


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
BQ = sys.modules["wcag_audit_engine_workers.providers.bigquery"]


class _FakeBigQuery:
    """Stands in for the REST calls query_resumable makes."""

    def __init__(self, complete_after_polls=None, previous_job_age=None):
        self.complete_after_polls = complete_after_polls
        self.previous_job_age = previous_job_age
        self.inserted = []
        self.polled = []

    async def post(self, body):
        assert body.get("dryRun") is True
        return {"totalBytesProcessed": "1000", "jobReference": {"location": "US"}}

    async def request(self, method, url, **kwargs):
        if method == "GET" and "/jobs/" in url:
            if self.previous_job_age is None:
                raise W.runtime.PermanentProviderError("BigQuery rejected the query: Not found: Job")
            created_ms = int((time.time() - self.previous_job_age) * 1000)
            return {"statistics": {"creationTime": str(created_ms)}}
        if method == "POST" and url.endswith("/jobs"):
            self.inserted.append(kwargs["json"]["jobReference"]["jobId"])
            return {}
        if method == "GET" and "/queries/" in url:
            self.polled.append(url.rsplit("/", 1)[-1])
            if self.complete_after_polls is None or len(self.polled) < self.complete_after_polls:
                await asyncio.sleep(0.05)
                return {"jobComplete": False}
            return {"jobComplete": True, "schema": {"fields": [{"name": "is_anomaly"}]},
                    "rows": [{"f": [{"v": "true"}]}], "totalRows": "1",
                    "totalBytesProcessed": "1000"}
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture
def fake(monkeypatch):
    def install(**kwargs):
        fake = _FakeBigQuery(**kwargs)
        provider = BQ.PROVIDER
        monkeypatch.setattr(BQ.google_auth, "configured", lambda: True)
        monkeypatch.setattr(BQ.google_auth, "project", lambda: "proj")
        monkeypatch.setattr(provider, "_post", fake.post)
        monkeypatch.setattr(provider, "_request", fake.request)
        return fake
    return install


def test_a_job_still_running_answers_still_computing_inside_the_wait(fake):
    f = fake(complete_after_polls=None)
    started = time.monotonic()
    with pytest.raises(W.runtime.StillComputing) as raised:
        asyncio.run(BQ.PROVIDER.query_resumable("SELECT 1", wait_seconds=0.6))
    assert time.monotonic() - started < 3
    assert raised.value.retry_after == 20
    assert len(f.inserted) == 1


def test_a_finished_job_returns_its_rows(fake):
    f = fake(complete_after_polls=2)
    result = asyncio.run(BQ.PROVIDER.query_resumable("SELECT 1", wait_seconds=5))
    assert result.value["rows"] == [{"is_anomaly": "true"}]
    assert len(f.polled) == 2


def test_the_identical_request_resumes_the_job_instead_of_starting_another(fake):
    f = fake(complete_after_polls=1, previous_job_age=30)
    asyncio.run(BQ.PROVIDER.query_resumable("SELECT 1", wait_seconds=5))
    assert f.inserted == []                       # no second run
    window = int(time.time() // BQ._JOB_REUSE_SECONDS)
    assert f.polled == [BQ._job_id("SELECT 1", int(BQ._MAX_GIB * BQ._BYTES_PER_GIB), window - 1)]


def test_a_stale_job_is_not_resumed(fake):
    f = fake(complete_after_polls=1, previous_job_age=BQ._JOB_REUSE_SECONDS + 5)
    asyncio.run(BQ.PROVIDER.query_resumable("SELECT 1", wait_seconds=5))
    assert len(f.inserted) == 1                   # fresh job; the old answer is too old


def test_still_computing_is_never_retried_in_call():
    calls = {"n": 0}

    class _P:
        id = "bigquery"

        def available(self):
            return True

    async def call(provider):
        calls["n"] += 1
        raise W.runtime.StillComputing("running", retry_after=20)

    W.runtime.reset_breakers()
    with pytest.raises(W.runtime.StillComputing):
        asyncio.run(W.runtime.run_with_policy([_P()], call, deadline_seconds=60))
    assert calls["n"] == 1


def test_still_computing_maps_to_503():
    router = sys.modules["wcag_audit_engine_workers.router"]
    assert router._error_status(W.runtime.StillComputing.reason) == 503


def test_anomaly_count_covers_every_scored_point_not_just_returned_rows():
    """56 series x 30 points = 1,680 scored, 200 returned: the count must be
    the query's own total (47), not the anomalies among the 200 (15)."""
    data = sys.modules["wcag_audit_engine_workers.skills.data"]
    captured = {}

    class _Ctx:
        def remaining(self):
            return 999

        async def run(self, step, providers, call, **kwargs):
            class _P:
                async def query_resumable(self, sql, max_gib=None, wait_seconds=None):
                    captured["sql"] = sql
                    rows = [{"state_name": "Texas", "is_anomaly": "true", "anomaly_probability": "0.99",
                             "_hv_anomaly_total": "47"}] * 15 + \
                           [{"state_name": "Ohio", "is_anomaly": "false", "anomaly_probability": "0.1",
                             "_hv_anomaly_total": "47"}] * 185
                    return W.runtime.ProviderResult(
                        value={"columns": ["state_name", "is_anomaly", "anomaly_probability", "_hv_anomaly_total"],
                               "rows": rows, "row_count": 200, "total_rows": 1680, "truncated": True,
                               "gib_processed": 0.01, "cache_hit": False},
                        cost_micros=0, cost_measured=True)
            return (await call(_P())).value

    out = asyncio.run(data.detect_anomalies(_Ctx(), {
        "history_table": "p.d.t", "target_table": "p.d.t", "timestamp_col": "date",
        "data_col": "v", "id_cols": ["state_name"]}))
    assert out["anomaly_count"] == 47
    assert out["total_rows"] == 1680 and out["truncated"] is True
    assert "_hv_anomaly_total" not in out["columns"]
    assert all("_hv_anomaly_total" not in r for r in out["rows"])
    assert "ORDER BY is_anomaly DESC, anomaly_probability DESC" in captured["sql"]
