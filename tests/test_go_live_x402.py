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


def test_it_deploys_the_source_and_not_only_the_variables():
    """`services update --update-env-vars` mints a revision carrying the SAME
    image. The variables change, the code does not -- so a service can report
    every variable correct while running an image from before the fixes those
    variables activate.

    That is not hypothetical: the #48 discovery checks failed against a live
    service whose config was entirely correct, because the running image
    predated #48. The only symptom was a checker failing where it passed
    locally, which reads as a broken checker rather than a stale deploy.
    """
    lines = [ln.strip() for ln in _text().splitlines() if not ln.strip().startswith("#")]
    invocation = [ln for ln in lines if "repair-and-deploy.sh" in ln]
    assert invocation, (
        "setting env vars is not a deploy -- the source has to ship too. "
        "A mention in a comment does not count; this must be an invocation."
    )
    text = _text()
    assert text.index("services update") < text.index(invocation[0])


def test_it_hands_off_rather_than_keeping_a_second_copy_of_the_deploy():
    """Two copies of the deploy logic drift, and the copy that drifts is
    always the one people are running. repair-and-deploy.sh owns the source
    deploy, its preflight, and the live verification."""
    text = _text()
    assert "gcloud run deploy" not in text
    assert "exec bash" in text


def test_it_does_not_point_at_the_facilitator_that_cannot_be_used():
    """CDP is gated on a business review that asks for a DBA this business
    does not have. It is unavailable, not pending."""
    text = _text()
    default = [ln for ln in text.splitlines() if ln.startswith("FACILITATOR=")]
    assert len(default) == 1
    assert "coinbase" not in default[0].lower()


def test_it_does_not_mint_a_revision_just_to_rewrite_identical_variables():
    """The variable step is skipped when both are already correct.

    The source deploy that follows is deliberately NOT conditional: there is
    no cheap way to know whether the running image matches the source, and
    reporting "nothing to do" while serving a stale image is precisely the
    failure this script was written to end.
    """
    assert "both variables already correct" in _text()
