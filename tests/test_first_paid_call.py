"""Guards for scripts/first-paid-call.sh's preflight.

This script is the only thing in the repo that spends real money on purpose,
and it spends it once. The whole point of that payment is to break the
discovery deadlock -- the Bazaar spec catalogs a resource only when a payment
payload carrying the discovery extension reaches a facilitator, so an unpaid
resource is an uncatalogued resource by construction, on every facilitator.

Which means the preflight is not a nicety. If the deployed revision emits a
Bazaar record the facilitator will reject on validation, the payment settles
and buys no index entry, and the one shot at bootstrapping discovery is gone.
The gate below is what makes the money conditional on the call being able to
do its job.

The preflight is extracted and driven against synthetic challenges here, so
every rejection path is exercised without a network or a wallet.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "first-paid-call.sh"


def _preflight_source() -> str:
    """The embedded python preflight, on its own."""
    text = SCRIPT.read_text()
    start = text.index("import json, sys", text.index("PREFLIGHT="))
    end = text.index("')", start)
    return text[start:end]


def _run(challenge: str):
    """Feed one challenge body through the preflight; return (verdict, detail)."""
    result = subprocess.run(
        [sys.executable, "-c", _preflight_source()],
        input=challenge,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    verdict, _, detail = out.partition("\t")
    return verdict, detail


GOOD_ADDR = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _challenge(*, pay_to=GOOD_ADDR, scheme="exact", bazaar_input=...,
               x402_version=1, drop_field=None):
    import json

    if bazaar_input is ...:
        bazaar_input = {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": {"url": "https://example.com"},
        }
    entry = {
        "scheme": scheme,
        "network": "base",
        "maxAmountRequired": "30000",
        "resource": "https://example.test/audit/wcag",
        "payTo": pay_to,
        "maxTimeoutSeconds": 300,
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    }
    if drop_field:
        entry.pop(drop_field)
    body = {"price": "$0.03", "x402Version": x402_version, "accepts": [entry]}
    if bazaar_input is not None:
        body["extensions"] = {"bazaar": {"info": {"input": bazaar_input}}}
    return json.dumps(body)


def test_a_well_formed_challenge_clears_the_gate():
    verdict, detail = _run(_challenge())
    assert verdict == "OK", detail
    assert GOOD_ADDR in detail


def test_no_x402_rail_means_nothing_is_paid():
    """There is no rail to pay on. Attempting anyway would burn a call for a
    402 that repeats itself."""
    verdict, detail = _run(_challenge(scheme="something-else"))
    assert verdict == "FAIL"
    assert "no payable x402 entry" in detail


def test_a_pre_61_challenge_is_refused_by_name():
    """The shape this script was originally written against. accepts[] with no
    x402Version means no v1 client reads it -- and the preflight itself read
    the pre-#61 field names for a while, so it reported "does not advertise
    x402" about a node that did. Name the real cause instead."""
    verdict, detail = _run(_challenge(x402_version=None))
    assert verdict == "FAIL"
    assert "x402Version:1" in detail
    assert "repair-and-deploy" in detail


def test_an_accepts_entry_missing_spec_fields_is_refused():
    """A client validates every entry and raises before signing. Paying into
    that spends nothing and proves nothing."""
    verdict, detail = _run(_challenge(drop_field="asset"))
    assert verdict == "FAIL"
    assert "asset" in detail
    assert "before" in detail


def test_the_zero_address_is_refused_before_any_signature():
    """0x + 40 zeros is shape-valid and unownable -- USDC reverts transfers to
    it. This exact address was deployed for days in 2026-08 while every shape
    check passed. Paying it would destroy the money and prove nothing."""
    verdict, detail = _run(_challenge(pay_to="0x" + "0" * 40))
    assert verdict == "FAIL"
    assert "zero address" in detail


def test_a_challenge_with_no_bazaar_record_is_refused():
    """Settling here would work and index nothing, which spends the one
    bootstrap payment for half its purpose."""
    verdict, detail = _run(_challenge(bazaar_input=None))
    assert verdict == "FAIL"
    assert "index nothing" in detail


def test_a_bazaar_record_missing_its_method_is_refused():
    """The #52 bug, seen from the paying side. A record without `method` fails
    the facilitator's own validator, so it is discarded before cataloguing --
    the payment settles and buys no index entry. If the live 402 still looks
    like this, the deployed revision predates the fix and the fix has to ship
    before the money does."""
    verdict, detail = _run(
        _challenge(
            bazaar_input={
                "type": "http",
                "bodyType": "json",
                "body": {"url": "https://example.com"},
            }
        )
    )
    assert verdict == "FAIL"
    assert "names no HTTP method" in detail
    assert "repair-and-deploy" in detail


def test_an_mcp_record_needs_no_method():
    """Only body records carry a method. Requiring one of an mcp-type record
    would refuse a perfectly catalogable call."""
    verdict, _ = _run(
        _challenge(bazaar_input={"type": "mcp", "toolName": "audit_wcag"})
    )
    assert verdict == "OK"


def test_a_non_json_response_is_refused_rather_than_parsed_as_empty():
    """A node that is down returns an HTML error page. Treating that as an
    empty challenge would fall through to 'no x402 advertised', which reads as
    a config problem and sends the next person to the wrong place."""
    verdict, detail = _run("<html>502 Bad Gateway</html>")
    assert verdict == "FAIL"
    assert "not JSON" in detail


def test_the_script_never_retries_a_payment():
    """A retried payment is a double charge. The script must make exactly one
    signed attempt -- no loop, no retry helper, anywhere in the paying block."""
    import re

    text = SCRIPT.read_text()
    paying = text[text.index("step \"Paying for one real call"):text.index("step \"Re-reading")]
    # Loop keywords as statements, not the word "for" inside prose.
    loops = [
        line for line in paying.splitlines()
        if re.match(r"\s*(for|while|until)\b", line) or re.search(r"\bdone\b", line)
    ]
    assert not loops, (
        f"the paying block loops: {loops} -- a retry around a signature is a "
        "double charge"
    )


def test_no_wallet_anywhere_stops_before_the_node_is_touched(tmp_path):
    """Fail closed at the top rather than partway through, and say the two
    things that actually unstick someone: the export may simply have been lost
    (Cloud Shell drops env on reconnect, which is exactly how this presented),
    and a wallet can be made right here."""
    env = {"PATH": "/usr/bin:/bin", "HUBVIBE_WALLET_FILE": str(tmp_path / "nope")}
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO_ROOT
    )
    assert result.returncode == 1
    assert "No paying wallet" in result.stdout
    assert "Cloud Shell drops env on" in result.stdout
    assert "--new-wallet" in result.stdout


def test_an_unset_HOME_does_not_crash_before_the_wallet_message(tmp_path):
    """set -u makes a bare $HOME fatal where HOME is not set. Dying on an
    unbound variable before the wallet message prints is the least useful
    failure this script could have."""
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"}, cwd=REPO_ROOT,
    )
    assert "unbound variable" not in result.stderr
    assert "No paying wallet" in result.stdout


def test_the_wallet_file_is_used_when_the_env_var_is_empty(tmp_path):
    """The whole point of the file: an export that did not survive a reconnect
    must not look like having no wallet at all."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file), "BASE": "http://127.0.0.1:9"},
    )
    assert "wallet key from" in result.stdout
    assert "No paying wallet" not in result.stdout
