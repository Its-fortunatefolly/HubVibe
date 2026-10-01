"""A browser job that never returns must not hold an audit slot forever.

On 2026-10-01 two pages whose scripts never yielded held both
MAX_CONCURRENT_AUDITS slots for four hours: page.evaluate has no timeout, so
every paid audit, every paid MCP tool call and every static file behind them
hung. browser_pool now kills the browser when a job outlives its deadline.

These tests drive with_page with a stand-in browser whose page blocks until
its browser process is killed, which is what a real hung evaluate does (the
kill itself was proven against Chromium 1140 on the deploy image).
"""

import importlib.util
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "browser_pool.py"


@pytest.fixture
def pool(monkeypatch):
    spec = importlib.util.spec_from_file_location("browser_pool_deadline_test", POOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 0.3)
    return module


class _Browser:
    """One browser process. Killing it fails whatever call is in flight."""

    launched = 0

    def __init__(self):
        type(self).launched += 1
        self.pid = 4000 + type(self).launched
        self.killed = threading.Event()
        self.closed = False

    def new_context(self, **kwargs):
        if self.killed.is_set():
            raise RuntimeError("Browser has been closed")
        return _Context(self)

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, browser):
        self.browser = browser

    def new_page(self):
        return _Page(self.browser)

    def close(self):
        if self.browser.killed.is_set():
            raise RuntimeError("Target page, context or browser has been closed")


class _Page:
    def __init__(self, browser):
        self.browser = browser

    def evaluate(self, script):
        if script == "hang":
            # Like page.evaluate on a script that never yields: no timeout,
            # returns only when the browser dies under it.
            self.browser.killed.wait()
            raise RuntimeError("Target page, context or browser has been closed")
        return 2


def _install(pool, monkeypatch):
    browsers = []
    kills = []

    def get_browser():
        browser = getattr(pool._state, "browser", None)
        if browser is None:
            browser = _Browser()
            browsers.append(browser)
            pool._state.browser = browser
            pool._state.browser_pid = browser.pid
        return browser

    def kill(pid):
        kills.append(pid)
        for browser in browsers:
            if browser.pid == pid:
                browser.killed.set()

    monkeypatch.setattr(pool, "_get_browser", get_browser)
    monkeypatch.setattr(pool, "_kill_browser", kill)
    return browsers, kills


def test_a_hung_job_is_killed_at_the_deadline_and_reported(pool, monkeypatch):
    browsers, kills = _install(pool, monkeypatch)
    started = time.monotonic()
    with pytest.raises(pool.BrowserDeadlineExceeded, match="within 0.3 seconds"):
        pool.with_page(lambda page: page.evaluate("hang"))
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"the hung job held its thread for {elapsed:.1f}s"
    assert kills == [browsers[0].pid]
    # The dead browser is dropped, so the thread cannot reuse it.
    assert getattr(pool._state, "browser", None) is None


def test_the_next_job_on_the_thread_gets_a_fresh_browser(pool, monkeypatch):
    browsers, kills = _install(pool, monkeypatch)
    with pytest.raises(pool.BrowserDeadlineExceeded):
        pool.with_page(lambda page: page.evaluate("hang"))
    assert pool.with_page(lambda page: page.evaluate("1 + 1")) == 2
    assert len(browsers) == 2 and kills == [browsers[0].pid]


def test_a_job_that_finishes_in_time_is_never_killed(pool, monkeypatch):
    browsers, kills = _install(pool, monkeypatch)
    assert pool.with_page(lambda page: page.evaluate("1 + 1")) == 2
    time.sleep(pool.DEADLINE_SECONDS * 2)
    assert kills == [] and len(browsers) == 1
    # The same browser serves the next job: nothing was torn down.
    assert pool.with_page(lambda page: page.evaluate("1 + 1")) == 2
    assert len(browsers) == 1


def test_an_ordinary_failure_is_still_the_callers_error(pool, monkeypatch):
    """Only a deadline becomes BrowserDeadlineExceeded; a navigation error
    raised in time propagates unchanged, as before."""
    _install(pool, monkeypatch)

    def fail(page):
        raise ValueError("net::ERR_NAME_NOT_RESOLVED")

    with pytest.raises(ValueError, match="ERR_NAME_NOT_RESOLVED"):
        pool.with_page(fail)


def test_without_a_known_pid_the_job_runs_unguarded_rather_than_failing(pool, monkeypatch):
    """If the browser's pid could not be read, the watchdog has nothing to
    kill; the job still runs (as before this change) instead of erroring."""
    _install(pool, monkeypatch)
    real_get = pool._get_browser

    def get_browser_without_pid():
        browser = real_get()
        pool._state.browser_pid = None
        return browser

    monkeypatch.setattr(pool, "_get_browser", get_browser_without_pid)
    assert pool.with_page(lambda page: page.evaluate("1 + 1")) == 2


def test_kill_takes_the_whole_process_group_when_the_browser_leads_it(pool, monkeypatch):
    calls = []
    monkeypatch.setattr(pool.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(pool.os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    monkeypatch.setattr(pool.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    pool._kill_browser(4242)
    assert calls == [("killpg", 4242, pool.signal.SIGKILL)]

    calls.clear()
    monkeypatch.setattr(pool.os, "getpgid", lambda pid: 1)
    pool._kill_browser(4242)
    assert calls == [("kill", 4242, pool.signal.SIGKILL)]

    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(pool.os, "getpgid", gone)
    pool._kill_browser(4242)  # already dead: no error
