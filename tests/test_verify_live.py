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


def _block(start_marker, end_marker):
    text = SCRIPT.read_text()
    start = text.index(start_marker)
    return text[start : text.index(end_marker, start)]


def _challenge_block():
    """The x402 challenge section, on its own."""
    return _block('echo "402 challenge is machine-actionable"', 'echo "MCP endpoint')


def _discovery_block():
    """The Bazaar / capability-discovery section, on its own."""
    return _block(
        'echo "Machine discovery:', "# A manifest that describes its inputs"
    )


_SPEC_ENTRY = {
    "scheme": "exact",
    "network": "base",
    "maxAmountRequired": "30000",
    "resource": "https://stub/audit/wcag",
    "description": "HubVibe site audit",
    "mimeType": "application/json",
    "payTo": "0x32b08c5e927c69877d0fcab35618c265674922bc",
    "maxTimeoutSeconds": 300,
    "asset": "0xa0b8",
    "extra": {},
}

_BAZAAR = {"bazaar": {"info": {"input": {"type": "http", "method": "POST"}}}}


def _run_x402_block(
    tmp_path,
    block,
    methods=("x402", "mpp-tempo", "stripe_api_key"),
    accepts=(),
    v2_header=True,
    bazaar=True,
    manifest_json=None,
):
    """Drive an x402-aware block against a stubbed node.

    `methods` is what /.well-known/agent.json claims; the rest is what the 402
    actually carries. The whole point of the checks under test is that those
    two can disagree, so the harness has to be able to make them disagree.
    """
    import json

    challenge = {
        "error": "payment_required",
        "price_usd": 0.03,
        "x402Version": 1,
        "accepts": list(accepts),
        # The API-key rail rides in other_rails, and a separate check asserts
        # the manifest and the challenge agree about it -- so the stub has to
        # keep those two in step or every run trips a check it is not testing.
        "other_rails": (
            [{"protocol": "api_key", "method": "stripe_api_key"}]
            if "stripe_api_key" in methods
            else []
        ),
    }
    if bazaar:
        challenge["extensions"] = _BAZAAR
    headers = "HTTP/2 402\r\nwww-authenticate: Payment realm=stub\r\n"
    if v2_header:
        headers += "payment-required: eyJ4NDAyVmVyc2lvbiI6Mn0=\r\n"
    manifest = (
        manifest_json
        if manifest_json is not None
        else json.dumps(
            {
                "payment": {"methods": list(methods)},
                "endpoints": [],
            }
        )
    )

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in *agent.json*) printf "%s" "$STUB_MANIFEST"; exit 0;; esac; done\n'
        'for a in "$@"; do [ "$a" = "-D" ] && { printf "%s" "$STUB_HEADERS"; exit 0; }; done\n'
        'printf "%s" "$STUB_CHALLENGE"\n'
    )
    (stub_dir / "curl").chmod(0o755)

    harness = tmp_path / "block.sh"
    harness.write_text(_HARNESS_PREAMBLE + block)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_MANIFEST"] = manifest
    env["STUB_HEADERS"] = headers
    env["STUB_CHALLENGE"] = json.dumps(challenge)
    # The challenge block computes this for itself and overwrites it; the
    # discovery block reads it as already-established state.
    env["X402_STATE"] = "on" if "x402" in methods else "off"
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=60
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_live_x402_rail_still_has_to_be_payable(tmp_path):
    """The 2026-08-27 checks, unchanged: when the manifest says x402 is on, the
    402 must carry a spec-shaped accepts[] and the v2 header."""
    result = _run_x402_block(tmp_path, _challenge_block(), accepts=[_SPEC_ENTRY])
    assert "FAIL|" not in result.stdout, result.stdout
    assert "spec-shaped" in result.stdout


def test_a_live_rail_with_an_invented_accepts_shape_still_fails(tmp_path):
    """The bug that made the rail unpayable for months. Making the checks
    state-aware must not have made this one skippable."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        accepts=[{"protocol": "x402", "price": "$0.03", "pay_to": "0xabc"}],
    )
    assert "FAIL|" in result.stdout
    assert "missing" in result.stdout


def test_a_deliberately_off_rail_is_green_not_red(tmp_path):
    """x402 switched off is a correct, fail-closed state -- not two red lines.

    A checker that reports FAIL for the state the operator just asked for is a
    checker people stop reading, and worse, it invites the next session to
    'fix' a rail that was turned off on purpose."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("mpp-tempo", "stripe_api_key"),
        accepts=[],
        v2_header=False,
    )
    assert "FAIL|" not in result.stdout, result.stdout
    assert "x402 is OFF and accepts[] is empty" in result.stdout
    assert "no v2 PAYMENT-REQUIRED header" in result.stdout


def test_an_off_rail_that_is_still_advertised_in_accepts_fails(tmp_path):
    """The failure that actually matters when the rail is off: the config said
    stop, and the 402 kept selling it anyway."""
    result = _run_x402_block(
        tmp_path,
        _challenge_block(),
        methods=("mpp-tempo",),
        accepts=[_SPEC_ENTRY],
        v2_header=False,
    )
    assert "FAIL|" in result.stdout
    assert "advertising a rail" in result.stdout


def test_an_off_rail_still_sending_the_v2_header_fails(tmp_path):
    """A v2 client reads PAYMENT-REQUIRED before it reads the body, so an
    orphaned header sells the rail on its own."""
    result = _run_x402_block(
        tmp_path, _challenge_block(), methods=("mpp-tempo",), accepts=[], v2_header=True
    )
    assert "FAIL|" in result.stdout
    assert "still carries a v2 PAYMENT-REQUIRED" in result.stdout


def test_an_unreadable_manifest_fails_rather_than_guessing(tmp_path):
    """Every x402 check asks a different question depending on the rail state.
    With no answer, the honest result is FAIL -- guessing 'off' would report a
    live unpayable rail as a clean run."""
    result = _run_x402_block(
        tmp_path, _challenge_block(), manifest_json="<html>502</html>"
    )
    assert "FAIL|" in result.stdout
    assert "no way to tell" in result.stdout


def test_an_off_rail_carrying_a_bazaar_record_fails(tmp_path):
    """The discovery record advertises this node to facilitators as a payable
    resource. Emitting it while the rail is off invites exactly the payment the
    shutdown exists to prevent."""
    result = _run_x402_block(
        tmp_path, _discovery_block(), methods=("mpp-tempo",), bazaar=True
    )
    assert "FAIL|" in result.stdout
    assert "still carries a Bazaar discovery record" in result.stdout


def test_an_off_rail_with_no_bazaar_record_is_a_counted_pass(tmp_path):
    """This branch used to print a NOTE and count nothing. A branch that cannot
    go red is exactly how 28/28 was reported over a dead paid path."""
    result = _run_x402_block(
        tmp_path, _discovery_block(), methods=("mpp-tempo",), bazaar=False
    )
    assert "FAIL|" not in result.stdout, result.stdout
    assert "PASS|" in result.stdout
    assert "nothing to index" in result.stdout


def test_a_live_rail_with_no_bazaar_record_fails(tmp_path):
    """Payable but unindexable is a real defect, not a footnote."""
    result = _run_x402_block(tmp_path, _discovery_block(), bazaar=False)
    assert "FAIL|" in result.stdout
    assert "no Bazaar record" in result.stdout


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
