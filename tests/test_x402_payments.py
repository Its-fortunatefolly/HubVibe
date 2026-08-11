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
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
X402_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py"


def _load_x402(monkeypatch, *, facilitator="https://facilitator.example", pay_to="0xabc",
               auth_headers=None):
    if facilitator is None:
        monkeypatch.delenv("X402_FACILITATOR_URL", raising=False)
    else:
        monkeypatch.setenv("X402_FACILITATOR_URL", facilitator)
    if pay_to is None:
        monkeypatch.delenv("X402_PAY_TO_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("X402_PAY_TO_ADDRESS", pay_to)
    if auth_headers is None:
        monkeypatch.delenv("X402_FACILITATOR_AUTH_HEADERS", raising=False)
    else:
        monkeypatch.setenv("X402_FACILITATOR_AUTH_HEADERS", auth_headers)

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


# --- Authenticated facilitators ------------------------------------------
#
# The free public facilitator at x402.org is testnet-only. Any facilitator
# that settles real money on mainnet authenticates the resource server, so
# without a way to send credentials, x402 could only ever have been switched
# on against a facilitator that wanted none -- i.e. not a paying one.


def test_no_auth_provider_when_no_credentials_are_configured(monkeypatch):
    module = _load_x402(monkeypatch)
    assert module._auth_provider() is None


def test_auth_headers_are_sent_on_every_facilitator_endpoint(monkeypatch):
    """verify, settle, supported and bazaar are separate calls. Credentials
    missing from any one of them means that call fails while the others
    succeed -- a partial outage that is far harder to diagnose than a clean
    rejection."""
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok123"})
    )
    headers = module._auth_provider().get_auth_headers()

    for endpoint in ("verify", "settle", "supported", "bazaar"):
        assert getattr(headers, endpoint) == {"Authorization": "Bearer tok123"}, (
            f"{endpoint} would be called without credentials"
        )


def test_auth_provider_is_handed_to_the_facilitator_client(monkeypatch):
    """Building the provider is useless if it never reaches the client."""
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok123"})
    )
    _install_fake_server(monkeypatch, module)

    module.verify_and_settle_sync("signed-payment", price="$0.03")

    assert module.FacilitatorConfig.call_args is not None, "FacilitatorConfig was never built"
    provider = module.FacilitatorConfig.call_args.kwargs.get("auth_provider")
    assert provider is not None, "the facilitator client was configured without credentials"
    assert provider.get_auth_headers().verify == {"Authorization": "Bearer tok123"}


@pytest.mark.parametrize(
    "bad", ['{"Authorization": 5}', '["not", "an", "object"]', "not json at all", '"a string"']
)
def test_malformed_auth_headers_raise_rather_than_silently_dropping(monkeypatch, bad):
    """Silently ignoring bad credentials leaves x402 advertised while the
    facilitator rejects every payment -- indistinguishable from nobody
    buying, and invisible for as long as nobody looks."""
    module = _load_x402(monkeypatch, auth_headers=bad)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        module._auth_provider()
