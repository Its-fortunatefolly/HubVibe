"""Any Mastodon instance's public REST API, keyless.

The endpoints an instance serves to anyone (verified on mastodon.social,
2026-09-27): a hashtag timeline, an account lookup and its statuses,
search for accounts and hashtags, and trending hashtags. Status search and
the public firehose need a token on mastodon.social, so they are not
offered. The instance is the caller's choice (default mastodon.social);
the skill validates it as a public hostname before this adapter is asked.

Nothing is cached; the skill stamps `checked_at`. Cost is zero and known.
"""

import html
import os
import re
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_MASTODON_TIMEOUT_SECONDS", "20"))
USER_AGENT = os.environ.get("WORKER_MASTODON_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
DEFAULT_INSTANCE = os.environ.get("WORKER_MASTODON_DEFAULT_INSTANCE", "mastodon.social")
SEARCH_KINDS = ("accounts", "hashtags")

_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<br\s*/?>|</p>", re.IGNORECASE)


def strip_html(text) -> str:
    """Mastodon content is sanitized HTML; a plain-text reading keeps the
    paragraph breaks and decodes entities."""
    if not isinstance(text, str) or not text:
        return ""
    plain = _BREAKS.sub("\n", text)
    plain = _TAGS.sub("", plain)
    plain = html.unescape(plain)
    return "\n".join(line.strip() for line in plain.splitlines()).strip()


async def _get_json(instance: str, path: str, params: Optional[dict] = None) -> tuple:
    url = f"https://{instance}/api/{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = await client.get(url, params=params, headers={"User-Agent": USER_AGENT,
                                                                     "Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"{instance} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"{instance} unreachable: {exc}") from exc
    if response.status_code in (301, 302, 303, 307, 308):
        raise runtime.PermanentProviderError(f"{instance} redirected the API call; it may not be a Mastodon server.")
    if response.status_code in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"{instance} returned {response.status_code}",
                                             reason="provider_overloaded" if response.status_code == 429
                                             else "provider_transient")
    try:
        return response.status_code, response.json()
    except ValueError:
        raise runtime.InvalidProviderResponse(f"{instance} did not return JSON for {path}.") from None


def _raise_for_status(instance: str, status: int, data, what: str) -> None:
    if status < 400:
        return
    message = (data.get("error") if isinstance(data, dict) else None) or f"HTTP {status}"
    if status == 404:
        raise runtime.InvalidRequest(f"{instance}: {what} not found ({message}).")
    if status in (401, 403, 422):
        raise runtime.PermanentProviderError(f"{instance} requires authentication for {what} ({message}).")
    raise runtime.PermanentProviderError(f"{instance} returned {status} for {what}: {message}")


def _expect_list(instance: str, data, what: str) -> list:
    if not isinstance(data, list):
        raise runtime.InvalidProviderResponse(f"{instance} answered {what} with something other than a list.")
    return [d for d in data if isinstance(d, dict)]


class _MastodonPublic:
    id = "mastodon-public-api"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def hashtag_timeline(self, instance: str, tag: str, limit: int = 10) -> runtime.ProviderResult:
        status, data = await _get_json(instance, f"v1/timelines/tag/{tag}", {"limit": str(min(max(int(limit), 1), 40))})
        _raise_for_status(instance, status, data, f"hashtag #{tag}")
        rows = _expect_list(instance, data, "the hashtag timeline")
        return runtime.ProviderResult(value={"statuses": rows}, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 statuses={len(rows)}")

    async def lookup(self, instance: str, acct: str) -> runtime.ProviderResult:
        status, data = await _get_json(instance, "v1/accounts/lookup", {"acct": acct})
        _raise_for_status(instance, status, data, f"account {acct}")
        if not isinstance(data, dict) or not data.get("id"):
            raise runtime.InvalidProviderResponse(f"{instance} answered the lookup without an account id.")
        return runtime.ProviderResult(value=data, cost_micros=0, cost_measured=True, usage="requests=1")

    async def statuses(self, instance: str, account_id: str, limit: int = 10,
                       exclude_replies: bool = True) -> runtime.ProviderResult:
        params = {"limit": str(min(max(int(limit), 1), 40)), "exclude_replies": "true" if exclude_replies else "false"}
        status, data = await _get_json(instance, f"v1/accounts/{account_id}/statuses", params)
        _raise_for_status(instance, status, data, f"statuses of account {account_id}")
        rows = _expect_list(instance, data, "the account's statuses")
        return runtime.ProviderResult(value={"statuses": rows}, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 statuses={len(rows)}")

    async def search(self, instance: str, query: str, kind: str = "accounts", limit: int = 10) -> runtime.ProviderResult:
        kind = kind if kind in SEARCH_KINDS else "accounts"
        status, data = await _get_json(instance, "v2/search", {"q": query, "type": kind,
                                                               "limit": str(min(max(int(limit), 1), 40))})
        _raise_for_status(instance, status, data, "search")
        if not isinstance(data, dict) or not isinstance(data.get(kind), list):
            raise runtime.InvalidProviderResponse(f"{instance} answered the search without `{kind}`.")
        rows = [d for d in data[kind] if isinstance(d, dict)]
        return runtime.ProviderResult(value={"kind": kind, "results": rows}, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 {kind}={len(rows)}")

    async def trends(self, instance: str, limit: int = 10) -> runtime.ProviderResult:
        status, data = await _get_json(instance, "v1/trends/tags", {"limit": str(min(max(int(limit), 1), 20))})
        _raise_for_status(instance, status, data, "trending hashtags")
        rows = _expect_list(instance, data, "trends")
        return runtime.ProviderResult(value={"hashtags": rows}, cost_micros=0, cost_measured=True,
                                      usage=f"requests=1 hashtags={len(rows)}")


PROVIDERS = [_MastodonPublic()]
