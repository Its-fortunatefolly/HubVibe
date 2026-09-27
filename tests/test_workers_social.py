"""social.bluesky and social.mastodon -- keyless public social reads.

Pinned from captured response shapes (2026-09-27): Bluesky profile, author
feed (incl. a repost and a reply), actor search and a post thread; Mastodon
hashtag timeline, account lookup + statuses (incl. a reblog), search for
accounts/hashtags and trends. Every mode returns the one result shape;
input is refused before the gate; private/internal Mastodon instances are
refused; unknown actors/accounts are the caller's error.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

BSKY_PROFILE = {"did": "did:plc:z72i7hdynmk6r22z27h6tvur", "handle": "bsky.app", "displayName": "Bluesky",
                "avatar": "https://cdn.bsky.app/img/avatar/plain/did:plc:z72i7hdynmk6r22z27h6tvur/x@jpeg",
                "description": "official bluesky account", "followersCount": 4200000, "followsCount": 3,
                "postsCount": 1234, "createdAt": "2023-04-12T04:53:57.057Z", "associated": {}}
_POST = lambda rkey, text, indexed, **extra: {"post": {  # noqa: E731
    "uri": f"at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/{rkey}", "cid": "bafy" + rkey,
    "author": {"did": "did:plc:z72i7hdynmk6r22z27h6tvur", "handle": "bsky.app", "displayName": "Bluesky"},
    "record": {"$type": "app.bsky.feed.post", "text": text, "createdAt": indexed, "langs": ["en"], **extra.get("record", {})},
    "replyCount": 5, "repostCount": 7, "likeCount": 90, "quoteCount": 2, "indexedAt": indexed,
    **({"embed": extra["embed"]} if "embed" in extra else {})}, **({"reason": extra["reason"]} if "reason" in extra else {})}
BSKY_FEED = {"feed": [
    _POST("3mw2cdr44fc2a", "hello world", "2026-09-26T20:00:00.000Z", embed={"$type": "app.bsky.embed.images#view", "images": []}),
    _POST("3mw1aaa", "a repost", "2026-09-25T10:00:00.000Z", reason={"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": "did:plc:other"}}),
    _POST("3mw0bbb", "a reply", "2026-09-24T10:00:00.000Z", record={"reply": {"parent": {"uri": "at://did:plc:other/app.bsky.feed.post/3parent", "cid": "x"}, "root": {"uri": "at://did:plc:other/app.bsky.feed.post/3parent", "cid": "x"}}}),
], "cursor": "abc"}
BSKY_ACTORS = {"actors": [{"did": "did:plc:coin", "handle": "coinbase.com", "displayName": "Coinbase", "description": "d", "avatar": "https://a/b.jpg"},
                          {"did": "did:plc:two", "handle": "two.bsky.social"}], "cursor": None}
BSKY_THREAD = {"thread": {"$type": "app.bsky.feed.defs#threadViewPost", **_POST("3root", "root post", "2026-09-26T21:00:00.000Z"),
                          "replies": [{"$type": "app.bsky.feed.defs#threadViewPost", **_POST("3r1", "first reply", "2026-09-26T21:05:00.000Z"),
                                       "replies": [{"$type": "app.bsky.feed.defs#threadViewPost", **_POST("3r1a", "nested", "2026-09-26T21:06:00.000Z"), "replies": []}]},
                                      {"$type": "app.bsky.feed.defs#notFoundPost", "uri": "at://x/app.bsky.feed.post/gone", "notFound": True}]},
               "threadgate": None}

MASTO_STATUS = lambda sid, html, created, **extra: {  # noqa: E731
    "id": sid, "created_at": created, "in_reply_to_id": None, "in_reply_to_account_id": None, "sensitive": False,
    "spoiler_text": "", "visibility": "public", "language": "en", "uri": f"https://mastodon.social/users/x/statuses/{sid}",
    "url": f"https://mastodon.social/@x/{sid}", "replies_count": 1, "reblogs_count": 2, "favourites_count": 3,
    "content": html, "reblog": extra.get("reblog"), "account": {"id": "1", "username": "Gargron", "acct": "Gargron", "display_name": "Eugen Rochko", "url": "https://mastodon.social/@Gargron"},
    "media_attachments": extra.get("media", []), "tags": extra.get("tags", [])}
MASTO_TIMELINE = [MASTO_STATUS("101", "<p>Open <b>source</b> rocks &amp; rolls<br>line two</p>", "2026-09-27T01:00:00.000Z", tags=[{"name": "opensource", "url": "https://mastodon.social/tags/opensource"}], media=[{"type": "image", "url": "https://files/x.png"}]),
                  MASTO_STATUS("100", "", "2026-09-26T01:00:00.000Z", reblog=MASTO_STATUS("99", "<p>original</p>", "2026-09-25T00:00:00.000Z"))]
MASTO_ACCOUNT = {"id": "1", "username": "Gargron", "acct": "Gargron", "display_name": "Eugen Rochko", "locked": False, "bot": False,
                 "created_at": "2016-03-16T00:00:00.000Z", "note": "<p>Founder of <a href=\"x\">Mastodon</a></p>", "url": "https://mastodon.social/@Gargron",
                 "avatar": "https://files/avatar.png", "followers_count": 500000, "following_count": 400, "statuses_count": 75000, "last_status_at": "2026-09-26"}
MASTO_SEARCH_ACCOUNTS = {"accounts": [MASTO_ACCOUNT], "statuses": [], "hashtags": [], "collections": []}
MASTO_SEARCH_TAGS = {"accounts": [], "statuses": [], "hashtags": [{"name": "x402", "url": "https://mastodon.social/tags/x402", "history": [{"day": "1790380800", "uses": "3", "accounts": "2"}, {"day": "1790294400", "uses": "1", "accounts": "1"}]}], "collections": []}
MASTO_TRENDS = [{"name": "caturday", "url": "https://mastodon.social/tags/caturday", "history": [{"day": "1790380800", "uses": "120", "accounts": "90"}]},
                {"name": "nohistory", "url": "https://mastodon.social/tags/nohistory", "history": []}]


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
    """Serves each step from a fixture keyed by (provider method, first arg)."""

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
                    key = (name, args[0] if args else None)
                    if key not in outer.answers and (name, None) in outer.answers:
                        key = (name, None)
                    if key not in outer.answers:
                        raise AssertionError(f"unexpected call {name}{args}")
                    answer = outer.answers[key]
                    if isinstance(answer, Exception):
                        raise answer
                    return W.runtime.ProviderResult(value=answer, cost_micros=0, cost_measured=True)
                return method

        result = await call(_P())
        self.providers_used.append(providers[0].id)
        return result.value


def _contract_ok(name, result):
    return W.catalog.contract.check(W.catalog.BY_NAME[name].output_schema, result)


# --- Bluesky ----------------------------------------------------------------------

def test_bluesky_profile_mode_returns_the_profile_and_its_posts_with_repost_and_reply_flags():
    S = W.skills.bluesky
    ctx = _Ctx({("profile", "bsky.app"): BSKY_PROFILE, ("author_feed", "bsky.app"): BSKY_FEED})
    r = asyncio.run(S.social(ctx, {"mode": "profile", "actor": "@Bsky.app", "posts": 3}))
    assert ctx.steps == ["profile", "author_feed"]
    assert r["actor"] == "bsky.app" and r["query"] is None and r["uri"] is None
    assert r["profile"] == {"did": "did:plc:z72i7hdynmk6r22z27h6tvur", "handle": "bsky.app", "display_name": "Bluesky",
                            "description": "official bluesky account", "avatar_url": BSKY_PROFILE["avatar"],
                            "followers": 4200000, "follows": 3, "posts": 1234, "created_at": "2023-04-12T04:53:57.057Z"}
    assert r["post_count"] == 3 and r["actor_count"] == 0 and r["actors"] == [] and r["thread"] is None
    first, repost, reply = r["posts"]
    assert first["url"] == "https://bsky.app/profile/bsky.app/post/3mw2cdr44fc2a"
    assert first["likes"] == 90 and first["langs"] == ["en"] and first["embed_type"] == "app.bsky.embed.images#view"
    assert first["is_repost"] is False and repost["is_repost"] is True
    assert reply["reply_to"] == "at://did:plc:other/app.bsky.feed.post/3parent"
    assert r["as_of"] == "2026-09-26T20:00:00.000Z" and r["checked_at"].endswith("Z")
    assert _contract_ok("social.bluesky", r) is None


def test_bluesky_actor_search_and_thread_modes():
    S = W.skills.bluesky
    r = asyncio.run(S.social(_Ctx({("search_actors", "coinbase"): BSKY_ACTORS}), {"mode": "search_actors", "query": "coinbase", "limit": 5}))
    assert r["actor_count"] == 2 and r["actors"][0]["handle"] == "coinbase.com" and r["actors"][1]["display_name"] is None
    assert r["posts"] == [] and r["profile"] is None and _contract_ok("social.bluesky", r) is None
    uri = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.post/3root"
    r = asyncio.run(S.social(_Ctx({("thread", uri): BSKY_THREAD}), {"mode": "thread", "uri": uri, "depth": 3}))
    assert r["thread"]["post"]["text"] == "root post"
    assert [p["text"] for p in r["thread"]["replies"]] == ["first reply", "nested"]  # notFound node skipped
    assert r["thread"]["reply_count"] == 2 and r["as_of"] == "2026-09-26T21:00:00.000Z"
    assert _contract_ok("social.bluesky", r) is None
    # depth 1 flattens only the first level
    r = asyncio.run(S.social(_Ctx({("thread", uri): BSKY_THREAD}), {"mode": "thread", "uri": uri, "depth": 1}))
    assert [p["text"] for p in r["thread"]["replies"]] == ["first reply"]


def test_bluesky_input_is_validated_before_the_gate():
    S = W.skills.bluesky
    assert S.validate_actor("did:plc:z72i7hdynmk6r22z27h6tvur") == "did:plc:z72i7hdynmk6r22z27h6tvur"
    assert S.validate_actor("Bsky.App") == "bsky.app"
    for bad in ({}, {"mode": "posts"}, {"mode": "profile"}, {"mode": "profile", "actor": "not a handle"},
                {"mode": "profile", "actor": "bsky.app", "posts": 0}, {"mode": "profile", "actor": "bsky.app", "posts": 51},
                {"mode": "search_actors"}, {"mode": "search_actors", "query": "x", "limit": 26},
                {"mode": "thread"}, {"mode": "thread", "uri": "https://bsky.app/profile/x/post/y"},
                {"mode": "thread", "uri": "at://did:plc:abc/app.bsky.feed.post/3x", "depth": 7}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.precheck(bad)


def test_bluesky_unknown_actor_is_the_callers_error(monkeypatch):
    async def fake(method, params):
        return 400, {"error": "InvalidRequest", "message": "Profile not found"}
    monkeypatch.setattr(W.providers.bluesky, "_get_json", fake)
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.providers.bluesky.PROVIDERS[0].profile("nobody.example"))

    async def forbidden(method, params):
        return 403, {"error": "AuthMissing"}
    monkeypatch.setattr(W.providers.bluesky, "_get_json", forbidden)
    with pytest.raises(W.runtime.PermanentProviderError):
        asyncio.run(W.providers.bluesky.PROVIDERS[0].search_actors("x"))


# --- Mastodon ---------------------------------------------------------------------

def test_mastodon_hashtag_mode_strips_html_and_flags_reblogs():
    S = W.skills.mastodon
    ctx = _Ctx({("hashtag_timeline", "mastodon.social"): {"statuses": MASTO_TIMELINE}})
    r = asyncio.run(S.social(ctx, {"mode": "hashtag", "tag": "#OpenSource", "limit": 5}))
    assert ctx.steps == ["hashtag_timeline"] and r["instance"] == "mastodon.social" and r["tag"] == "OpenSource"
    first, reblog = r["statuses"]
    assert first["text"] == "Open source rocks & rolls\nline two" and "<b>" in first["content_html"]
    assert first["tags"] == ["opensource"] and first["media"] == [{"type": "image", "url": "https://files/x.png"}]
    assert first["replies"] == 1 and first["favourites"] == 3 and first["author_acct"] == "Gargron"
    assert reblog["is_reblog"] is True and reblog["reblog_of_url"] == "https://mastodon.social/@x/99" and reblog["text"] == "original"
    assert r["status_count"] == 2 and r["account"] is None and r["accounts"] == [] and r["hashtags"] == []
    assert r["as_of"] == "2026-09-27T01:00:00.000Z"
    assert _contract_ok("social.mastodon", r) is None


def test_mastodon_account_search_and_trends_modes():
    S = W.skills.mastodon
    ctx = _Ctx({("lookup", "mastodon.social"): MASTO_ACCOUNT, ("statuses", "mastodon.social"): {"statuses": MASTO_TIMELINE[:1]}})
    r = asyncio.run(S.social(ctx, {"mode": "account", "acct": "@Gargron", "statuses": 5}))
    assert ctx.steps == ["lookup", "statuses"]
    assert r["account"]["bio"] == "Founder of Mastodon" and r["account"]["followers"] == 500000 and r["account"]["bot"] is False
    assert r["status_count"] == 1 and _contract_ok("social.mastodon", r) is None
    r = asyncio.run(S.social(_Ctx({("search", "mastodon.social"): {"kind": "hashtags", "results": MASTO_SEARCH_TAGS["hashtags"]}}),
                             {"mode": "search", "query": "x402", "kind": "hashtags"}))
    assert r["hashtags"] == [{"name": "x402", "url": "https://mastodon.social/tags/x402", "uses_7d": 4, "accounts_7d": 3, "days": 2}]
    assert _contract_ok("social.mastodon", r) is None
    r = asyncio.run(S.social(_Ctx({("search", "mastodon.social"): {"kind": "accounts", "results": [MASTO_ACCOUNT]}}),
                             {"mode": "search", "query": "gargron", "kind": "accounts", "limit": 3}))
    assert r["accounts"][0]["acct"] == "Gargron" and r["hashtags"] == []
    r = asyncio.run(S.social(_Ctx({("trends", "fosstodon.org"): {"hashtags": MASTO_TRENDS}}), {"mode": "trends", "instance": "https://Fosstodon.org/", "limit": 2}))
    assert r["instance"] == "fosstodon.org" and r["hashtags"][0]["uses_7d"] == 120 and r["hashtags"][1]["uses_7d"] is None
    assert r["as_of"] == r["checked_at"] and _contract_ok("social.mastodon", r) is None


def test_mastodon_instance_and_input_validation_before_the_gate():
    S = W.skills.mastodon
    assert S.validate_instance(None) == "mastodon.social"
    assert S.validate_instance("https://Mastodon.Social/") == "mastodon.social"
    for bad_instance in ("localhost", "10.0.0.1", "foo.internal", "box.local", "nodots", "mastodon.social:8080", "169.254.169.254"):
        with pytest.raises(W.runtime.InvalidRequest):
            S.validate_instance(bad_instance)
    for bad in ({}, {"mode": "firehose"}, {"mode": "hashtag"}, {"mode": "hashtag", "tag": "two words"},
                {"mode": "hashtag", "tag": "x", "limit": 41}, {"mode": "account"}, {"mode": "account", "acct": "bad acct!"},
                {"mode": "search"}, {"mode": "search", "query": "x", "kind": "statuses"}, {"mode": "trends", "limit": 21}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.precheck(bad)


def test_mastodon_unknown_account_is_the_callers_error_and_auth_walls_are_permanent(monkeypatch):
    async def missing(instance, path, params=None):
        return 404, {"error": "Record not found"}
    monkeypatch.setattr(W.providers.mastodon, "_get_json", missing)
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(W.providers.mastodon.PROVIDERS[0].lookup("mastodon.social", "nobody"))

    async def walled(instance, path, params=None):
        return 422, {"error": "This method requires an authenticated user"}
    monkeypatch.setattr(W.providers.mastodon, "_get_json", walled)
    with pytest.raises(W.runtime.PermanentProviderError):
        asyncio.run(W.providers.mastodon.PROVIDERS[0].trends("mastodon.social"))


def test_strip_html():
    s = W.providers.mastodon.strip_html
    assert s("<p>Hi <a href='x'>there</a> &amp; you</p><p>Second</p>") == "Hi there & you\nSecond"
    assert s(None) == "" and s("") == ""
