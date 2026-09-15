"""Web page retrieval and extraction.

REUSES THE BROWSER THIS SERVICE ALREADY RUNS. The audit engine keeps a warm
per-thread Chromium pool (app/browser_pool.py); rendering a page for a worker
is the same operation an audit already performs. So this adapter does not
launch anything -- it is handed the pool's `with_page` at configure() time.

Two providers, in fallback order:
  1. browser   -- real Chromium, so JS-rendered pages extract correctly
  2. http-fetch -- plain GET, no JS, for when the browser is unavailable or
                   the page is static anyway

That fallback is not decoration: Chromium is the heaviest thing on the box and
the first casualty of memory pressure, and a static page extracts perfectly
well without it.

INJECTION RATHER THAN IMPORT: the workers package is loaded by file path in
this repo's tests, where `from .. import browser_pool` does not resolve. The
pool arrives through configure(), which also keeps this module importable --
and unit-testable -- with no browser present at all.
"""

import asyncio
import os
import re
from typing import Callable, Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_TIMEOUT = float(os.environ.get("WORKER_FETCH_TIMEOUT_SECONDS", "45"))
MAX_TEXT_CHARS = int(os.environ.get("WORKER_MAX_EXTRACT_CHARS", "40000"))

_with_page: Optional[Callable] = None
_executor = None
_goto_guarded: Optional[Callable] = None


def configure(with_page: Optional[Callable] = None, executor=None,
              goto_guarded: Optional[Callable] = None) -> None:
    """Hand this module the audit engine's browser pool.

    `goto_guarded` is the audit code's own navigation guard (SSRF blocking,
    redirect limits). Reusing it means a worker cannot be pointed at a private
    address that the audit routes already refuse -- one guard, not two.
    """
    global _with_page, _executor, _goto_guarded
    _with_page = with_page
    _executor = executor
    _goto_guarded = goto_guarded


_TAG_STRIP = re.compile(r"<(script|style|noscript|template)[^>]*>.*?</\1>",
                        re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    text = _TAG_STRIP.sub(" ", html or "")
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = _TAGS.sub(" ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", "\n".join(l.strip() for l in text.splitlines())).strip()


def _title_of(html: str) -> Optional[str]:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    return _WS.sub(" ", _TAGS.sub("", match.group(1))).strip() if match else None


def _meta_description(html: str) -> Optional[str]:
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        html or "", re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _truncate(text: str) -> tuple:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


class _BrowserExtractor:
    id = "browser"

    def available(self) -> bool:
        return _with_page is not None and _executor is not None

    def unavailable_reason(self) -> str:
        return "browser pool not configured on this deployment"

    async def extract(self, url: str) -> runtime.ProviderResult:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())

        def _render(page):
            if _goto_guarded is not None:
                _goto_guarded(page, url, wait_until="networkidle", timeout=30000)
            else:  # pragma: no cover - only when the guard was not injected
                page.goto(url, wait_until="networkidle", timeout=30000)
            return {
                "title": page.title(),
                "text": page.evaluate("document.body ? document.body.innerText : ''"),
                "html_length": page.evaluate("document.documentElement.outerHTML.length"),
                "links": page.evaluate(
                    "Array.from(document.querySelectorAll('a[href]')).slice(0,100)"
                    ".map(a => ({text: (a.innerText||'').trim().slice(0,120), href: a.href}))"),
                "description": page.evaluate(
                    "(document.querySelector('meta[name=\\\"description\\\"]')||{}).content || null"),
                "final_url": page.url,
            }

        loop = asyncio.get_running_loop()
        try:
            rendered = await loop.run_in_executor(_executor, lambda: _with_page(_render, user_agent=USER_AGENT))
        except Exception as exc:
            # A navigation failure is the SITE failing, not the browser. Still
            # transient from our side: sites time out and come back.
            raise runtime.TransientProviderError(
                f"Could not render {url}: {type(exc).__name__}: {exc}") from exc

        text, truncated = _truncate((rendered.get("text") or "").strip())
        if not text:
            raise runtime.InvalidProviderResponse(f"{url} rendered with no readable text.")
        return runtime.ProviderResult(
            value={"url": url, "final_url": rendered.get("final_url") or url,
                   "title": rendered.get("title"), "description": rendered.get("description"),
                   "text": text, "text_chars": len(text), "truncated": truncated,
                   "links": rendered.get("links") or [], "rendered": True},
            # Our own flat-rate box: the marginal provider cost really is zero.
            cost_micros=0, cost_measured=True, usage=f"chars={len(text)}")


class _HttpExtractor:
    id = "http-fetch"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def extract(self, url: str) -> runtime.ProviderResult:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True,
                                         max_redirects=5) as client:
                response = await client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{url} unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(f"{url} returned {response.status_code}")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"{url} returned {response.status_code}; nothing to extract.")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise runtime.PermanentProviderError(
                f"{url} is {content_type or 'an unknown type'}; this worker extracts "
                "HTML and text.")

        html = response.text
        text, truncated = _truncate(_html_to_text(html))
        if not text:
            raise runtime.InvalidProviderResponse(f"{url} contained no readable text.")
        return runtime.ProviderResult(
            value={"url": url, "final_url": str(response.url), "title": _title_of(html),
                   "description": _meta_description(html), "text": text,
                   "text_chars": len(text), "truncated": truncated, "links": [],
                   "rendered": False},
            cost_micros=0, cost_measured=True, usage=f"chars={len(text)}")


PROVIDERS = [_BrowserExtractor(), _HttpExtractor()]
