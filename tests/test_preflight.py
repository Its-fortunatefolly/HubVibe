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
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "repair-and-deploy.sh"

_GOOD_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922bc"   # 40 hex
_SHORT_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922b"    # 39 hex


def _stub(firestore_ok=True, addr=_GOOD_ADDR, min_scale="0"):
    return f"""#!/usr/bin/env bash
case "$*" in
  *"secrets describe"*) exit 0 ;;
  *"firestore databases describe"*) {"echo ok; exit 0" if firestore_ok else "exit 1"} ;;
  *"run services describe"*)
    case "$*" in
      *minScale*) echo "{min_scale}" ;;
      *) printf 'name: STRIPE_SECRET_KEY\\n  secretKeyRef.name: SECRET_STRIPE_KEY\\n'
         printf 'name: X402_PAY_TO_ADDRESS\\n  value: {addr}\\n' ;;
    esac ;;
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
    a crypto rail while every settlement fails -- indistinguishable from
    outside from nobody buying."""
    result = _run(tmp_path, addr=_SHORT_ADDR)
    assert "NOT a valid EVM address" in result.stdout
    assert "39" in result.stdout
    assert "DEPLOY_INVOKED" not in result.stdout


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
