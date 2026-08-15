"""Guards for scripts/lib-api-key.sh.

The API key broke six ways in one evening and every one had the same root
cause: it had to be hand-exported into the shell. Placeholders got pasted
literally, the backing secret's name had to be looked up by hand (and guessing
it wrote a test string into the Stripe key secret), and a fresh Cloud Shell
drops the export -- so verify-live.sh printed SKIP on every run.

The consequence mattered more than the friction. The paid path -- the only
check that answers "can this service take money" -- was skipped by default,
permanently. Everything else could be green while the thing that earns was
dead, which is exactly what happened.

Resolution is automatic now. These tests cover each way it can go, because a
resolver that silently returns the wrong thing is worse than the export it
replaced.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "scripts" / "lib-api-key.sh"

SECRET_REF = {
    "name": "AUDIT_API_KEY",
    "valueFrom": {"secretKeyRef": {"name": "sudul-api-key", "key": "latest"}},
}


def _gcloud_stub(env, secret_value):
    body = json.dumps(
        {"spec": {"template": {"spec": {"containers": [{"env": env}]}}}}
    )
    return f"""#!/usr/bin/env bash
case "$*" in
  *"--format=json"*) cat <<'J'
{body}
J
    ;;
  *"versions access"*) printf '{secret_value}' ;;
  *) exit 1 ;;
esac
"""


def _resolve(tmp_path, env=None, secret_value="the-real-key", exported=None):
    """Source the lib with a stubbed gcloud and report what it resolved."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gcloud").write_text(
        _gcloud_stub(env if env is not None else [SECRET_REF], secret_value)
    )
    (bin_dir / "gcloud").chmod(0o755)

    # Report length and provenance, never the key itself.
    script = f"""
. {LIB}
if hv_resolve_api_key; then
  printf 'OK|%s|%s\\n' "${{#HV_API_KEY}}" "$HV_KEY_SOURCE"
else
  printf 'FAIL|%s\\n' "$(printf '%s' "$HV_KEY_PROBLEM" | head -1)"
fi
"""
    shell_env = dict(os.environ)
    shell_env["PATH"] = f"{bin_dir}:{shell_env['PATH']}"
    shell_env.pop("HUBVIBE_API_KEY", None)
    if exported is not None:
        shell_env["HUBVIBE_API_KEY"] = exported

    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env=shell_env, timeout=60,
    )
    return result.stdout.strip()


def test_the_lib_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_it_resolves_the_key_from_secret_manager(tmp_path):
    """The whole point: no export, no secret name to look up, no human."""
    out = _resolve(tmp_path)
    assert out.startswith("OK|12|"), out
    assert "sudul-api-key" in out


def test_an_explicit_export_still_wins(tmp_path):
    """Someone testing a specific customer key must not be silently
    overridden by the internal one."""
    out = _resolve(tmp_path, exported="a-customer-key")
    assert out.startswith("OK|14|"), out
    assert "from the environment" in out


def test_a_literal_value_on_the_service_is_used(tmp_path):
    out = _resolve(tmp_path, env=[{"name": "AUDIT_API_KEY", "value": "plain-literal"}])
    assert out.startswith("OK|13|"), out
    assert "literal value" in out


def test_a_trailing_newline_is_refused_with_the_reason(tmp_path):
    """Unfixable client-side: the container keeps the newline, an HTTP header
    cannot carry one, so no request can ever match. Handing back a key that
    will always 402 would send the caller hunting the wrong problem."""
    out = _resolve(tmp_path, secret_value="key-with-newline\\n")
    assert out.startswith("FAIL|"), out
    assert "newline" in out


def test_a_clean_key_is_not_mistaken_for_one_with_a_newline(tmp_path):
    """The first newline check was `case $raw in *"$(printf '\\n')")`, but
    command substitution strips trailing newlines -- so the pattern was *""
    and matched every key. It reported a perfectly good key as broken."""
    out = _resolve(tmp_path, secret_value="perfectly-fine-key")
    assert out.startswith("OK|"), out
    assert "newline" not in out


def test_no_audit_api_key_on_the_service_is_reported_clearly(tmp_path):
    out = _resolve(tmp_path, env=[{"name": "OTHER", "value": "x"}])
    assert out.startswith("FAIL|"), out
    assert "no AUDIT_API_KEY" in out


def test_the_key_value_is_never_printed(tmp_path):
    """This output lands in terminals, screenshots and chat logs."""
    out = _resolve(tmp_path, secret_value="super-secret-value")
    assert "super-secret-value" not in out


def test_provenance_survives_the_call(tmp_path):
    """It sets a variable rather than echoing on purpose. An echoing version
    forces callers into `$(...)`, which runs in a subshell -- so HV_KEY_SOURCE
    and HV_KEY_PROBLEM were set in a process that exits immediately and every
    caller saw them empty. The diagnosis existed and was invisible."""
    text = LIB.read_text()
    assert "HV_API_KEY=" in text
    assert "printf '%s' \"$raw\"" not in text


def test_callers_do_not_use_command_substitution():
    """Same trap, one level up."""
    for name in ("verify-live.sh", "measure-call-cost.sh"):
        text = (REPO_ROOT / "scripts" / name).read_text()
        assert "$(hv_resolve_api_key)" not in text, name
        assert "hv_resolve_api_key &&" in text, name


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
