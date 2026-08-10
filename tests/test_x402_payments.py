"""x402 payment path tests.

The bug these exist to prevent: x402ResourceServer.initialize() is a plain
synchronous method, but this module used to `await` it. `await None` raises
TypeError, and verify_and_settle catches every exception and fails closed --
so a fully configured x402 deployment rejected 100% of payments with no
diagnostic anywhere. Fail-closed is the right default, but it means a wiring
mistake is indistinguishable from a genuinely invalid payment unless
something actually exercises the accept path.

Every test here mocks the facilitator: verification and settlement are the
facilitator's job, and the point is to prove this module drives it correctly,
not to re-test the x402 library or reach the network.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
X402_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py"


def _load_x402(monkeypatch, *, facilitator="https://facilitator.example", pay_to="0xabc"):
    if facilitator is None:
        monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)
    else:
        monkeypatch.setenv("X402_FACILITATOR_URL", facilitator)
    if pay_to is None:
        monkeypatch.delenv("X402_PAY_TO_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("X402_PAY_TO_ADDRESS", pay_to)

    name = "hubvibe_x402_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, X402_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_fake_server(monkeypatch, module, *, valid=True, settled=True):
    """Stand in for x402ResourceServer with the REAL library's shape.

    initialize() is deliberately a plain MagicMock (sync, returns a
    non-awaitable) because that is exactly what the real method is -- an
    AsyncMock here would hide the very bug this file exists to catch.
    """
    server = MagicMock()
    server.initialize = MagicMock(return_value=None)
    server.register = MagicMock()
    server.build_payment_requirements = MagicMock(return_value=[MagicMock()])

    verify_result = MagicMock()
    verify_result.is_valid = valid
    settle_result = MagicMock()
    settle_result.success = settled

    async def _verify(*a, **k):
        return verify_result

    async def _settle(*a, **k):
        return settle_result

    server.verify_payment = _verify
    server.settle_payment = _settle

    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=server))
    monkeypatch.setattr(module, "HTTPFacilitatorClient", MagicMock())
    monkeypatch.setattr(module, "FacilitatorConfig", MagicMock())
    monkeypatch.setattr(module, "ExactEvmServerScheme", MagicMock())
    monkeypatch.setattr(module, "ResourceConfig", MagicMock())
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: MagicMock())
    return server


def test_valid_payment_is_accepted(monkeypatch):
    """The regression. Before the fix this returned False -- always."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is True


def test_initialize_is_called_synchronously_not_awaited(monkeypatch):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    module.verify_and_settle_sync("signed-payment", price="$0.03")

    server.initialize.assert_called_once()


def test_facilitator_rejection_fails_closed(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, valid=False)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is False


def test_failed_settlement_fails_closed(monkeypatch):
    """Verified but not settled means we were not actually paid."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, valid=True, settled=False)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is False


def test_unconfigured_deployment_never_accepts(monkeypatch):
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.is_configured() is False
    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is False


def test_requirements_are_cached_per_price_not_shared(monkeypatch):
    """A $0.03 payment must never satisfy a $0.10 bundle challenge."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    module.verify_and_settle_sync("p", price="$0.03")
    module.verify_and_settle_sync("p", price="$0.10")
    module.verify_and_settle_sync("p", price="$0.03")

    prices = {c.kwargs["price"] for c in module.ResourceConfig.call_args_list}
    assert prices == {"$0.03", "$0.10"}
    # Two distinct prices -> two builds; the repeat $0.03 is served from cache.
    assert server.build_payment_requirements.call_count == 2


def test_server_is_not_cached_when_initialize_fails(monkeypatch):
    """A briefly unreachable facilitator must not poison the server for the
    life of the process -- the next request should retry."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("facilitator unreachable"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    assert module.verify_and_settle_sync("p", price="$0.03") is False
    assert module._server is None, "a server that failed to initialize was cached"


def test_payment_required_body_is_empty_when_unconfigured(monkeypatch):
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.payment_required_body(price="$0.03") == {}
    assert module.accepts_entry(price="$0.03") is None


def test_payment_required_body_advertises_real_address_when_configured(monkeypatch):
    module = _load_x402(monkeypatch, pay_to="0xdeadbeef")
    body = module.payment_required_body(price="$0.03")

    assert body["payTo"] == "0xdeadbeef"
    assert body["accepted_payment_header"] == "X-PAYMENT"
    assert body["price"] == "$0.03"

    entry = module.accepts_entry(price="$0.03")
    assert entry["protocol"] == "x402"
    assert entry["pay_to"] == "0xdeadbeef"


@pytest.mark.parametrize("bad_header", ["", "   ", "not-base64-at-all"])
def test_malformed_payment_header_fails_closed(monkeypatch, bad_header):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    def _explode(_header):
        raise ValueError("malformed payment header")

    monkeypatch.setattr(module, "decode_payment_signature_header", _explode)
    assert module.verify_and_settle_sync(bad_header, price="$0.03") is False
