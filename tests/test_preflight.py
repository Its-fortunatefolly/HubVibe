"""Guards for the preflight in scripts/repair-and-deploy.sh.

Four separate live-environment problems reached production undetected: a
missing Firestore database (every authenticated call 500'd), a secret holding
a test string where a Stripe key belonged, min-instances=1 burning ~$137/month
against no traffic, and a pay-to address one character short of a valid EVM
address.

They have nothing in common except that nothing verified the deployed
environment. Tests passed, deploys succeeded, and verify-live.sh reported
28/28 while the paid path was dead. The preflight closes that gap by refusing
to deploy into an environment it can see is broken.

The pay-to check gets the most attention here because its first version was
itself an instance of the bug: it grepped `flattened` output for
`name: X402_PAY_TO_ADDRESS`, gcloud pads that format with alignment spaces,
so the pattern matched nothing and the check silently skipped -- producing
output that looked exactly like a pass. Every branch is asserted now,
including "not configured", so silence is never the answer.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "repair-and-deploy.sh"

GOOD_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922bc"   # 0x + 40 hex
SHORT_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922b"    # 0x + 39 hex

_PAY_TO_GOOD = [{"name": "X402_PAY_TO_ADDRESS", "value": GOOD_ADDR}]
_PAY_TO_SHORT = [{"name": "X402_PAY_TO_ADDRESS", "value": SHORT_ADDR}]
_PAY_TO_SECRET = [
    {"name": "X402_PAY_TO_ADDRESS",
     "valueFrom": {"secretKeyRef": {"name": "s", "key": "latest"}}}
]
_FACILITATOR_ONLY = [{"name": "X402_FACILITATOR_URL", "value": "https://f.example"}]
_NO_X402 = [{"name": "PUBLIC_BASE_URL", "value": "https://x"}]


def _stub(env=None, firestore_ok=True, min_scale="0"):
    body = json.dumps({
        "spec": {"template": {
            "metadata": {"annotations": {"autoscaling.knative.dev/minScale": min_scale}},
            "spec": {"containers": [{"env": env if env is not None else _PAY_TO_GOOD}]},
        }}
    })
    firestore = "echo ok; exit 0" if firestore_ok else "exit 1"
    return f"""#!/usr/bin/env bash
case "$*" in
  *"secrets describe"*) exit 0 ;;
  *"firestore databases describe"*) {firestore} ;;
  *"--format=json"*) cat <<'J'
{body}
J
    ;;
  *minScale*) echo "{min_scale}" ;;
  *"run services describe"*)
    printf 'name:  STRIPE_SECRET_KEY\\n  secretKeyRef.name:  SECRET_STRIPE_KEY\\n' ;;
  *"run services update"*) echo "UPDATE_INVOKED" ;;
  *"run deploy"*) echo "DEPLOY_INVOKED" ;;
  *) exit 0 ;;
esac
"""


def _run(tmp_path, **kwargs):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gcloud").write_text(_stub(**kwargs))
    (bin_dir / "gcloud").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # verify-live.sh makes real network calls with retries; the preflight is
    # what is under test here, not the post-deploy check.
    env["SKIP_VERIFY"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=120
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_missing_firestore_database_blocks_the_deploy(tmp_path):
    """The outage that returned 500 to every authenticated caller."""
    result = _run(tmp_path, firestore_ok=False)
    assert "no Firestore" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_a_short_pay_to_address_blocks_the_deploy(tmp_path):
    """39 hex characters looks right at a glance. The service would advertise
    a crypto rail while every settlement fails -- indistinguishable, from
    outside, from nobody buying."""
    result = _run(tmp_path, env=_PAY_TO_SHORT)
    assert "NOT a valid EVM address" in result.stdout
    assert "39" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_a_well_formed_pay_to_address_passes(tmp_path):
    result = _run(tmp_path, env=_PAY_TO_GOOD)
    assert "well-formed EVM address" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_secret_backed_address_is_reported_as_unchecked(tmp_path):
    """Its value cannot be read here. Claiming a pass would be worse than
    saying nothing, so it says exactly what it did not check."""
    result = _run(tmp_path, env=_PAY_TO_SECRET)
    assert "comes from Secret Manager" in result.stdout
    assert "well-formed EVM address" not in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_facilitator_without_a_destination_blocks_the_deploy(tmp_path):
    """A rail advertised with nowhere to send the money."""
    result = _run(tmp_path, env=_FACILITATOR_ONLY)
    assert "is not set" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


def test_x402_being_absent_is_stated_not_silent(tmp_path):
    """The original bug produced no line at all, which read as a pass. Every
    path must say something -- including 'there was nothing to check'."""
    result = _run(tmp_path, env=_NO_X402)
    assert "x402 is not configured" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_nothing_parses_config_out_of_flattened_output():
    """gcloud's `flattened` format pads names with alignment spaces, so any
    `grep 'name: X'` pattern silently matches nothing. That bug shipped twice
    here -- once in the pay-to check (which then reported nothing at all) and
    once in the Stripe secret lookup (which minted a revision on every run).
    Both now read JSON; nothing should go back to grepping the padded form."""
    text = SCRIPT.read_text()
    assert "flattened(" not in text
    assert "grep -A1 'name:" not in text
    assert "grep -A5 'name:" not in text
    assert "--format=json" in text


def test_a_correctly_configured_service_is_not_repaired_again(tmp_path):
    """The churn bug: CURRENT_SECRET came back empty every run, so the script
    concluded the repair was always needed and created a revision each time.
    The service reached revision 62 that way, while the docstring promised
    re-running "does not create pointless revisions"."""
    env = [
        {"name": "STRIPE_SECRET_KEY",
         "valueFrom": {"secretKeyRef": {"name": "SECRET_STRIPE_KEY", "key": "latest"}}},
    ]
    result = _run(tmp_path, env=env)
    assert "already points at SECRET_STRIPE_KEY" in result.stdout
    assert "nothing to repair" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout


def test_a_plain_value_is_still_repointed_at_the_secret(tmp_path):
    """The fix must not make the script stop repairing what genuinely needs it."""
    env = [{"name": "STRIPE_SECRET_KEY", "value": "sk_live_plainvalue"}]
    result = _run(tmp_path, env=env)
    assert "will repoint" in result.stdout
    assert "UPDATE_INVOKED" in result.stdout


def test_min_instances_is_reported_but_does_not_block(tmp_path):
    """Idle billing is money, not brokenness -- worth saying, not worth
    refusing to ship over."""
    result = _run(tmp_path, min_scale="1")
    assert "min-instances=1" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_a_healthy_environment_deploys(tmp_path):
    result = _run(tmp_path)
    assert "preflight passed" in result.stdout
    assert "DEPLOY_INVOKED" in result.stdout


def test_preflight_runs_before_the_deploy(tmp_path):
    """Checking after deploying would leave a broken revision serving."""
    result = _run(tmp_path)
    assert result.stdout.index("Preflight") < result.stdout.index("DEPLOY_INVOKED")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
