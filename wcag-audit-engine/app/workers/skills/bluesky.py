"""social.bluesky -- a Bluesky profile with its latest posts, an actor
search, or a post thread, from the public AppView, keyless, live.

One worker, three modes: `profile` (the account and its newest posts),
`search_actors` (accounts matching a query) and `thread` (a post and the
replies under it). Post search is not offered: Bluesky's public AppView
refuses it to unauthenticated callers (verified 2026-09-27).

Every result carries the same keys, with [] or null where a mode has no
say, so a buyer's parser never branches on absence. `as_of` is the newest
post's indexing time when there are posts, else `checked_at`. NEVER A
GUESS: a count Bluesky omits is null.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import bluesky

MODES = ("profile", "search_actors", "thread")
MAX_POSTS = 50
MAX_ACTORS = 25
MAX_DEPTH = 6
MAX_QUERY_CHARS = 200
SOURCE = "bluesky-public-appview"

_HANDLE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.IGNORECASE)
_DID = re.compile(r"^did:(plc:[a-z2-7]{24}|web:[A-Za-z0-9.\-%:]+)$")
_POST_URI = re.compile(r"^at://(did:[a-z]+:[A-Za-z0-9._:%-]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)$")
_REPOST = "app.bsky.feed.defs#reasonRepost"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _str(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate_actor(raw, field: str = "actor") -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest(f"`{field}` is required: a Bluesky handle (bsky.app) or DID (did:plc:...).")
    actor = raw.strip().lstrip("@")
    if _DID.match(actor):
        return actor
    if len(actor) <= 253 and _HANDLE.match(actor):
        return actor.lower()
    raise runtime.InvalidRequest(f"`{field}` must be a handle such as bsky.app or a DID such as did:plc:....")


def validate_post_uri(raw) -> str:
    if not isinstance(raw, str) or not _POST_URI.match(raw.strip()):
        raise runtime.InvalidRequest("`uri` must be a post AT-URI: at://did:plc:.../app.bsky.feed.post/<rkey>.")
    return raw.strip()


def _count(payload: dict, key: str, default: int, maximum: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise runtime.InvalidRequest(f"`{key}` must be a whole number from 1 to {maximum}.")
    return value


def parse(payload: dict) -> dict:
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise runtime.InvalidRequest(f"`mode` must be one of {list(MODES)}.")
    req = {"mode": mode, "actor": None, "query": None, "uri": None, "posts": None, "limit": None, "depth": None}
    if mode == "profile":
        req["actor"] = validate_actor(payload.get("actor"))
        req["posts"] = _count(payload, "posts", 10, MAX_POSTS)
    elif mode == "search_actors":
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise runtime.InvalidRequest("`query` is required for search_actors.")
        if len(query.strip()) > MAX_QUERY_CHARS:
            raise runtime.InvalidRequest(f"`query` is over the {MAX_QUERY_CHARS}-character limit.")
        req["query"] = query.strip()
        req["limit"] = _count(payload, "limit", 10, MAX_ACTORS)
    else:
        req["uri"] = validate_post_uri(payload.get("uri"))
        req["depth"] = _count(payload, "depth", 2, MAX_DEPTH)
    return req


def precheck(payload: dict) -> None:
    parse(payload)


def post_record(item: dict) -> Optional[dict]:
    """One feed item or thread node's `post` -> the worker's post shape."""
    post = item.get("post") if isinstance(item.get("post"), dict) else item
    if not isinstance(post, dict) or not _str(post.get("uri")):
        return None
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    uri = post["uri"].strip()
    match = _POST_URI.match(uri)
    handle = _str(author.get("handle"))
    url = f"https://bsky.app/profile/{handle or (match.group(1) if match else '')}/post/{match.group(2)}" if match else None
    reason = item.get("reason") if isinstance(item.get("reason"), dict) else None
    reply = record.get("reply") if isinstance(record.get("reply"), dict) else None
    parent = reply.get("parent") if reply and isinstance(reply.get("parent"), dict) else None
    embed = post.get("embed") if isinstance(post.get("embed"), dict) else (
        record.get("embed") if isinstance(record.get("embed"), dict) else None)
    langs = [l for l in (record.get("langs") or []) if isinstance(l, str)] if isinstance(record.get("langs"), list) else []
    return {
        "uri": uri,
        "cid": _str(post.get("cid")),
        "url": url,
        "author_handle": handle,
        "author_did": _str(author.get("did")),
        "text": record.get("text") if isinstance(record.get("text"), str) else "",
        "created_at": _str(record.get("createdAt")),
        "indexed_at": _str(post.get("indexedAt")),
        "likes": _int(post.get("likeCount")),
        "reposts": _int(post.get("repostCount")),
        "replies": _int(post.get("replyCount")),
        "quotes": _int(post.get("quoteCount")),
        "langs": langs,
        "is_repost": bool(reason and reason.get("$type") == _REPOST),
        "reply_to": _str(parent.get("uri")) if parent else None,
        "embed_type": _str(embed.get("$type")) if embed else None,
    }


def profile_record(data: dict) -> dict:
    return {
        "did": _str(data.get("did")),
        "handle": _str(data.get("handle")),
        "display_name": _str(data.get("displayName")),
        "description": data.get("description") if isinstance(data.get("description"), str) else None,
        "avatar_url": _str(data.get("avatar")),
        "followers": _int(data.get("followersCount")),
        "follows": _int(data.get("followsCount")),
        "posts": _int(data.get("postsCount")),
        "created_at": _str(data.get("createdAt")),
    }


def actor_record(data: dict) -> dict:
    return {
        "did": _str(data.get("did")),
        "handle": _str(data.get("handle")),
        "display_name": _str(data.get("displayName")),
        "description": data.get("description") if isinstance(data.get("description"), str) else None,
        "avatar_url": _str(data.get("avatar")),
    }


def _flatten_replies(node: dict, depth: int, out: list) -> None:
    if depth <= 0 or not isinstance(node, dict):
        return
    for reply in node.get("replies") or []:
        if not isinstance(reply, dict) or not isinstance(reply.get("post"), dict):
            continue  # notFound / blocked nodes carry no post
        rec = post_record(reply)
        if rec:
            out.append(rec)
        _flatten_replies(reply, depth - 1, out)


async def social(ctx, payload: dict) -> dict:
    req = parse(payload)
    profile, posts, actors, thread = None, [], [], None

    if req["mode"] == "profile":
        async def get_profile(provider):
            return await provider.profile(req["actor"])

        async def get_feed(provider):
            return await provider.author_feed(req["actor"], limit=req["posts"])

        data = await ctx.run("profile", bluesky.PROVIDERS, get_profile, per_attempt_seconds=15, max_attempts=2)
        profile = profile_record(data)
        feed = await ctx.run("author_feed", bluesky.PROVIDERS, get_feed, per_attempt_seconds=15, max_attempts=2)
        posts = [p for p in (post_record(item) for item in feed["feed"] if isinstance(item, dict)) if p][:req["posts"]]
    elif req["mode"] == "search_actors":
        async def search(provider):
            return await provider.search_actors(req["query"], limit=req["limit"])

        data = await ctx.run("search_actors", bluesky.PROVIDERS, search, per_attempt_seconds=15, max_attempts=2)
        actors = [actor_record(a) for a in data["actors"] if isinstance(a, dict)][:req["limit"]]
    else:
        async def get_thread(provider):
            return await provider.thread(req["uri"], depth=req["depth"])

        data = await ctx.run("thread", bluesky.PROVIDERS, get_thread, per_attempt_seconds=15, max_attempts=2)
        root = data["thread"]
        if not isinstance(root.get("post"), dict):
            raise runtime.InvalidRequest("Bluesky has no visible post at that URI (not found or blocked).")
        replies: list = []
        _flatten_replies(root, req["depth"], replies)
        thread = {"post": post_record(root), "replies": replies, "reply_count": len(replies)}

    checked_at = _now()
    stamps = [p["indexed_at"] for p in posts if p.get("indexed_at")]
    if thread and thread["post"].get("indexed_at"):
        stamps.append(thread["post"]["indexed_at"])
    return {
        "mode": req["mode"],
        "actor": req["actor"],
        "query": req["query"],
        "uri": req["uri"],
        "profile": profile,
        "posts": posts,
        "actors": actors,
        "thread": thread,
        "post_count": len(posts),
        "actor_count": len(actors),
        "source": SOURCE,
        "as_of": max(stamps) if stamps else checked_at,
        "checked_at": checked_at,
    }


SKILLS = {"social.bluesky": social}
PRECHECKS = {"social.bluesky": precheck}
