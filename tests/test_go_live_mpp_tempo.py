"""Guards for scripts/go-live-mpp-tempo.sh.

This script mints a real Stripe crypto deposit address and points a live
service's payment rail at it, so what matters is what it refuses: a bad
address must never reach a revision, and a failure must never leave the
service half-changed.

The 39-hex case is not hypothetical. Exactly that value -- a truncated paste
of the test-suite constant -- sat on this service as MPP_TEMPO_RECIPIENT_ADDRESS
advertising an unsettleable rail, and only the protocol's own reference client
(`mppx validate`) ever noticed.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go-live-mpp-tempo.sh"

GOOD = "0x32b08c5e927c69877d0fcab35618c265674922bc"
SHORT = "0x32b08c5e927c69877d0fcab35618c265674922b"
ZERO = "0x" + "0" * 40


def _run(tmp_path, curl_body=None, secret="sk_live_fake", env=None):
    """Drive the script with gcloud and curl stubbed out."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    (bin_dir / "gcloud").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"secrets versions access"*) printf "%s" "{secret}" ;;\n'
        '  *"run services update"*) echo "UPDATE_INVOKED $*" ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    (bin_dir / "gcloud").chmod(0o755)

    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\nprintf '%s' \"$STUB_CURL_BODY\"\n"
    )
    (bin_dir / "curl").chmod(0o755)

    # repair-and-deploy.sh is exec'd at the end; stub it so the test stops at
    # the handoff rather than trying to deploy.
    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True, exist_ok=True)
    (fake_repo / "scripts" / "go-live-mpp-tempo.sh").write_text(SCRIPT.read_text())
    (fake_repo / "scripts" / "repair-and-deploy.sh").write_text(
        "#!/usr/bin/env bash\necho DEPLOY_HANDOFF\n"
    )

    full_env = dict(os.environ)
    full_env["PATH"] = f"{bin_dir}:{full_env['PATH']}"
    full_env["STUB_CURL_BODY"] = curl_body or ""
    full_env.pop("MPP_TEMPO_RECIPIENT_ADDRESS", None)
    if env:
        full_env.update(env)

    return subprocess.run(
        ["bash", str(fake_repo / "scripts" / "go-live-mpp-tempo.sh")],
        capture_output=True, text=True, env=full_env, timeout=60,
    )


def test_the_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_minted_address_is_set_and_then_deployed(tmp_path):
    result = _run(tmp_path, curl_body='{"address": "%s"}' % GOOD)
    assert "well-formed" in result.stdout
    assert f"MPP_TEMPO_RECIPIENT_ADDRESS={GOOD}" in result.stdout
    # Config alone is not a deploy -- it must hand off to the source deploy.
    assert "DEPLOY_HANDOFF" in result.stdout


def test_a_nested_address_shape_is_also_read(tmp_path):
    """A preview API's response shape is not a promise. Reading only the flat
    key would turn a successful mint into 'could not read an address'."""
    result = _run(tmp_path, curl_body='{"tempo": {"address": "%s"}}' % GOOD)
    assert f"MPP_TEMPO_RECIPIENT_ADDRESS={GOOD}" in result.stdout


def test_a_short_address_is_never_set(tmp_path):
    """39 hex characters. The exact value that sat live on this service."""
    result = _run(tmp_path, curl_body='{"address": "%s"}' % SHORT)
    assert "39" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout
    assert "DEPLOY_HANDOFF" not in result.stdout


def test_the_zero_address_is_never_set(tmp_path):
    """Shape-valid and unownable -- it passes the hex check that catches the
    short address, and nothing can ever receive at it."""
    result = _run(tmp_path, curl_body='{"address": "%s"}' % ZERO)
    assert "zero address" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout


def test_a_stripe_error_stops_before_changing_anything(tmp_path):
    result = _run(
        tmp_path,
        curl_body='{"error": {"message": "Unrecognized request URL", "type": "invalid_request_error"}}',
    )
    assert "Stripe refused" in result.stdout
    assert "Unrecognized request URL" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout
    assert "Nothing was changed" in result.stdout


def test_an_unreadable_secret_stops_before_calling_stripe(tmp_path):
    result = _run(tmp_path, secret="")
    assert "could not read secret" in result.stdout
    assert "UPDATE_INVOKED" not in result.stdout


def test_an_address_supplied_by_hand_skips_minting(tmp_path):
    """So a deposit address that already exists does not require minting a
    second one -- addresses accumulate on the account otherwise."""
    result = _run(
        tmp_path,
        curl_body='{"error": {"message": "should not be called"}}',
        env={"MPP_TEMPO_RECIPIENT_ADDRESS": GOOD},
    )
    assert "from the environment" in result.stdout
    assert f"MPP_TEMPO_RECIPIENT_ADDRESS={GOOD}" in result.stdout
    assert "DEPLOY_HANDOFF" in result.stdout


def test_the_secret_is_read_not_typed():
    """A live Stripe key pasted into a phone terminal lands in shell history
    and stays there. It is read from Secret Manager instead."""
    text = SCRIPT.read_text()
    assert "secrets versions access" in text
    assert "sk_live" not in text


def test_the_api_version_is_pinned_and_overridable():
    """The deposit-address endpoint is preview-only. An older version 404s it,
    which reads as 'crypto is not enabled' rather than 'wrong version' -- a
    wrong diagnosis that has cost this project days before."""
    text = SCRIPT.read_text()
    assert "STRIPE_API_VERSION" in text
    assert "2026-07-29.preview" in text
    assert 'Stripe-Version: $STRIPE_API_VERSION' in text
