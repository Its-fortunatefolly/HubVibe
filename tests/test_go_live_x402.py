"""Static guards on scripts/go-live-x402.sh.

The script turns on the rail that earns. Its failure modes are all silent --
setting one variable without the other leaves x402 inert, a malformed address
leaves it advertised and unpayable -- so the properties that stop those are
worth pinning even though the script itself needs live gcloud to run.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go-live-x402.sh"


def _text() -> str:
    return SCRIPT.read_text()


def test_the_script_exists_and_is_valid_bash():
    import subprocess

    assert SCRIPT.is_file()
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, capture_output=True)


def test_it_reads_json_not_flattened():
    """`gcloud --format=flattened` pads names with alignment spaces, so any
    grep against it silently matches nothing. That shipped three times in this
    repo, once reporting nothing at all -- which read as a pass."""
    # The word appears in a comment explaining why it is not used, so match
    # the actual flag rather than the string.
    assert "--format=flattened" not in _text()
    assert "--format=json" in _text()


def test_it_sets_both_variables_or_neither():
    """x402 needs a facilitator AND a destination. Setting one alone leaves
    the rail inert, which is the quiet failure this script exists to prevent."""
    text = _text()
    assert "X402_FACILITATOR_URL=$FACILITATOR" in text
    assert "X402_PAY_TO_ADDRESS=$PAY_TO" in text


def test_a_supplied_address_is_shape_checked_before_it_can_be_deployed():
    """The self-custodial escape hatch must not be a way to smuggle a
    malformed address past the guard that exists for exactly that."""
    text = _text()
    marker = 'X402_PAY_TO_ADDRESS from the environment is not 0x + 40 hex'
    assert marker in text
    idx = text.index(marker)
    assert r"grep -qiE '^0x[0-9a-f]{40}$'" in text[:idx]


def test_the_zero_address_is_rejected_even_though_it_is_well_formed():
    """USDC reverts transfers to address(0). It passes the 40-hex check, and
    this exact value shipped once."""
    assert "0x0000000000000000000000000000000000000000" in _text()


def test_it_verifies_against_the_live_service_after_deploying():
    """Green tests do not prove a deploy. A route that passed every local
    test still 500'd in the container."""
    text = _text()
    assert "verify-live.sh" in text
    assert text.index("services update") < text.index("verify-live.sh")


def test_it_does_not_point_at_the_facilitator_that_cannot_be_used():
    """CDP is gated on a business review that asks for a DBA this business
    does not have. It is unavailable, not pending."""
    text = _text()
    default = [ln for ln in text.splitlines() if ln.startswith("FACILITATOR=")]
    assert len(default) == 1
    assert "coinbase" not in default[0].lower()


def test_it_is_safe_to_re_run():
    """A correct deployment must mint no revision -- Cloud Run revisions were
    once created on every run of a script that was supposed to be idempotent."""
    assert "nothing to change -- no revision minted" in _text()
