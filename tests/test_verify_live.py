"""Guards for scripts/verify-live.sh's paid-path check.

Why this file exists: verify-live.sh reported 28/28 passing while the live
service returned HTTP 500 to every authenticated caller. It only ever asserted
that paid routes refuse UNauthenticated requests -- the cheap half of the
contract -- so the revenue path could be completely dead and the checker would
still be green.

The paid-path block is extracted and driven against a stubbed curl here, so
each HTTP status it can encounter is exercised without touching the network or
spending money. The distinctions matter: 500 and 402 and 502 have entirely
different causes, and a checker that collapses them is barely better than none.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify-live.sh"

_HARNESS_PREAMBLE = """#!/usr/bin/env bash
set -uo pipefail
BASE="https://stub"
PASSES=0; FAILURES=0
pass() { echo "PASS|$1"; PASSES=$((PASSES+1)); }
fail() { echo "FAIL|$1"; FAILURES=$((FAILURES+1)); }
"""


def _paid_path_block():
    """The paid-path section of the script, on its own."""
    text = SCRIPT.read_text()
    start = text.index('echo "The paid path:')
    end = text.index('echo "-----------------------------------------------"', start)
    return text[start:end]


def _run_block(tmp_path, status, body='{"error":"x"}', api_key="k"):
    """Drive the paid-path block with curl stubbed to return `status`."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text(
        '#!/usr/bin/env bash\n'
        'out=""\n'
        'for ((i=1;i<=$#;i++)); do a=${!i}; [ "$a" = "-o" ] && { j=$((i+1)); out=${!j}; }; done\n'
        f"[ -n \"$out\" ] && cat > \"$out\" <<'BODY'\n{body}\nBODY\n"
        f'printf "%s" "{status}"\n'
    )
    (stub_dir / "curl").chmod(0o755)

    harness = tmp_path / "block.sh"
    harness.write_text(_HARNESS_PREAMBLE + _paid_path_block())
    # The block resolves the API key via lib-api-key.sh, found relative to
    # $BASH_SOURCE -- which here is this harness, not scripts/. Copy the real
    # lib next to it so the resolution path under test is the real one.
    shutil.copy(REPO_ROOT / "scripts" / "lib-api-key.sh", tmp_path / "lib-api-key.sh")

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env.pop("HUBVIBE_API_KEY", None)
    if api_key is not None:
        env["HUBVIBE_API_KEY"] = api_key

    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=60
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_real_audit_result_passes(tmp_path):
    result = _run_block(tmp_path, "200", body='{"pass":true,"violations":[]}')
    assert "PASS|" in result.stdout
    assert "FAIL|" not in result.stdout


def test_a_200_without_an_audit_result_is_a_failure(tmp_path):
    """A 200 carrying an error object is still a dead path. Checking only the
    status code would call that healthy."""
    result = _run_block(tmp_path, "200", body='{"error":"nope"}')
    assert "FAIL|" in result.stdout
    assert "no audit result" in result.stdout


def test_500_fails_loudly_and_points_at_the_traceback(tmp_path):
    """The exact production outage. A 500 here means the paid path is dead;
    the only useful next step is the server-side traceback."""
    result = _run_block(tmp_path, "500", body="Internal Server Error")
    assert "FAIL|" in result.stdout
    assert "PAID PATH IS DEAD" in result.stdout
    assert "gcloud logging read" in result.stdout


def test_402_fails_and_names_the_key_store_as_a_possible_cause(tmp_path):
    """Now that a dead key store degrades to 402 rather than 500, a 402 on an
    authenticated call is ambiguous between 'bad key' and 'no key store'. The
    message has to say so or the next debugging step is a guess."""
    result = _run_block(tmp_path, "402")
    assert "FAIL|" in result.stdout
    assert "key store" in result.stdout


def test_502_is_not_a_failure_because_auth_worked(tmp_path):
    """502 means the audit could not run against the target site. Nothing was
    billed and the paid path is alive -- failing the deploy check over someone
    else's website being down is how a checker gets ignored."""
    result = _run_block(tmp_path, "502")
    assert "FAIL|" not in result.stdout
    assert "paid path is alive" in result.stdout


def test_an_unexpected_status_still_fails(tmp_path):
    result = _run_block(tmp_path, "418")
    assert "FAIL|" in result.stdout


def test_skipping_without_a_key_is_loud_not_silent(tmp_path):
    """Silence is what let the outage live. A skipped paid-path check must say
    that the most important thing was not verified."""
    result = _run_block(tmp_path, "200", api_key=None)
    assert "SKIP" in result.stdout
    assert "NOT verified" in result.stdout
    assert "FAIL|" not in result.stdout


def test_the_paid_check_uses_the_cheapest_route():
    """It spends real money on every run; $0.03 rather than the $0.10 bundle."""
    block = _paid_path_block()
    assert "/audit/wcag" in block
    assert "/audit/bundle" not in block


def test_the_paid_check_sends_a_resolved_key_not_a_bare_export():
    """It used to require `export HUBVIBE_API_KEY=...`, so a fresh shell meant
    the paid path was skipped -- which is how the one check that answers "can
    this take money" went unrun while everything else looked green."""
    block = _paid_path_block()
    assert "X-API-Key: $PAID_KEY" in block
    assert "hv_resolve_api_key" in block
    assert "X-API-Key: $HUBVIBE_API_KEY" not in block


@pytest.mark.parametrize("status", ["200", "500", "402", "502"])
def test_every_documented_status_has_a_branch(status):
    """A status that falls through to the catch-all reports a bare number and
    no next step, which is the failure mode this whole file exists to prevent."""
    assert re.search(rf"^\s+{status}\)", _paid_path_block(), re.MULTILINE)


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
