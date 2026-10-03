"""The daily check must not call a working tool broken.

A job still running at the node's hand-back limit answers 202 with a link to
collect. That is the product working, and the checker used to count it as a
failed delivery -- four long-running tools were flagged on 2026-10-03, and the
settlement that arrived later was then counted against the NEXT tool, flagging
two more that had nothing wrong with them. The checker now collects the job
the way a buyer does.

And an empty list of tools to check must never mean "check everything":
that would generate a video and an image on every failed index read.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "simulate-work-calls.py"


def _load():
    spec = importlib.util.spec_from_file_location("simulate_work_calls_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SIM = _load()


class _Response:
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class _Http:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return _Response(status, retry_after="3" if status == 202 else None)


def test_a_handed_back_job_is_collected_until_it_delivers():
    http, slept = _Http([202, 202, 200]), []
    response = SIM.collect_job(http, "http://node/work/jobs/abc", 60, sleep=slept.append)
    assert response.status_code == 200
    assert http.urls == ["http://node/work/jobs/abc"] * 3
    assert slept == [3.0, 3.0], "it waits what Retry-After asks between polls"


def test_a_failed_job_is_returned_as_its_failure():
    response = SIM.collect_job(_Http([202, 502]), "http://node/work/jobs/abc", 60, sleep=lambda s: None)
    assert response.status_code == 502


def test_a_job_that_never_finishes_is_reported_not_waited_on_forever():
    clock = iter(range(0, 1000, 10))
    response = SIM.collect_job(_Http([202]), "http://node/work/jobs/abc", 25,
                               sleep=lambda s: None, clock=lambda: next(clock))
    assert response.status_code == 202, "the caller reports this as not delivered"


def test_the_checker_collects_a_202_and_checks_nothing_was_charged_first():
    source = SCRIPT.read_text()
    assert "paid.status_code == 202" in source and "collect_job(http" in source
    assert "nothing charged before delivery" in source


def test_an_empty_only_list_is_refused_rather_than_run_as_everything():
    result = subprocess.run([sys.executable, str(SCRIPT), "--only", ""],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stdout[-400:] + result.stderr[-400:]
    assert "refusing to run the whole catalog" in result.stderr


def test_the_gate_does_not_sweep_when_it_has_no_list_of_tools():
    script = (REPO_ROOT / "scripts" / "box-checks.sh").read_text()
    guard = script.index('if [ -z "$only" ]; then')
    sweep = script.index("simulate-work-calls.py --only")
    assert guard < sweep, "the sweep must sit behind the empty-list guard"
    assert "not run: the node's /work index could not be read" in script


def test_the_run_stops_itself_before_the_gate_kills_it():
    """box-checks.sh kills the sweep at 1,500 s with no summary. The checker
    must give up first, say which tools it did not reach, and print what it
    did check; and one slow job must not be waited on past that budget."""
    source = SCRIPT.read_text()
    assert SIM.RUN_BUDGET_SECONDS < 1500
    assert "timeout 1500" in (REPO_ROOT / "scripts" / "box-checks.sh").read_text()
    assert "out of time after" in source and "not checked:" in source
    assert "budget_ends - time.monotonic()" in source
    assert "worker.max_seconds + 30" in source


def test_a_connection_lost_while_collecting_is_a_reported_failure_not_a_crash():
    source = SCRIPT.read_text()
    collect_at = source.index("paid = collect_job(http")
    guarded = source[collect_at - 200:collect_at + 400]
    assert "except httpx.HTTPError" in guarded and "while collecting the job" in guarded


def test_a_job_charged_before_delivery_is_marked_as_failed_in_the_summary():
    source = SCRIPT.read_text()
    assert 'row.get("handed_back", True)' in source
