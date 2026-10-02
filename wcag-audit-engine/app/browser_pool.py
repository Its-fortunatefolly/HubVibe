"""A reused, per-thread Chromium pool for every audit that needs a real browser.

Launching a browser costs roughly a second of wall time and a few hundred MB
of RSS. Doing that per request -- which is what this service used to do --
made process startup, not the actual audit, the dominant cost of a call and
put a hard ceiling on how many audits a single container could serve. Keeping
one browser alive per worker thread and opening a cheap, isolated context per
request removes that cost from the hot path entirely.

Why thread-local rather than a shared pool object: Playwright's *sync* API is
bound to the thread that created it and is not safe to drive from another
thread. FastAPI runs these sync routes in anyio's threadpool, which app.main
caps at MAX_CONCURRENT_AUDITS, so the number of live browsers is bounded by
that cap rather than growing without limit.

Isolation is still per request: each call gets a fresh BrowserContext (its own
cookie jar, storage, and cache), so one customer's audit can never observe or
be influenced by another's. Only the expensive process is shared.

Every job has a hard deadline. Navigation carries Playwright timeouts, but a
page.evaluate (axe-core, the DOM count) has none: a page whose script never
yields keeps the call waiting forever, and the thread with it. Those threads
are the MAX_CONCURRENT_AUDITS slots, so on 2026-10-01 two such pages held
both slots for four hours and every paid audit and MCP tool call behind them
hung. A watchdog now kills the browser process when a job outlives the
deadline; the pending call fails at once, the job is reported as an audit
that could not run (never billed), and the thread's next job launches a
fresh browser.
"""

import os
import signal
import threading
from typing import Callable, Optional, TypeVar

from playwright.sync_api import sync_playwright

T = TypeVar("T")

_state = threading.local()

# The longest one browser job may take: context, page, the caller's
# navigation and scripts, and the context close. The slowest legitimate job
# (the bundle: a 30 s networkidle load plus axe-core in every frame) fits
# well inside; a page that hangs is cut off here.
DEADLINE_SECONDS = float(os.environ.get("BROWSER_JOB_DEADLINE_SECONDS", "60"))


class BrowserDeadlineExceeded(RuntimeError):
    """The page did not finish within DEADLINE_SECONDS; its browser was killed."""


def _browser_pid(browser) -> Optional[int]:
    """The browser's OS process id, from the DevTools protocol.

    SystemInfo.getProcessInfo lists the browser process as type "browser";
    measured on this image (Playwright 1.48, Chromium 1140) it is the chrome
    process the Playwright driver launched.
    """
    try:
        session = browser.new_browser_cdp_session()
        try:
            info = session.send("SystemInfo.getProcessInfo")
        finally:
            try:
                session.detach()
            except Exception:
                pass
    except Exception:
        return None
    for process in info.get("processInfo") or []:
        if process.get("type") == "browser":
            try:
                return int(process["id"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _kill_browser(pid: int) -> None:
    """Kill the browser and everything it started.

    The driver launches the browser as its own process group, so the group
    holds the zygote, GPU and renderer processes too; killing the group
    leaves none of them behind. A pid that is not a group leader is killed
    alone.
    """
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class _Watchdog:
    """Kills `pid` if not cancelled within `seconds`."""

    def __init__(self, pid: Optional[int], seconds: float):
        self.fired = False
        self._timer = None
        if pid is not None and seconds > 0:
            self._timer = threading.Timer(seconds, self._fire, args=(pid,))
            self._timer.daemon = True
            self._timer.start()

    def _fire(self, pid: int) -> None:
        self.fired = True
        _kill_browser(pid)

    def cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

# --disable-dev-shm-usage matters specifically on Cloud Run: the container
# gets a small /dev/shm, and without this Chromium intermittently crashes
# mid-navigation on heavier pages rather than returning a result.
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _close_thread_browser() -> None:
    browser = getattr(_state, "browser", None)
    _state.browser = None
    _state.browser_pid = None
    if browser is not None:
        try:
            browser.close()
        except Exception:
            # Already dead or unreachable -- dropping the reference is the
            # whole point; a failure to close cleanly must not propagate.
            pass


def _get_browser():
    browser = getattr(_state, "browser", None)
    if browser is not None:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
        _close_thread_browser()

    playwright = getattr(_state, "playwright", None)
    if playwright is None:
        playwright = sync_playwright().start()
        _state.playwright = playwright

    browser = playwright.chromium.launch(args=_LAUNCH_ARGS)
    _state.browser = browser
    _state.browser_pid = _browser_pid(browser)
    return browser


def with_page(fn: Callable[..., T], *, deadline_seconds: Optional[float] = None,
              **context_kwargs) -> T:
    """Run `fn(page)` on a fresh context of this thread's pooled browser.

    Retries exactly once, and only when the pooled browser itself turns out
    to be dead (crashed, or reaped while the thread sat idle). An exception
    raised by `fn` is a real audit failure -- a navigation timeout, an
    unreachable host -- and is propagated unchanged rather than retried, so a
    genuinely failing audit still fails honestly instead of being masked by a
    second attempt.

    The whole job runs under the DEADLINE_SECONDS watchdog, or under
    `deadline_seconds` when the caller has a tighter budget of its own (the
    bundle answers routers whose clients give up at 30 s). When it fires,
    this raises BrowserDeadlineExceeded (not retried: the same page would
    hang the same way) and the thread's dead browser is dropped.
    """
    deadline = DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    last_error: Exception

    for attempt in (1, 2):
        browser = _get_browser()
        watchdog = _Watchdog(getattr(_state, "browser_pid", None), deadline)
        try:
            try:
                context = browser.new_context(**context_kwargs)
            except Exception as exc:
                # Could not even open a context: treat the browser as dead.
                _close_thread_browser()
                if watchdog.fired:
                    raise _deadline_error(deadline) from exc
                last_error = exc
                if attempt == 2:
                    raise
                continue

            try:
                page = context.new_page()
                return fn(page)
            except Exception as exc:
                if watchdog.fired:
                    raise _deadline_error(deadline) from exc
                raise
            finally:
                try:
                    context.close()
                except Exception:
                    # A context that won't close means the browser is unhealthy;
                    # drop it so the next request gets a fresh one.
                    _close_thread_browser()
        finally:
            watchdog.cancel()
            if watchdog.fired:
                _close_thread_browser()

    raise last_error


def _deadline_error(seconds: float = DEADLINE_SECONDS) -> BrowserDeadlineExceeded:
    return BrowserDeadlineExceeded(
        f"the page did not finish loading and auditing within {seconds:g} seconds"
    )
