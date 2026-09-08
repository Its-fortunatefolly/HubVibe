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

import os
import re
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


def test_refusing_to_overwrite_still_shows_the_wallet_you_have(tmp_path):
    """Refusing is right -- overwriting a key that may hold funds destroys
    them. Refusing silently is not. The wallet you already own is the answer
    to the question just asked, and its address is the next thing needed; a
    STOP without it sends someone hunting for a wallet they already have."""
    key_file = tmp_path / "key"
    key_file.write_text("0x" + "1" * 63 + "2")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--new-wallet"], capture_output=True, text=True,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file)},
    )
    assert result.returncode == 1
    assert "already have a wallet" in result.stdout
    # The address the existing key actually signs for, not a placeholder.
    from eth_account import Account
    assert Account.from_key(key_file.read_text()).address in result.stdout
    assert "USDC on Base" in result.stdout
    assert key_file.read_text() == "0x" + "1" * 63 + "2", "the key was modified"


def test_an_unreadable_key_file_says_so_instead_of_printing_nothing(tmp_path):
    """A truncated or garbage file is not a wallet. Falling through to a blank
    address would be worse than the silent stop it replaced."""
    key_file = tmp_path / "key"
    key_file.write_text("not-a-key")
    result = subprocess.run(
        ["bash", str(SCRIPT), "--new-wallet"], capture_output=True, text=True,
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "HUBVIBE_WALLET_FILE": str(key_file)},
    )
    assert "does not contain a readable private key" in result.stdout
    assert "HUBVIBE_FORCE_NEW_WALLET=1" in result.stdout


# --- the paying wallet must not be the recipient ---------------------------
#
# The owner's Base wallet is the natural thing to reach for and it is also
# X402_PAY_TO_ADDRESS, so this mistake is one paste away. Nothing else catches
# it: verified by running the script against a node whose payTo was the
# payer's own address -- the x402 client produced a signature without
# complaint. The failure would land at the facilitator, on the one call whose
# entire purpose is to prove the facilitator settles a real payment.
#
# These drive the whole shell script with curl stubbed, because the guard is
# in the shell after the embedded preflight, and the preflight-only harness
# above cannot see it.

# Deterministic throwaway keys. Never funded; they exist so the payer address
# is known to the test rather than generated per run.
KEY_A = "0x" + "11" * 32
ADDR_A = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
KEY_B = "0x" + "22" * 32


def _drive(tmp_path, wallet_key, pay_to, extra_env=None):
    """Run the real script with curl stubbed to serve one challenge."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    challenge = tmp_path / "challenge.json"
    challenge.write_text(_challenge(pay_to=pay_to))

    # The script asks curl for the body then the status code on its own line
    # (-w '\n%{http_code}'); the stub answers the same way.
    (bin_dir / "curl").write_text(
        f'#!/usr/bin/env bash\ncat "{challenge}"\nprintf "\\n402"\n'
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["HUBVIBE_WALLET_KEY"] = wallet_key
    env["BASE"] = "https://example.test"
    env.pop("HUBVIBE_ALLOW_SELF_PAYMENT", None)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=180
    )


def test_paying_from_the_recipient_wallet_is_refused(tmp_path):
    result = _drive(tmp_path, wallet_key=KEY_A, pay_to=ADDR_A)
    assert "The paying wallet IS the recipient" in result.stdout
    assert result.returncode == 1
    # It must stop BEFORE spending: no payment attempt, no settlement report.
    assert "Paying for one real call" not in result.stdout


def test_the_recipient_check_is_case_insensitive(tmp_path):
    """EIP-55 checksummed and all-lowercase spellings are the same address.
    Comparing them raw would let the mistake through on a lowercase paste --
    which is exactly the form a wallet app's copy button produces."""
    result = _drive(tmp_path, wallet_key=KEY_A, pay_to=ADDR_A.lower())
    assert "The paying wallet IS the recipient" in result.stdout
    assert result.returncode == 1


def test_a_different_paying_wallet_passes_the_guard(tmp_path):
    """The guard must stop one specific mistake, not become a gate on the
    normal case it exists to protect."""
    result = _drive(tmp_path, wallet_key=KEY_B, pay_to=ADDR_A)
    assert "The paying wallet IS the recipient" not in result.stdout


def test_the_self_payment_refusal_is_overridable(tmp_path):
    """It may well be a valid transfer. Refusing to let the owner try it is
    not this script's call -- refusing to let them do it BY ACCIDENT is."""
    result = _drive(
        tmp_path, wallet_key=KEY_A, pay_to=ADDR_A,
        extra_env={"HUBVIBE_ALLOW_SELF_PAYMENT": "1"},
    )
    assert "self-transfer, allowed by override" in result.stdout
    assert "The paying wallet IS the recipient" not in result.stdout


def test_an_unreadable_balance_hands_over_the_basescan_link(tmp_path):
    """The Base RPC has failed from Cloud Shell on every run so far, and the
    script proceeds without the check -- so a rejection cannot be told apart
    from an empty wallet. When the script cannot answer that question it must
    hand over the page that can, for the exact paying address."""
    from eth_account import Account

    result = _drive(tmp_path, wallet_key=KEY_B, pay_to=ADDR_A,
                    extra_env={"BASE_RPC": "http://127.0.0.1:9"})
    payer = Account.from_key(KEY_B).address
    assert "proceeding without the check" in result.stdout
    assert f"https://basescan.org/address/{payer}" in result.stdout


def test_a_settled_call_prints_the_transaction_link():
    """The receipt is the proof. After a settlement the script must print
    the Basescan link for the transaction the node handed back in
    PAYMENT-RESPONSE, and must say so plainly when the node sent none --
    an empty link would read as a settlement with no transaction."""
    text = SCRIPT.read_text()
    pay_block = text[text.index("Paying for one real call"):]
    assert "booth.last_settlement" in pay_block, "the receipt is never read off the client"
    assert "https://basescan.org/tx/$TX" in pay_block
    assert "sent no PAYMENT-RESPONSE receipt" in pay_block


def test_a_non_json_response_shows_the_status_and_the_body():
    """"not JSON -- is the node up?" was all the owner got on 2026-09-04,
    with the HTTP status and the body discarded. Both go in the message."""
    import os

    result = subprocess.run(
        [sys.executable, "-c", _preflight_source()],
        input="<html><title>503 Service Unavailable</title></html>",
        capture_output=True, text=True,
        env={**os.environ, "STATUS": "503"},
    )
    detail = result.stdout.strip().partition("\t")[2]
    assert "HTTP 503" in detail
    assert "503 Service Unavailable" in detail
    assert "cold-starting" in detail


def test_a_cold_start_5xx_on_the_free_read_is_retried(tmp_path):
    """min-instances is 0: the first request after idle can get Cloud Run's
    own 503 page while the container boots. Reading the challenge costs
    nothing, so the script tries again instead of stopping on it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    challenge = tmp_path / "challenge.json"
    challenge.write_text(_challenge(pay_to=ADDR_A))
    counter = tmp_path / "calls"
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(( $(cat "{counter}" 2>/dev/null || echo 0) + 1 )); echo $n > "{counter}"\n'
        'if [ "$n" -eq 1 ]; then printf "<html>Service Unavailable</html>\\n503"; exit 0; fi\n'
        f'cat "{challenge}"\nprintf "\\n402"\n'
    )
    (bin_dir / "curl").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["HUBVIBE_WALLET_KEY"] = KEY_B
    env["BASE"] = "https://example.test"
    env["HUBVIBE_RETRY_SLEEP"] = "0"
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                            env=env, timeout=180)

    assert "HTTP 503 from the node (attempt 1 of 3" in result.stdout
    assert "x402 advertised" in result.stdout, result.stdout
    assert "not JSON" not in result.stdout


def test_the_paying_block_still_has_no_loop_after_the_read_retry():
    """The retry lives on the free read only. This pins where the loop ends
    relative to where the money starts."""
    text = SCRIPT.read_text()
    read_block = text[text.index('step "Reading the live 402'):text.index('step "Paying for one real call')]
    assert "for attempt in 1 2 3" in read_block


def test_the_client_is_installed_into_a_venv_never_with_bare_pip():
    """A fresh Ubuntu VPS has python3 but no `pip` command and refuses
    system-wide installs (PEP 668). The first real run died at
    `pip: command not found`. The script must build its own environment
    and reach pip through the interpreter."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh").read_text()
    assert "python3 -m venv" in text, "no private environment is created"
    assert 'export PATH="$VENV/bin:$PATH"' in text, "the venv is not put on PATH for the rest of the script"
    assert "python3 -m pip install" in text
    bare = [line for line in text.splitlines() if re.match(r"^\s*pip\s+install", line)]
    assert not bare, "bare `pip install` again: " + "; ".join(bare)
    assert "python3-venv" in text, "no apt fallback for a box whose python3 lacks the venv module"


def test_a_recovery_phrase_pays_from_the_owners_own_wallet(tmp_path):
    """The Base app exports a recovery phrase, not a raw key. The script
    derives the app's first account from it in memory, names the address,
    and refuses a phrase that derives some other wallet."""
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh"
    phrase = tmp_path / ".hubvibe-wallet-phrase"
    phrase.write_text("test test test test test test test test test test test junk\n")
    venv_bin = str(Path(__file__).resolve().parent.parent / ".venv" / "bin")
    env = {"HOME": str(tmp_path), "PATH": f"{venv_bin}:/usr/bin:/bin", "BASE": "http://127.0.0.1:9"}

    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=180, env=env).stdout
    assert "paying from your own wallet" in out
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out, "the first account of the standard test phrase"
    assert "delete the phrase" in out
    assert "New Base wallet created" not in out, "a phrase must never mint a throwaway wallet"
    assert not (tmp_path / ".hubvibe-wallet-key").exists(), "the derived key must not be written to disk"

    # One phrase, many accounts: the wallet the owner funded may not be the
    # app's first. Naming it finds it rather than failing as "wrong phrase".
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=180,
        env={**env, "HUBVIBE_EXPECT_ADDRESS": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"},
    ).stdout
    assert "account 2" in out, out[:400]
    assert "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC" in out

    # A phrase that genuinely does not hold the wallet says so, and lists what
    # it does hold, instead of sending the owner after a second seed.
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=180,
        env={**env, "HUBVIBE_EXPECT_ADDRESS": "0x37555E884c5EbA10f6E816DbecEA30965B9b38C0"},
    ).stdout
    assert "STOP" in out and "first 10 accounts" in out
    assert "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266" in out, "must show what the phrase does derive"
    assert os.environ.get("HUBVIBE_WALLET_KEY") is None


def test_an_empty_wallet_is_not_reported_as_a_broken_rail():
    """The facilitator refusing for want of funds means the signature was
    VALID -- everything up to the money is working. The generic branch told
    the owner their rail was broken and to fix it "before spending any
    effort on demand" (2026-09-07), which is a checker lying about the one
    thing it exists to report."""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "scripts" / "first-paid-call.sh").read_text()
    failure = text.split("the payment did not go through", 1)[1].split("esac", 1)[0]

    assert "insufficient_balance" in failure, "the empty-wallet case is not distinguished"
    assert "insufficient_funds" in failure, "only one of the two facilitator spellings is handled"
    assert "The rail is fine" in failure, "an empty wallet must not read as a broken rail"
    assert "$PAYER" in failure, "must name the address to fund"

    # The alarming wording must survive for the case it was written for: a
    # real rejection (bad signature, wrong network, unreachable facilitator)
    # IS the answer worth having.
    generic = failure.split("*)", 2)[-1]
    assert "answer worth having" in generic
    assert "insufficient" not in generic, "the alarm now fires on an empty wallet again"


def test_the_handoff_names_the_key_file_the_script_actually_uses():
    """The runbook told the owner the payer key was at ~/.hubvibe-payer-key;
    the script has always written ~/.hubvibe-wallet-key. Nobody hit it while
    only scripts read the file -- but the owner asking "who is 0x104f, and
    why should I send it money" (2026-09-08) is answered by deriving the
    address from that key on the box, and a runbook that names a file which
    does not exist turns the one available proof into another dead end.

    Read off both files rather than restated, so they cannot drift again --
    the same guard the facilitator defaults already carry.
    """
    root = Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "first-paid-call.sh").read_text()
    handoff = (root / "docs" / "HANDOFF.md").read_text()

    default = re.search(r'WALLET_FILE="\$\{HUBVIBE_WALLET_FILE:-\$\{HOME:-/tmp\}/([^}"]+)\}"', script)
    assert default, "the script's wallet-file default is no longer where this test looks"
    name = default.group(1)

    assert name in handoff, (
        f"the script writes the payer key to ~/{name}, and the handoff does not say so"
    )
    for wrong in ("hubvibe-payer-key",):
        assert wrong not in handoff, f"the handoff still names ~/{wrong}, which nothing writes"
