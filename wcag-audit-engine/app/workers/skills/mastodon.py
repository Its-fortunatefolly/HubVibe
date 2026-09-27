"""social.mastodon -- a hashtag timeline, an account with its latest
statuses, an account or hashtag search, or the trending hashtags, from any
Mastodon instance's public API, keyless, live.

One worker, four modes, one result shape: `hashtag`, `account`, `search`
(accounts or hashtags) and `trends`. The instance defaults to
mastodon.social and must be a public hostname; the node never fetches a
private or internal address. Status search and the public firehose are not
offered: mastodon.social requires a token for both (verified 2026-09-27).

`as_of` is the newest status's creation time when there are statuses, else
`checked_at`. NEVER A GUESS: a count the instance omits is null; HTML is
kept in `content_html` and reduced to `text` without inventing anything.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from .. import runtime
from ..providers import mastodon

MODES = ("hashtag", "account", "search", "trends")
MAX_STATUSES = 40
MAX_SEARCH = 25
MAX_TRENDS = 20
MAX_TAG_CHARS = 100
MAX_QUERY_CHARS = 200
SOURCE = "mastodon-public-api"

_HOST = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)
_IPISH = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_TAG = re.compile(r"^[^\s#@/?&]{1,100}$")
_ACCT = re.compile(r"^[A-Za-z0-9_.-]{1,64}(?:@[A-Za-z0-9.-]{3,253})?$")
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp", ".arpa")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _str(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def validate_instance(raw) -> str:
    if raw is None:
        return mastodon.DEFAULT_INSTANCE
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest("`instance`, when given, must be a Mastodon server hostname such as mastodon.social.")
    host = raw.strip().lower().rstrip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    if _IPISH.match(host) or ":" in host or "." not in host or host.endswith(_BLOCKED_SUFFIXES) \
            or host == "localhost" or not _HOST.match(host):
        raise runtime.InvalidRequest("`instance` must be a public hostname (no IP addresses, ports or internal names).")
    return host


def _count(payload: dict, key: str, default: int, maximum: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise runtime.InvalidRequest(f"`{key}` must be a whole number from 1 to {maximum}.")
    return value


def parse(payload: dict) -> dict:
    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise runtime.InvalidRequest(f"`mode` must be one of {list(MODES)}.")
    req = {"mode": mode, "instance": validate_instance(payload.get("instance")), "tag": None, "acct": None,
           "query": None, "kind": None, "limit": None}
    if mode == "hashtag":
        tag = payload.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            raise runtime.InvalidRequest("`tag` is required for hashtag mode.")
        tag = tag.strip().lstrip("#")
        if not _TAG.match(tag) or len(tag) > MAX_TAG_CHARS:
            raise runtime.InvalidRequest(f"`tag` must be one hashtag word of up to {MAX_TAG_CHARS} characters.")
        req["tag"] = tag
        req["limit"] = _count(payload, "limit", 10, MAX_STATUSES)
    elif mode == "account":
        acct = payload.get("acct")
        if not isinstance(acct, str) or not _ACCT.match(acct.strip().lstrip("@")):
            raise runtime.InvalidRequest("`acct` is required for account mode: a username, optionally @domain (Gargron or Gargron@mastodon.social).")
        req["acct"] = acct.strip().lstrip("@")
        req["limit"] = _count(payload, "statuses", 10, MAX_STATUSES)
    elif mode == "search":
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            raise runtime.InvalidRequest("`query` is required for search mode.")
        if len(query.strip()) > MAX_QUERY_CHARS:
            raise runtime.InvalidRequest(f"`query` is over the {MAX_QUERY_CHARS}-character limit.")
        req["query"] = query.strip()
        kind = payload.get("kind", "accounts")
        if not isinstance(kind, str) or kind not in mastodon.SEARCH_KINDS:
            raise runtime.InvalidRequest(f"`kind` must be one of {list(mastodon.SEARCH_KINDS)}.")
        req["kind"] = kind
        req["limit"] = _count(payload, "limit", 10, MAX_SEARCH)
    else:
        req["limit"] = _count(payload, "limit", 10, MAX_TRENDS)
    return req


def precheck(payload: dict) -> None:
    parse(payload)


def status_record(status: dict) -> Optional[dict]:
    if not isinstance(status, dict) or not status.get("id"):
        return None
    reblog = status.get("reblog") if isinstance(status.get("reblog"), dict) else None
    content = status.get("content") if isinstance(status.get("content"), str) else ""
    if not content and reblog and isinstance(reblog.get("content"), str):
        content = reblog["content"]
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    media = [{"type": _str(m.get("type")), "url": _str(m.get("url"))}
             for m in (status.get("media_attachments") or []) if isinstance(m, dict) and _str(m.get("url"))]
    tags = [t["name"] for t in (status.get("tags") or []) if isinstance(t, dict) and _str(t.get("name"))]
    return {
        "id": str(status["id"]),
        "url": _str(status.get("url")) or _str(status.get("uri")),
        "created_at": _str(status.get("created_at")),
        "text": mastodon.strip_html(content),
        "content_html": content,
        "language": _str(status.get("language")),
        "visibility": _str(status.get("visibility")),
        "replies": _int(status.get("replies_count")),
        "reblogs": _int(status.get("reblogs_count")),
        "favourites": _int(status.get("favourites_count")),
        "is_reblog": reblog is not None,
        "reblog_of_url": (_str(reblog.get("url")) or _str(reblog.get("uri"))) if reblog else None,
        "author_acct": _str(account.get("acct")),
        "author_display_name": _str(account.get("display_name")),
        "media": media,
        "tags": tags,
        "sensitive": bool(status.get("sensitive")),
        "spoiler_text": status.get("spoiler_text") if isinstance(status.get("spoiler_text"), str) else "",
    }


def account_record(data: dict) -> dict:
    return {
        "id": str(data.get("id")) if data.get("id") is not None else None,
        "username": _str(data.get("username")),
        "acct": _str(data.get("acct")),
        "display_name": _str(data.get("display_name")),
        "url": _str(data.get("url")),
        "bio": mastodon.strip_html(data.get("note")),
        "avatar_url": _str(data.get("avatar")),
        "followers": _int(data.get("followers_count")),
        "following": _int(data.get("following_count")),
        "statuses": _int(data.get("statuses_count")),
        "created_at": _str(data.get("created_at")),
        "bot": data.get("bot") if isinstance(data.get("bot"), bool) else None,
    }


def hashtag_record(data: dict) -> dict:
    history = [h for h in (data.get("history") or []) if isinstance(h, dict)]
    uses = [_int(h.get("uses")) for h in history]
    accounts = [_int(h.get("accounts")) for h in history]
    return {
        "name": _str(data.get("name")) or "",
        "url": _str(data.get("url")),
        "uses_7d": sum(u for u in uses if u is not None) if any(u is not None for u in uses) else None,
        "accounts_7d": sum(a for a in accounts if a is not None) if any(a is not None for a in accounts) else None,
        "days": len(history),
    }


async def social(ctx, payload: dict) -> dict:
    req = parse(payload)
    instance = req["instance"]
    account, statuses, accounts, hashtags = None, [], [], []

    if req["mode"] == "hashtag":
        async def call(provider):
            return await provider.hashtag_timeline(instance, req["tag"], limit=req["limit"])

        data = await ctx.run("hashtag_timeline", mastodon.PROVIDERS, call, per_attempt_seconds=15, max_attempts=2)
        statuses = [s for s in (status_record(x) for x in data["statuses"]) if s][:req["limit"]]
    elif req["mode"] == "account":
        async def lookup(provider):
            return await provider.lookup(instance, req["acct"])

        data = await ctx.run("lookup", mastodon.PROVIDERS, lookup, per_attempt_seconds=15, max_attempts=2)
        account = account_record(data)

        async def get_statuses(provider):
            return await provider.statuses(instance, account["id"], limit=req["limit"], exclude_replies=True)

        rows = await ctx.run("statuses", mastodon.PROVIDERS, get_statuses, per_attempt_seconds=15, max_attempts=2)
        statuses = [s for s in (status_record(x) for x in rows["statuses"]) if s][:req["limit"]]
    elif req["mode"] == "search":
        async def search(provider):
            return await provider.search(instance, req["query"], kind=req["kind"], limit=req["limit"])

        data = await ctx.run("search", mastodon.PROVIDERS, search, per_attempt_seconds=15, max_attempts=2)
        if req["kind"] == "accounts":
            accounts = [account_record(a) for a in data["results"]][:req["limit"]]
        else:
            hashtags = [hashtag_record(h) for h in data["results"]][:req["limit"]]
    else:
        async def trends(provider):
            return await provider.trends(instance, limit=req["limit"])

        data = await ctx.run("trends", mastodon.PROVIDERS, trends, per_attempt_seconds=15, max_attempts=2)
        hashtags = [hashtag_record(h) for h in data["hashtags"]][:req["limit"]]

    checked_at = _now()
    stamps = [s["created_at"] for s in statuses if s.get("created_at")]
    return {
        "mode": req["mode"],
        "instance": instance,
        "tag": req["tag"],
        "acct": req["acct"],
        "query": req["query"],
        "kind": req["kind"],
        "account": account,
        "statuses": statuses,
        "accounts": accounts,
        "hashtags": hashtags,
        "status_count": len(statuses),
        "source": SOURCE,
        "as_of": max(stamps) if stamps else checked_at,
        "checked_at": checked_at,
    }


SKILLS = {"social.mastodon": social}
PRECHECKS = {"social.mastodon": precheck}
