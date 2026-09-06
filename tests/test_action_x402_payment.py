"""Guards for the GitHub Action's x402 payment path (scripts/x402_pay.py).

This is the code that spends money inside somebody else's CI pipeline, on a
schedule they did not choose, so the guards here are about restraint rather
than features: the per-call cap must reach the signer, a bad key must not be
echoed into a CI log, and a failure must report an unpaid call rather than a
broken action.

It exists because the action's only payment path was an API key, and a key
costs a browser checkout. A CI step whose first run fails with "HTTP 402 -- go
buy a plan" is deleted on the next push, which closed the adoption funnel of
the highest-volume distribution channel this service has.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "x402_pay.py"
ACTION = REPO_ROOT / "action.yml"

# A syntactically valid secp256k1 key. Never funded, never used: these tests
# stub the network, so no signature ever leaves the process.
_FAKE_KEY = "0x" + "11" * 32


def _run(tmp_path, stub, **env_overrides):
    """Run the payer with x402 stubbed out, in a scratch cwd."""
    (tmp_path / "x402_stub.py").write_text(stub)
    env = dict(os.environ)
    env.update(
        {
            "BASE_URL": "https://node.example",
            "AUDIT_ENDPOINT": "bundle",
            "TARGET_URL": "https://example.com",
            "TIMEOUT_SECONDS": "90",
            "HUBVIBE_WALLET_KEY": _FAKE_KEY,
            "MAX_PRICE_USD": "0.15",
            "PYTHONPATH": str(tmp_path),
        }
    )
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )


# A stub that stands in for the whole x402 client surface and records the
# spend policy it was handed, so the cap can be asserted where it matters --
# on the signer, before a signature exists.
_STUB = '''
import json, sys, types

recorded = {}

def max_amount(atomic):
    recorded["cap_atomic"] = atomic
    return ("max_amount", atomic)

class x402ClientSync:
    pass

def register_exact_evm_client(client, signer, policies=None):
    recorded["policies"] = policies
    with open("recorded.json", "w") as fh:
        json.dump(recorded, fh)

class _Response:
    status_code = %(status)s
    text = %(text)r

class x402HTTPClientSync:
    def __init__(self, client):
        pass
    def post(self, url, json=None, timeout=None):
        recorded["url"] = url
        with open("recorded.json", "w") as fh:
            import json as _j
            _j.dump(recorded, fh)
        %(post_body)s
        return _Response()

class EthAccountSigner:
    def __init__(self, account):
        pass

x402 = types.ModuleType("x402")
x402.max_amount = max_amount
x402.x402ClientSync = x402ClientSync
sys.modules["x402"] = x402

http_mod = types.ModuleType("x402.http")
http_mod.x402HTTPClientSync = x402HTTPClientSync
sys.modules["x402.http"] = http_mod

evm = types.ModuleType("x402.mechanisms.evm")
evm.EthAccountSigner = EthAccountSigner
sys.modules["x402.mechanisms"] = types.ModuleType("x402.mechanisms")
sys.modules["x402.mechanisms.evm"] = evm

exact = types.ModuleType("x402.mechanisms.evm.exact")
exact.register_exact_evm_client = register_exact_evm_client
sys.modules["x402.mechanisms.evm.exact"] = exact
'''


def _stub(status=200, text='{"pass": true}', post_body="pass"):
    body = _STUB % {"status": status, "text": text, "post_body": post_body}
    # Imported for its side effects before the payer runs.
    return body + "\n"


@pytest.fixture(autouse=True)
def _install_stub(monkeypatch):
    """The payer imports x402 at module scope inside main(); the stub has to be
    on sys.path AND imported first, which a sitecustomize does for free."""
    yield


def _with_sitecustomize(tmp_path, stub):
    (tmp_path / "sitecustomize.py").write_text(stub)
    return tmp_path


def test_a_successful_payment_reports_the_status_and_writes_the_body(tmp_path):
    _with_sitecustomize(tmp_path, _stub(status=200, text='{"pass": true}'))
    result = _run(tmp_path, _stub())
    assert result.stdout.strip().endswith("200"), result.stderr
    assert json.loads((tmp_path / "response.json").read_text())["pass"] is True


def test_the_per_call_cap_reaches_the_signer_as_a_spend_policy(tmp_path):
    """The cap has to bind BEFORE a signature exists. Checking the price after
    the fact is not a cap, it is a receipt."""
    _with_sitecustomize(tmp_path, _stub())
    result = _run(tmp_path, _stub(), MAX_PRICE_USD="0.15")
    assert result.returncode == 0, result.stderr
    recorded = json.loads((tmp_path / "recorded.json").read_text())
    # USDC is 6 decimals: $0.15 -> 150000 atomic units.
    assert recorded["cap_atomic"] == 150000
    assert recorded["policies"], "no spend policy was registered on the signer"


def test_a_different_cap_is_converted_not_hardcoded(tmp_path):
    _with_sitecustomize(tmp_path, _stub())
    _run(tmp_path, _stub(), MAX_PRICE_USD="0.03")
    recorded = json.loads((tmp_path / "recorded.json").read_text())
    assert recorded["cap_atomic"] == 30000


def test_a_zero_or_negative_cap_refuses_to_pay(tmp_path):
    """An unbounded cap in someone else's CI is the one setting that can empty
    a wallet, so it fails closed rather than defaulting."""
    _with_sitecustomize(tmp_path, _stub())
    result = _run(tmp_path, _stub(), MAX_PRICE_USD="0")
    assert result.stdout.strip() == "402"
    assert "greater than zero" in result.stderr


def test_a_bad_wallet_key_never_appears_in_the_log(tmp_path):
    """CI logs are retained and often public. A diagnostic must not make a
    private key -- or its length -- recoverable."""
    secret = "0xdeadbeef"
    _with_sitecustomize(tmp_path, _stub())
    result = _run(tmp_path, _stub(), HUBVIBE_WALLET_KEY=secret)
    assert result.stdout.strip() == "402"
    assert "not a valid EVM private key" in result.stderr
    assert secret not in result.stderr
    assert secret not in result.stdout
    assert "deadbeef" not in (tmp_path / "response.json").read_text()


def test_a_payment_failure_reports_an_unpaid_call_not_a_crash(tmp_path):
    """A traceback would read as a broken action; it is an unpaid call, and the
    action already has a branch that says so usefully."""
    _with_sitecustomize(tmp_path, _stub(post_body="raise RuntimeError('facilitator said no')"))
    result = _run(tmp_path, _stub())
    assert result.stdout.strip() == "402"
    assert "x402 payment failed" in result.stderr
    assert "Traceback" not in result.stderr


def test_the_action_passes_the_wallet_and_cap_through_to_the_script():
    """An input the run block never receives is an input that silently does
    nothing -- and this one is the difference between paying and not."""
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(ACTION.read_text())

    assert "wallet-key" in spec["inputs"]
    assert "max-price-usd" in spec["inputs"]
    assert spec["inputs"]["max-price-usd"]["default"] == "0.15"

    step = spec["runs"]["steps"][-1]
    assert step["env"]["HUBVIBE_WALLET_KEY"] == "${{ inputs.wallet-key }}"
    assert step["env"]["MAX_PRICE_USD"] == "${{ inputs.max-price-usd }}"
    assert "x402_pay.py" in step["run"]


def test_the_published_action_repo_ships_the_payer():
    """action.yml calls it by $GITHUB_ACTION_PATH. A published copy without it
    fails with 'No such file or directory' on the one path that spends money."""
    generator = (REPO_ROOT / "scripts" / "publish-action-repo.sh").read_text()
    assert "x402_pay.py" in generator


def test_the_api_key_path_still_wins_when_both_are_set():
    """A key is prepaid; a wallet spends per call. Preferring the wallet would
    charge someone who has already paid."""
    yaml = pytest.importorskip("yaml")
    run = yaml.safe_load(ACTION.read_text())["runs"]["steps"][-1]["run"]
    assert '[ -z "$HUBVIBE_API_KEY" ] && [ -n "${HUBVIBE_WALLET_KEY:-}" ]' in run
