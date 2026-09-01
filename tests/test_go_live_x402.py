"""Static guards on scripts/go-live-x402.sh.

The script turns on the rail that earns. Its failure modes are all silent --
setting one variable without the other leaves x402 inert, a malformed address
leaves it advertised and unpayable -- so the properties that stop those are
worth pinning even though the script itself needs live gcloud to run.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go-live-x402.sh"


def _text() -> str:
    return SCRIPT.read_text()


def test_the_script_exists_and_is_valid_bash():
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


# --- the recipient nobody can claim ----------------------------------------
#
# Added after 0x2b3b...0256 was found deployed on this service as
# X402_PAY_TO_ADDRESS, unrecognised by the owner. Every gate in this repo said
# yes to it, because every gate checked shape. These run the script rather
# than reading it: the branch order is the whole behaviour, and a well-formed
# unaffirmed address must lose to the "replace it" path, not to "already set".

UNRECOGNISED = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"
TEST_CONSTANT = "0x32b08c5e927c69877d0fcab35618c265674922bc"
OWNER_WALLET = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"


def _drive(tmp_path, live_pay_to="", env=None):
    """Run the script with gcloud, the minter and the deploy handoff stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    svc = tmp_path / "service.json"
    env_list = [{"name": "X402_FACILITATOR_URL", "value": "https://facilitator.xpay.sh"}]
    if live_pay_to:
        env_list.append({"name": "X402_PAY_TO_ADDRESS", "value": live_pay_to})
    svc.write_text(json.dumps(
        {"spec": {"template": {"spec": {"containers": [{"env": env_list}]}}}}
    ))

    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"services describe"*) cat "{svc}" ;;\n'
        '  *"secrets versions access"*) printf "%s" "sk_live_fake" ;;\n'
        '  *"services update"*) echo "UPDATE_INVOKED $*" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)

    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_repo / "scripts" / "go-live-x402.sh").write_text(SCRIPT.read_text())
    (fake_repo / "scripts" / "repair-and-deploy.sh").write_text(
        "#!/usr/bin/env bash\necho DEPLOY_HANDOFF\n"
    )
    # Stands in for the Stripe mint, which needs network and a live account.
    (fake_repo / "scripts" / "x402-setup.py").write_text(
        "print('address: 0x9999999999999999999999999999999999999999')\n"
    )

    full_env = dict(os.environ)
    full_env["PATH"] = f"{bin_dir}:{full_env['PATH']}"
    full_env.pop("X402_PAY_TO_ADDRESS", None)
    if env:
        full_env.update(env)

    return subprocess.run(
        ["bash", str(fake_repo / "scripts" / "go-live-x402.sh")],
        capture_output=True, text=True, env=full_env, timeout=60,
    )


def test_a_deployed_unrecognised_address_is_not_kept_for_being_well_formed(tmp_path):
    result = _drive(tmp_path, live_pay_to=UNRECOGNISED)
    assert "NOBODY HERE HOLDS THE KEY TO" in result.stdout
    assert "already set and well-formed" not in result.stdout
    assert UNRECOGNISED not in "".join(
        ln for ln in result.stdout.splitlines() if "UPDATE_INVOKED" in ln
    )


def test_the_test_suite_constant_is_refused_too(tmp_path):
    """It exists to make the rail inspectable locally. Nobody holds its key,
    and a truncated paste of it was deployed on the tempo rail once."""
    result = _drive(tmp_path, live_pay_to=TEST_CONSTANT)
    assert "NOBODY HERE HOLDS THE KEY TO" in result.stdout


def test_supplying_an_unaffirmed_address_by_hand_stops_the_run(tmp_path):
    """A paste is how it got there in the first place."""
    result = _drive(tmp_path, env={"X402_PAY_TO_ADDRESS": UNRECOGNISED})
    assert "nobody here holds the key to" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout
    assert "DEPLOY_HANDOFF" not in result.stdout


def test_an_affirmed_address_still_goes_through(tmp_path):
    """The guard must refuse two specific addresses, not become a third gate
    that blocks the wallet this rail is meant to pay into."""
    result = _drive(tmp_path, env={"X402_PAY_TO_ADDRESS": OWNER_WALLET})
    assert f"X402_PAY_TO_ADDRESS={OWNER_WALLET}" in result.stdout
    assert "DEPLOY_HANDOFF" in result.stdout


def test_it_never_tries_to_mint_an_x402_address_from_stripe(tmp_path):
    """Stripe does MPP, not x402. The old fallback minted a Stripe-custodied
    deposit address here and, when that failed, told you to ask Stripe support
    to enable machine payments -- advice for a product Stripe does not sell.
    Following it costs a support thread that cannot resolve, which is the same
    shape of wrong diagnosis that already cost weeks on the CDP review.

    With no address anywhere, the script must say so and stop, not reach for
    Stripe.
    """
    result = _drive(tmp_path, live_pay_to="")
    assert "Stripe does not do x402" in result.stdout
    assert "self-custody" in result.stdout
    assert "support" not in result.stdout.lower()
    assert "UPDATE_INVOKED" not in result.stdout
    assert "DEPLOY_HANDOFF" not in result.stdout


def test_the_stripe_key_is_not_even_read_for_x402(tmp_path):
    """Reading the Stripe secret at all implies Stripe has a role on this rail.
    It does not -- the key is for MPP, and go-live.sh reads it there."""
    text = SCRIPT.read_text()
    assert "x402-setup.py" not in text
