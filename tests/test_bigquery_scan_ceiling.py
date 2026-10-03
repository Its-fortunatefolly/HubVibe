"""The scan ceiling on the BigQuery tools belongs to the node. A buyer may
lower it for their own query; a buyer may never raise it. This node's own
queries (news.search) keep the ceiling they name.

`max_scan_gib` was used as the ceiling as sent: a $0.50 data.query carrying
{"max_scan_gib": 100000} was dry-run against a 100,000 GiB limit and then run
with that as maximumBytesBilled, so one cheap call could scan (and bill this
node for) terabytes. And a value that was not a number at all reached the
arithmetic: a string was multiplied by 2**30 -- a gigabyte of text allocated
per request -- before anything refused it.
"""

import asyncio
import importlib.util
import math
import sys
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
BQ = W.providers.bigquery
GIB = 1024 ** 3
NODE_CEILING = int(BQ._MAX_GIB * GIB)


@pytest.fixture
def ran(monkeypatch):
    """The provider with its REST calls replaced: records the byte ceiling
    each real run was given. The dry run estimates `estimate` bytes."""
    calls = {"ceilings": [], "estimate": 1000}
    provider = BQ.PROVIDER if hasattr(BQ, "PROVIDER") else BQ.PROVIDERS[0]
    monkeypatch.setattr(BQ.google_auth, "configured", lambda: True)

    async def estimate(sql):
        return calls["estimate"]

    async def run(sql, ceiling_bytes, estimated):
        calls["ceilings"].append(ceiling_bytes)
        return W.runtime.ProviderResult(value={
            "columns": [], "rows": [], "row_count": 0, "total_rows": 0, "truncated": False,
            "bytes_processed": 0, "gib_processed": 0.0, "cache_hit": False})

    monkeypatch.setattr(provider, "estimate", estimate)
    monkeypatch.setattr(provider, "_run", run)
    calls["provider"] = provider
    return calls


class _Ctx:
    """The piece of JobContext the data skills use: run the call on the provider."""

    async def run(self, label, providers, call, **kwargs):
        result = await call(providers[0])
        return result.value


def _run_sql(max_scan_gib):
    payload = {"sql": "SELECT 1"}
    if max_scan_gib is not None:
        payload["max_scan_gib"] = max_scan_gib
    return asyncio.run(W.skills.REGISTRY["data.query"](_Ctx(), payload))


def test_a_buyer_cannot_raise_the_scan_ceiling(ran):
    _run_sql(100000)
    assert ran["ceilings"] == [NODE_CEILING], (
        "the buyer's number became maximumBytesBilled; one $0.50 call could bill terabytes")


def test_a_number_too_large_to_be_a_float_is_capped_not_a_crash(ran):
    _run_sql(10 ** 400)
    assert ran["ceilings"] == [NODE_CEILING]
    for worker in ("data.query", "data.forecast", "data.anomalies"):
        payload = dict(W.catalog.example_for(W.catalog.BY_NAME[worker]))
        W.skills.PRECHECKS[worker]({**payload, "max_scan_gib": 10 ** 400})  # must not raise


def test_a_query_over_the_nodes_ceiling_is_refused_whatever_the_buyer_sent(ran):
    ran["estimate"] = NODE_CEILING + 1
    with pytest.raises(W.runtime.InvalidRequest):
        _run_sql(100000)
    assert ran["ceilings"] == [], "it must never have run"


def test_a_buyer_can_lower_the_ceiling_for_their_own_query(ran):
    _run_sql(0.5)
    assert ran["ceilings"] == [int(0.5 * GIB)]
    ran["estimate"] = GIB
    with pytest.raises(W.runtime.InvalidRequest):
        _run_sql(0.5)


def test_no_ceiling_sent_means_the_nodes_ceiling(ran):
    _run_sql(None)
    assert ran["ceilings"] == [NODE_CEILING]


def test_every_tool_that_takes_a_buyers_ceiling_caps_it():
    """The cap lives where a buyer's value enters. Any skill that reads
    `max_scan_gib` must read it through _scan_ceiling, never raw."""
    for path in sorted((PKG / "skills").glob("*.py")):
        source = path.read_text()
        raw = [line.strip() for line in source.splitlines()
               if 'get("max_scan_gib")' in line and "value = payload.get" not in line]
        assert raw == [], f"{path.name} passes a buyer's max_scan_gib on unchecked: {raw}"
    assert W.skills.data._scan_ceiling({"max_scan_gib": 100000}) == BQ.node_ceiling_gib()
    assert W.skills.data._scan_ceiling({"max_scan_gib": 0.25}) == 0.25
    assert W.skills.data._scan_ceiling({}) is None


def test_this_nodes_own_queries_keep_the_ceiling_they_name(ran, monkeypatch):
    """news.search runs our own query under its own constant. Lowering the
    BUYER ceiling on a box must not shrink it (it did not before)."""
    monkeypatch.setattr(BQ, "_MAX_GIB", 5.0)
    asyncio.run(ran["provider"].query("SELECT 1", max_gib=20.0))
    assert ran["ceilings"] == [int(20.0 * GIB)]
    assert W.skills.data._scan_ceiling({"max_scan_gib": 20}) == 5.0, "a buyer is still capped"


def test_the_provider_never_does_arithmetic_on_a_non_number():
    assert BQ.scan_ceiling_bytes("5") == NODE_CEILING
    assert BQ.scan_ceiling_bytes(True) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(float("nan")) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(float("inf")) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(10 ** 400) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(-1) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(None) == NODE_CEILING
    assert BQ.scan_ceiling_bytes(0.25) == int(0.25 * GIB)
    source = (PKG / "providers" / "bigquery.py").read_text()
    assert "max_gib if max_gib is not None else _MAX_GIB" not in source


@pytest.mark.parametrize("worker", ["data.query", "data.forecast", "data.anomalies"])
@pytest.mark.parametrize("bad", ["5", "1e9", True, [5], {"gib": 5}, 0, -1, math.inf, math.nan])
def test_a_ceiling_that_is_not_a_positive_number_is_refused_before_payment(worker, bad):
    precheck = W.skills.PRECHECKS.get(worker)
    assert precheck is not None, f"{worker} validates nothing before the payment gate"
    payload = dict(W.catalog.example_for(W.catalog.BY_NAME[worker]))
    payload["max_scan_gib"] = bad
    with pytest.raises(W.runtime.InvalidRequest):
        precheck(payload)


@pytest.mark.parametrize("worker", ["data.query", "data.forecast", "data.anomalies"])
def test_an_ordinary_request_passes_the_precheck(worker):
    payload = dict(W.catalog.example_for(W.catalog.BY_NAME[worker]))
    W.skills.PRECHECKS[worker](payload)
    W.skills.PRECHECKS[worker]({**payload, "max_scan_gib": 2})
    W.skills.PRECHECKS[worker]({**payload, "max_scan_gib": 0.5})
