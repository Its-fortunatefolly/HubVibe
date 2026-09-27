"""social.x_pulse -- X (Twitter) as aggregates only.

Pinned: the reduction from raw posts to figures leaks nothing identifying
(no id, text, author or handle survives), counts and search pages are
parsed from the shapes X served on 2026-09-27, the effective query adds
lang: and -is:retweet unless the caller opted otherwise, sample=0 reads no
posts (counts only, no per-post charge), the sample is capped, input is
refused before the gate, and the token's absence hides the worker.
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

COUNTS = {"data": [{"end": "2026-09-26T00:00:00.000Z", "start": "2026-09-25T00:00:00.000Z", "tweet_count": 12},
                   {"end": "2026-09-27T00:00:00.000Z", "start": "2026-09-26T00:00:00.000Z", "tweet_count": 30}],
          "meta": {"total_tweet_count": 42}}
POST = lambda i, likes, imps, lang="en", tags=(), urls=(), sensitive=False: {  # noqa: E731
    "id": f"19000000000000000{i}", "text": f"post number {i} #topic", "created_at": "2026-09-26T12:00:00.000Z", "lang": lang,
    "edit_history_tweet_ids": [f"19000000000000000{i}"], "possibly_sensitive": sensitive,
    "public_metrics": {"retweet_count": 1, "reply_count": 2, "like_count": likes, "quote_count": 0, "bookmark_count": 0, "impression_count": imps},
    "entities": {"hashtags": [{"start": 0, "end": 6, "tag": t} for t in tags], "urls": [{"url": "https://t.co/x", "expanded_url": u} for u in urls]}}
SEARCH_PAGE_1 = {"data": [POST(1, 10, 100, tags=("Topic", "news"), urls=("https://example.com/a",)),
                          POST(2, 30, 300, lang="ja", tags=("topic",)),
                          POST(3, 0, 50, urls=("https://x.com/u/status/1/photo/1",), sensitive=True)],
                 "meta": {"newest_id": "3", "oldest_id": "1", "result_count": 3, "next_token": "b26v89c19zqg8o3fpds"}}
SEARCH_PAGE_2 = {"data": [POST(4, 5, 0)], "meta": {"result_count": 1}}


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


class _Ctx:
    def __init__(self, counts, pages):
        self.counts, self.pages = counts, list(pages)
        self.steps, self.providers_used, self.attempts = [], [], []
        self.queries = []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = "x-api-v2"

            async def counts_recent(self, query, granularity="day", start_time=None, end_time=None):
                outer.queries.append(("counts", query, granularity))
                return W.runtime.ProviderResult(value=outer.counts, cost_micros=0, cost_measured=True)

            async def search_recent(self, query, max_results=10, start_time=None, end_time=None, next_token=None):
                outer.queries.append(("search", query, max_results, next_token))
                page = outer.pages.pop(0)
                return W.runtime.ProviderResult(value=page, cost_micros=len(page["posts"]) * 5000, cost_measured=True)

        result = await call(_P())
        self.providers_used.append("x-api-v2")
        return result.value


def _counts_value():
    return {"buckets": [{"start": b["start"], "end": b["end"], "count": b["tweet_count"]} for b in COUNTS["data"]], "total": 42}


def _page(raw):
    return {"posts": raw["data"], "next_token": (raw.get("meta") or {}).get("next_token"), "result_count": len(raw["data"])}


def test_aggregates_leak_nothing_identifying_and_the_numbers_are_right():
    S = W.skills.x_pulse
    agg = S.aggregate(SEARCH_PAGE_1["data"] + SEARCH_PAGE_2["data"])
    assert agg["posts_read"] == 4
    assert agg["likes_total"] == 45 and agg["impressions_total"] == 450 and agg["reposts_total"] == 4 and agg["replies_total"] == 8
    assert agg["mean_likes"] == 11.25 and agg["max_likes"] == 30 and agg["max_impressions"] == 300
    assert agg["engagement_rate_pct"] == round(100.0 * (45 + 4 + 8 + 0) / 450, 3)
    assert agg["languages"] == {"en": 3, "ja": 1}
    assert agg["top_hashtags"] == [{"tag": "topic", "posts": 2}, {"tag": "news", "posts": 1}]
    assert agg["with_hashtags_pct"] == 50.0 and agg["with_links_pct"] == 25.0 and agg["with_media_pct"] == 25.0
    assert agg["possibly_sensitive_pct"] == 25.0
    dumped = json.dumps(agg)
    for forbidden in ("19000000000000000", "post number", "created_at", "text", "author", "username", "handle"):
        assert forbidden not in dumped, forbidden


def test_pulse_reads_counts_then_pages_the_sample_and_stamps_the_window():
    S = W.skills.x_pulse
    ctx = _Ctx(_counts_value(), [_page(SEARCH_PAGE_1), _page(SEARCH_PAGE_2)])
    r = asyncio.run(S.pulse(ctx, {"query": "open source", "days": 2, "sample": 4, "lang": "en"}))
    assert ctx.steps == ["counts", "search_page_1", "search_page_2"]
    assert ctx.queries[0] == ("counts", "open source lang:en -is:retweet", "day")
    assert ctx.queries[1][2] == 10 and ctx.queries[1][3] is None   # X's minimum page is 10
    assert ctx.queries[2][3] == "b26v89c19zqg8o3fpds"
    assert r["effective_query"] == "open source lang:en -is:retweet"
    assert r["volume"] == {"total": 42, "buckets": _counts_value()["buckets"], "bucket_count": 2}
    assert r["sample"] == {"requested": 4, "read": 4, "pages": 2, "x_cost_usd": 0.02}
    assert r["engagement"]["posts_read"] == 4 and r["window"]["days"] == 2
    assert r["as_of"] == r["window"]["end"] and r["checked_at"].endswith("Z")
    assert set(r) == {"query", "effective_query", "window", "granularity", "volume", "sample", "engagement",
                      "source", "as_of", "checked_at"}
    for forbidden in ("19000000000000000", "post number"):
        assert forbidden not in json.dumps(r)


def test_sample_zero_is_counts_only_and_costs_no_post_reads():
    S = W.skills.x_pulse
    ctx = _Ctx(_counts_value(), [])
    r = asyncio.run(S.pulse(ctx, {"query": "open source", "sample": 0, "include_retweets": True}))
    assert ctx.steps == ["counts"] and r["engagement"] is None
    assert r["sample"] == {"requested": 0, "read": 0, "pages": 0, "x_cost_usd": 0.0}
    assert r["effective_query"] == "open source"  # retweets included, no lang added


def test_input_is_validated_before_the_gate():
    S = W.skills.x_pulse
    for bad in ({}, {"query": " "}, {"query": "x" * 401}, {"query": "x", "days": 8}, {"query": "x", "days": 0},
                {"query": "x", "granularity": "minute"}, {"query": "x", "sample": 101}, {"query": "x", "sample": -1},
                {"query": "x", "lang": "english"}, {"query": "x", "include_retweets": "yes"}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.precheck(bad)
    req = S.parse({"query": "\"machine learning\" -is:retweet lang:ja", "sample": 40})
    assert req["effective_query"] == "\"machine learning\" -is:retweet lang:ja"  # operators already present, nothing added


def test_the_provider_parses_counts_and_search_pages_and_maps_errors(monkeypatch):
    X = W.providers.x_api
    monkeypatch.setenv("X_BEARER_TOKEN", "test")
    responses = {"tweets/counts/recent": (200, COUNTS), "tweets/search/recent": (200, SEARCH_PAGE_1)}

    async def fake(path, params):
        return responses[path]
    monkeypatch.setattr(X, "_get_json", fake)
    p = X.PROVIDERS[0]
    assert p.available()
    c = asyncio.run(p.counts_recent("q", granularity="day", start_time="2026-09-25T00:00:00Z")).value
    assert c["total"] == 42 and c["buckets"][1]["count"] == 30
    s = asyncio.run(p.search_recent("q", max_results=3))
    assert len(s.value["posts"]) == 3 and s.value["next_token"] == "b26v89c19zqg8o3fpds"
    assert s.cost_micros == 15000 and s.cost_measured  # 3 posts x $0.005
    responses["tweets/search/recent"] = (400, {"errors": [{"message": "Invalid query"}], "title": "Invalid Request"})
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(p.search_recent("bad("))
    responses["tweets/search/recent"] = (403, {"title": "Forbidden", "detail": "credits exhausted"})
    with pytest.raises(W.runtime.ProviderUnavailable):
        asyncio.run(p.search_recent("q"))
    monkeypatch.delenv("X_BEARER_TOKEN")
    assert not p.available() and "X_BEARER_TOKEN" in p.unavailable_reason()
