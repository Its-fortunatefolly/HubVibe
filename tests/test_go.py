"""scripts/go.sh -- the one command the owner runs on the box.

The first paid call needs exactly two things a script cannot do: money moved
out of the owner's own wallet, and their hands on their own accounts.
Everything else was still being handed to them as homework -- make a wallet,
read an address, go fund it, come back to a terminal that has since dropped,
run a second command. This script is the machine doing the machine's half
unattended, so the owner's part is one transfer whenever they get to it.

These tests drive it with a fake chain, because the sandbox cannot reach Base
and a test that needs the real chain is a test that silently skips.
"""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "go.sh"

# A key whose address is stable, so a test can assert the address is printed.
KEY = "0x" + "1" * 63 + "2"


def _address():
    from eth_account import Account

    return Account.from_key(KEY).address


def _fake_rpc(tmp_path, balances):
    """A file:// 'RPC' is not possible -- urlopen POSTs. So run a real one.

    balances: list of USDC amounts served in order, last value repeating. The
    loop must see a balance CHANGE without restarting, which a single static
    response cannot prove.
    """
    import http.server
    import json
    import threading

    remaining = list(balances)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            value = remaining[0] if len(remaining) == 1 else remaining.pop(0)
            raw = hex(int(round(value * 1_000_000)))[2:].rjust(64, "0")
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x" + raw}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def _env(tmp_path, rpc, **extra):
    key_file = tmp_path / "key"
    key_file.write_text(KEY)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "HUBVIBE_WALLET_FILE": str(key_file),
        "BASE_RPC": rpc,
        "POLL_SECONDS": "1",
        "WAIT_SECONDS": "3",
        "BASE": "http://127.0.0.1:9",
    }
    env.update(extra)
    return env


def _stub_first_paid_call(tmp_path):
    """Stand in for the payment itself, so these tests never spend anything
    and never need a node. It records that it ran."""
    stub_dir = tmp_path / "stub" / "scripts"
    stub_dir.mkdir(parents=True)
    (stub_dir / "go.sh").write_bytes(SCRIPT.read_bytes())
    marker = tmp_path / "paid"
    (stub_dir / "first-paid-call.sh").write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        printf 'PAID CALL RAN\\n'
        printf 'ran' > {marker}
        """))
    return stub_dir / "go.sh", marker


def test_it_pays_immediately_when_the_wallet_is_already_funded(tmp_path):
    """No waiting when there is nothing to wait for."""
    server, rpc = _fake_rpc(tmp_path, [1.0])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert "PAID CALL RAN" in result.stdout, result.stdout
    assert marker.exists()
    assert "Waiting for the money" not in result.stdout


def test_it_waits_then_pays_when_the_money_lands(tmp_path):
    """The whole point: the owner sends the money whenever, and the box
    notices and makes the call without them present."""
    server, rpc = _fake_rpc(tmp_path, [0.0, 0.0, 0.25])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc, WAIT_SECONDS="30"), timeout=90,
        )
    finally:
        server.shutdown()
    assert "Waiting for the money" in result.stdout, result.stdout
    assert "PAID CALL RAN" in result.stdout, result.stdout
    assert marker.exists()
    # The address to fund must be on screen -- an unfunded run that does not
    # say where to send money is the homework this script exists to remove.
    assert _address() in result.stdout


def test_an_unreadable_chain_never_reads_as_funded(tmp_path):
    """An RPC that is down must not look like a zero balance forever, and must
    never look like enough to pay. Guessing either way spends or stalls."""
    script, marker = _stub_first_paid_call(tmp_path)
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        # A port nothing listens on: every RPC read fails.
        env=_env(tmp_path, "http://127.0.0.1:9"), timeout=90,
    )
    assert not marker.exists(), "it paid without ever reading a balance"
    assert "unreadable" in result.stdout
    assert result.returncode != 0
    assert "Nothing was spent" in result.stdout


def test_it_gives_up_without_spending_when_no_money_arrives(tmp_path):
    """A timeout is not a failure to report darkly -- say nothing was spent
    and how to resume."""
    server, rpc = _fake_rpc(tmp_path, [0.0])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert not marker.exists()
    assert result.returncode != 0
    assert "Nothing was spent" in result.stdout
    assert _address() in result.stdout, "must name the address to fund on the way out"


def test_a_balance_under_the_price_does_not_trigger_a_call(tmp_path):
    """$0.02 cannot pay for a $0.03 call. Trying anyway burns the run and
    hands back a facilitator refusal instead of an answer."""
    server, rpc = _fake_rpc(tmp_path, [0.02])
    try:
        script, marker = _stub_first_paid_call(tmp_path)
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env=_env(tmp_path, rpc), timeout=90,
        )
    finally:
        server.shutdown()
    assert not marker.exists(), "it tried to pay $0.03 out of $0.02"
    assert result.returncode != 0


@pytest.mark.parametrize("marker_env", [
    {"CLOUD_SHELL": "true"},
    {"DEVSHELL_PROJECT_ID": "resolver-time"},
])
def test_google_cloud_shell_is_refused_before_a_wallet_can_be_made(tmp_path, marker_env):
    """Worse here than anywhere: a wallet made in a temporary terminal
    disappears with the session, and this script would print its address and
    invite the owner to send real money to it."""
    env = _env(tmp_path, "http://127.0.0.1:9")
    env.pop("HUBVIBE_WALLET_FILE")  # no wallet: it would otherwise make one
    env.update(marker_env)
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env,
        cwd=REPO_ROOT, timeout=60,
    )
    assert result.returncode != 0
    assert "Cloud Shell" in result.stdout
    assert "Browser terminal" in result.stdout or "ssh root@" in result.stdout
    assert not (tmp_path / ".hubvibe-wallet-key").exists(), "it made a wallet anyway"


def test_it_never_generates_a_second_wallet_over_an_existing_one(tmp_path):
    """The existing key may hold funds. Overwriting it destroys them."""
    server, rpc = _fake_rpc(tmp_path, [1.0])
    try:
        script, _ = _stub_first_paid_call(tmp_path)
        env = _env(tmp_path, rpc)
        subprocess.run(["bash", str(script)], capture_output=True, text=True,
                       env=env, timeout=90)
    finally:
        server.shutdown()
    assert (tmp_path / "key").read_text() == KEY, "the wallet key was modified"
