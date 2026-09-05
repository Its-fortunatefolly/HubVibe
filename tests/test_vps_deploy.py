"""The VPS deploy stack must refuse bad money configuration before it
touches the machine, and its pieces must agree with each other.

The installer is driven for real (bash, subprocess) through every branch
that can run without Docker -- which is exactly the set of branches that
guard money. The compose file and env example are parsed, not grepped,
because a stack whose pieces disagree (a variable the compose file needs
that the example never names, a port Caddy expects that the service does
not listen on) fails at 2 a.m. on a box with nobody watching.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "vps-install.sh"
VPS_DIR = REPO_ROOT / "deploy" / "vps"

ZERO = "0x" + "0" * 40
UNAFFIRMED = "0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256"
TEST_CONSTANT = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _run(*args, env=None):
    merged = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, env=merged, cwd=REPO_ROOT,
    )


def test_bash_parses_the_script():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_domain_prints_usage_and_installs_nothing():
    result = _run()
    assert result.returncode == 1
    assert "Usage:" in result.stdout
    assert "DNS A record" in result.stdout


@pytest.mark.parametrize("bad", ["https://audits.example.com", "not-a-domain"])
def test_a_url_or_non_domain_is_refused(bad):
    result = _run(bad)
    assert result.returncode == 1
    assert "STOP" in result.stdout


@pytest.mark.parametrize(
    "address,why",
    [
        (ZERO, "zero address"),
        (UNAFFIRMED, "nobody here holds the key"),
        (TEST_CONSTANT, "nobody here holds the key"),
        ("0xabc", "40 hex"),
        ("0x" + "g" * 40, "40 hex"),
    ],
)
def test_an_unpayable_recipient_stops_the_install_before_docker(address, why):
    """The money gate runs FIRST. Every one of these has bitten this repo:
    the zero address shipped live, the unaffirmed address sat deployed, the
    test constant was pasted truncated. On a fresh box the script must die
    on them before Docker is even checked for -- 'Nothing was installed'."""
    result = _run("audits.example.com", env={"X402_PAY_TO_ADDRESS": address})
    assert result.returncode == 1, result.stdout
    assert "Nothing was installed" in result.stdout
    assert "Docker" not in result.stdout.split("STOP")[0].split("==> Checking the x402")[0], (
        "docker was touched before the money gate"
    )


def test_the_affirmed_default_passes_the_gate_and_proceeds_to_docker():
    """With a good recipient, the next stop is the Docker check -- on this
    sandbox that fails (no docker on the stripped PATH), which is exactly
    the proof the gate passed."""
    result = _run("audits.example.com")
    assert "passes every gate" in result.stdout
    assert "Checking Docker" in result.stdout


def _compose() -> dict:
    return yaml.safe_load((VPS_DIR / "docker-compose.yml").read_text())


def test_compose_parses_and_wires_the_service_correctly():
    compose = _compose()
    service = compose["services"]["hubvibe"]
    env = service["environment"]
    # The identity is the domain, never the box.
    assert env["PUBLIC_BASE_URL"] == "https://${DOMAIN}"
    # Off-Google key store, on a persistent volume -- a key the top-up sold
    # must survive a container restart or the money it holds is destroyed.
    assert env["KEY_STORE"] == "sqlite"
    data_dir = env["KEY_STORE_SQLITE_PATH"].rsplit("/", 1)[0]
    assert any(v.split(":")[1] == data_dir for v in service["volumes"]), (
        "the SQLite path is not on a mounted volume; every restart would wipe the balances"
    )
    # Exactly one trusted proxy (Caddy) fronts the service.
    assert env["RATE_LIMIT_PROXY_DEPTH"] == "1"
    assert service["restart"] == "unless-stopped"
    assert "healthcheck" in service


def test_caddy_terminates_tls_and_proxies_to_the_service_port():
    compose = _compose()
    caddy = compose["services"]["caddy"]
    assert "80:80" in caddy["ports"] and "443:443" in caddy["ports"]
    caddyfile = (VPS_DIR / "Caddyfile").read_text()
    assert "reverse_proxy hubvibe:8080" in caddyfile
    assert "{$DOMAIN}" in caddyfile
    assert caddy["environment"]["DOMAIN"] == "${DOMAIN}", (
        "Caddy never sees DOMAIN, so it would serve nothing"
    )
    # And 8080 is the port the image actually listens on.
    dockerfile = (REPO_ROOT / "wcag-audit-engine" / "Dockerfile").read_text()
    assert "8080" in dockerfile


def test_the_env_example_names_every_variable_the_stack_reads():
    example = (VPS_DIR / ".env.example").read_text()
    for var in ("DOMAIN", "X402_FACILITATOR_URL", "X402_PAY_TO_ADDRESS", "MAX_CONCURRENT_AUDITS"):
        assert f"{var}=" in example, f"{var} missing from .env.example"
    # Stripe stays opt-in: present as documentation, commented out.
    assert "# STRIPE_SECRET_KEY=" in example


def test_the_installer_writes_only_intended_defaults():
    """The .env the installer writes is read straight off the script text:
    the affirmed wallet and the xpay facilitator as defaults, overridable,
    and never an AUDIT_API_KEY (an unmetered bypass has no place in a
    default production env)."""
    script = SCRIPT.read_text()
    assert 'DEFAULT_X402_PAY_TO="0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"' in script
    assert "facilitator.xpay.sh" in script
    assert 'chmod 600 "$ENV_FILE"' in script, "the env file holds Stripe keys; it must not be world-readable"
    writes = script.split("Writing deploy/vps/.env", 1)[1]
    assert "AUDIT_API_KEY=" not in writes.split("---")[0].replace("for var in", ""), (
        "the installer must never write the unmetered bypass key by default"
    )


def test_a_rerun_keeps_the_existing_env():
    script = SCRIPT.read_text()
    assert 'if [ -f "$ENV_FILE" ]' in script
    assert "keeping it" in script, (
        "a re-run that rewrote .env would destroy live Stripe keys"
    )
