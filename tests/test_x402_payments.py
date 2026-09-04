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
import logging
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
X402_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "x402_payments.py"


# 0x + exactly 40 hex. The default used to be "0xabc", so five tests below
# asserted verify/settle behaviour under a pay-to address that could never
# receive a payment. is_configured() now shape-checks it, so a placeholder
# here silently disables the very rail these tests exercise.
VALID_PAY_TO = "0x32b08c5e927c69877d0fcab35618c265674922bc"


def _load_x402(monkeypatch, *, facilitator="https://facilitator.example",
               pay_to=VALID_PAY_TO, auth_headers=None):
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


def _install_fake_server(monkeypatch, module, *, valid=True, settled=True,
                         settle_response=None):
    """Stand in for x402ResourceServer with the REAL library's shape.

    initialize() is deliberately a plain MagicMock (sync, returns a
    non-awaitable) because that is exactly what the real method is -- an
    AsyncMock here would hide the very bug this file exists to catch.

    `settle_response` replaces the MagicMock settle result with a real
    x402 SettleResponse, for tests that encode it into the receipt header.
    """
    server = MagicMock()
    server.initialize = MagicMock(return_value=None)
    server.register = MagicMock()
    # Concrete amount so the Stripe-recording path can do arithmetic on it;
    # atomic USDC units, $0.03. A bare MagicMock would make int() raise and
    # silently skip recording in every test that goes through this helper.
    requirement = MagicMock()
    requirement.amount = "30000"
    server.build_payment_requirements = MagicMock(return_value=[requirement])

    verify_result = MagicMock()
    verify_result.is_valid = valid
    settle_result = MagicMock()
    settle_result.success = settled
    settle_result.transaction = "0xsettledtx"
    if settle_response is not None:
        settle_result = settle_response

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
    module = _load_x402(monkeypatch, pay_to=VALID_PAY_TO)
    _install_fake_server(monkeypatch, module)
    body = module.payment_required_body(price="$0.03")

    assert body["payTo"] == VALID_PAY_TO
    assert body["accepted_payment_header"] == "X-PAYMENT"
    assert body["price"] == "$0.03"

    entry = module.accepts_entry(price="$0.03")
    assert entry["scheme"] == "exact"
    assert entry["payTo"] == VALID_PAY_TO, "the spec spells it payTo; pay_to is unreadable"


@pytest.mark.parametrize(
    "bad_address",
    [
        "0x32b08c5e927c69877d0fcab35618c265674922b",   # 39 hex -- one short
        "0x32b08c5e927c69877d0fcab35618c265674922bcd",  # 41 hex -- one long
        "0xabc",                                        # a placeholder
        "0x32b08c5e927c69877d0fcab35618c26567492zz",   # right length, not hex
        "32b08c5e927c69877d0fcab35618c265674922bc",    # 40 hex, missing 0x
        "changeme",
        # Shape-valid but unownable: passes every format check, and USDC
        # reverts transfers to address(0). Shipped once, live, for real.
        "0x0000000000000000000000000000000000000000",
    ],
)
def test_a_malformed_pay_to_address_never_advertises_x402(monkeypatch, bad_address):
    """A recipient that cannot receive must not be offered as a live rail.

    This is the incident, not a hypothetical: a deployment ran with a 16-hex
    pay-to address while advertising x402 as live, so every agent that found
    the service through the Bazaar built a payment to an address that could
    not receive it. Nothing errored. From this side it was indistinguishable
    from nobody wanting to buy.

    `bool(_PAY_TO_ADDRESS)` was the entire check, so every string below used
    to switch the rail ON. The deploy preflight catches some of these, but
    only when the value is a plain env var -- a Secret Manager value is
    explicitly not shape-checked there, so this is the only check that holds
    wherever the value came from.
    """
    module = _load_x402(monkeypatch, pay_to=bad_address)

    assert module.is_configured() is False
    assert module.payment_required_body(price="$0.03") == {}
    assert module.accepts_entry(price="$0.03") is None


def test_a_non_evm_network_is_not_held_to_the_evm_address_shape(monkeypatch):
    """0x + 40 hex is an EVM address format. Enforcing it on a Solana or
    other non-eip155 deployment would fail closed on a correctly configured
    service -- the same false-negative this guard exists to prevent, pointed
    the other way."""
    monkeypatch.setenv("X402_NETWORK", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    module = _load_x402(monkeypatch, pay_to="9mcxc1SomeSolanaStyleAddressVG12")

    assert module.is_configured() is True


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


# --- Coinbase CDP facilitator --------------------------------------------
#
# CDP is the facilitator that matters commercially: it settles on mainnet and
# it is what gets a resource listed in the x402 Bazaar. It signs a fresh JWT
# per call, bound to that call's method, host and FULL path -- so unlike a
# bearer token these headers cannot be computed once and reused, and a wrong
# path means every request is rejected with a signature that looks valid.


def _cdp_secret():
    """A real Ed25519 keypair in CDP's base64(private||public) format, so the
    SDK actually signs rather than being mocked into agreeing with us."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw + pub).decode()


def _load_cdp(monkeypatch, url="https://api.cdp.coinbase.com/platform/v2/x402", **kw):
    monkeypatch.setenv("CDP_API_KEY_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("CDP_API_KEY_SECRET", _cdp_secret())
    return _load_x402(monkeypatch, facilitator=url, **kw)


def _jwt_claims(header_value):
    import base64

    payload = header_value.split(" ", 1)[1].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_cdp_signs_each_endpoint_with_its_own_method_and_full_path(monkeypatch):
    """The signed `uris` claim must name the real method and the real path,
    prefix included. CDP's facilitator lives under /platform/v2/x402, and a
    JWT signed for the bare path authenticates nothing -- which would present
    as x402 configured and every payment rejected."""
    module = _load_cdp(monkeypatch)
    headers = module._auth_provider().get_auth_headers()

    expected = {
        "verify": "POST api.cdp.coinbase.com/platform/v2/x402/verify",
        "settle": "POST api.cdp.coinbase.com/platform/v2/x402/settle",
        "supported": "GET api.cdp.coinbase.com/platform/v2/x402/supported",
        "bazaar": "GET api.cdp.coinbase.com/platform/v2/x402/discovery/resources",
    }
    for endpoint, uri in expected.items():
        claims = _jwt_claims(getattr(headers, endpoint)["Authorization"])
        assert claims["uris"] == [uri], f"{endpoint} signed for the wrong request"


def test_cdp_tokens_are_distinct_per_endpoint(monkeypatch):
    """Reusing one token across endpoints is the obvious shortcut and it does
    not work -- each is bound to its own method and path."""
    module = _load_cdp(monkeypatch)
    headers = module._auth_provider().get_auth_headers()
    tokens = {
        getattr(headers, e)["Authorization"]
        for e in ("verify", "settle", "supported", "bazaar")
    }
    assert len(tokens) == 4


def test_cdp_takes_precedence_over_static_headers(monkeypatch):
    """Nobody sets a CDP key pair by accident; it is the more specific config."""
    module = _load_cdp(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer stale"})
    )
    provider = module._auth_provider()
    assert type(provider).__name__ == "_CdpAuthProvider"
    assert "stale" not in provider.get_auth_headers().verify["Authorization"]


def test_cdp_rejects_a_facilitator_url_it_cannot_sign_for(monkeypatch):
    """Without a host there is nothing to bind the JWT to, so fail loudly at
    construction rather than emitting tokens no facilitator will accept."""
    with pytest.raises(ValueError):
        _load_cdp(monkeypatch, url="not-a-url")._auth_provider()


def test_static_headers_still_used_when_no_cdp_credentials(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    module = _load_x402(
        monkeypatch, auth_headers=json.dumps({"Authorization": "Bearer tok"})
    )
    assert type(module._auth_provider()).__name__ == "_StaticAuthProvider"


# --- Recording settlements in Stripe -------------------------------------
#
# The pattern from Stripe's machine-payments sample: after the facilitator
# settles USDC on-chain, mirror it into Stripe as a PaymentIntent in
# transaction_verification mode. This is what makes x402 revenue appear in
# the Stripe balance instead of accumulating invisibly on an address -- and
# "earning while reading zero" is the one confusion this project cannot
# afford, because zero is also what no demand looks like.


def _settlement(tx="0xtxhash", success=True, amount="30000"):
    """A settle result + requirements pair shaped like the real library's:
    amount is atomic USDC units (6 decimals), $0.03 == 30_000."""
    result = MagicMock()
    result.transaction = tx
    result.success = success
    requirements = MagicMock()
    requirements.amount = amount
    return result, requirements


def _capture_payment_intents(monkeypatch, module, *, boom=False):
    """Intercept stripe.PaymentIntent.create inside the module under test."""
    import stripe

    calls = []

    def _create(**kwargs):
        if boom:
            raise RuntimeError("stripe exploded")
        calls.append(kwargs)
        pi = MagicMock()
        pi.id = "pi_test"
        return pi

    monkeypatch.setattr(stripe.PaymentIntent, "create", staticmethod(_create))
    return calls


def test_a_settled_payment_is_recorded_as_a_stripe_payment_intent(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement(amount="30000")  # $0.03
    module.record_settlement_in_stripe(result, requirements)

    assert len(calls) == 1
    call = calls[0]
    assert call["amount"] == 3, "30_000 atomic USDC units is 3 cents"
    assert call["currency"] == "usd"
    opts = call["payment_method_options"]["crypto"]
    assert opts["mode"] == "transaction_verification"
    assert opts["transaction_verification_options"]["network"] == "base"
    assert opts["transaction_verification_options"]["transaction_hash"] == "0xtxhash"


def test_recording_is_idempotent_by_transaction_hash(monkeypatch):
    """A retry or double call must not double-count revenue. The idempotency
    key IS the transaction hash, so Stripe collapses duplicates server-side."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement(tx="0xsame")
    module.record_settlement_in_stripe(result, requirements)

    assert calls[0]["idempotency_key"] == "0xsame"


def test_recording_failure_never_fails_the_settlement(monkeypatch):
    """By the time recording runs the money has already moved on-chain. A
    bookkeeping failure that turned into a payment failure would refuse
    service to a caller who has already paid -- the worst possible outcome."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    _capture_payment_intents(monkeypatch, module, boom=True)
    _install_fake_server(monkeypatch, module)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is True


def test_settlement_still_succeeds_without_a_stripe_key(monkeypatch):
    """Stripe recording is optional bookkeeping, not a payment dependency.
    A deployment paying to a self-custody wallet has no Stripe to record
    into, and its payments must still settle."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)
    _install_fake_server(monkeypatch, module)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is True
    assert calls == [], "no key, no PaymentIntent -- and no crash"


def test_a_failed_settlement_is_never_recorded(monkeypatch):
    """Recording an unsettled payment would invent revenue in Stripe that
    never arrived on-chain -- bookkeeping fraud by bug."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement(success=False)
    module.record_settlement_in_stripe(result, requirements)
    result2, requirements2 = _settlement(tx=None)
    module.record_settlement_in_stripe(result2, requirements2)

    assert calls == []


def test_an_unmapped_network_skips_recording_rather_than_guessing(monkeypatch):
    """transaction_verification verifies against a named network. Guessing
    the name records the payment against the wrong chain, which is worse
    than not recording: it looks reconciled and is not."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    monkeypatch.setenv("X402_NETWORK", "eip155:1")  # Ethereum mainnet, unmapped
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement()
    module.record_settlement_in_stripe(result, requirements)

    assert calls == []


def test_sub_cent_settlements_are_not_recorded(monkeypatch):
    """Stripe rejects zero-cent PaymentIntents; a sub-cent settlement would
    turn every recording attempt into a logged error."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement(amount="4000")  # $0.004
    module.record_settlement_in_stripe(result, requirements)

    assert calls == []


def test_settle_sync_records_after_a_successful_settle(monkeypatch):
    """Both settle paths must record -- settle_sync is the one the paid
    routes actually use (verify first, deliver, then settle)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is True
    assert len(calls) == 1


def test_verify_and_settle_records_after_a_successful_settle(monkeypatch):
    """The legacy /audit route settles through verify_and_settle_sync, not
    settle_sync -- if only one path records, revenue splits into visible and
    invisible depending on which route the agent happened to call."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("X402_STRIPE_MIRROR", "1")
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)
    _install_fake_server(monkeypatch, module)

    assert module.verify_and_settle_sync("signed-payment", price="$0.03") is True
    assert len(calls) == 1


# --- Switching away from CDP -------------------------------------------------
#
# The handoff bills leaving CDP as "one env var": point X402_FACILITATOR_URL at
# a keyless facilitator, redeploy. The CDP key pair stays mounted on the Cloud
# Run service, so that claim only holds if the credentials stop being used when
# the facilitator is no longer Coinbase's.


def test_cdp_credentials_are_not_sent_to_a_non_coinbase_facilitator(monkeypatch):
    """A CDP token is a JWT bound to Coinbase's own host, not a shared secret
    anyone else could validate. Signing a third-party facilitator's requests
    with one is meaningless at best and a 401 at worst -- and a 401 here is
    the worst failure this file knows: x402 still advertised on every 402,
    every payment rejected, indistinguishable from nobody buying."""
    module = _load_cdp(monkeypatch, url="https://facilitator.xpay.sh")
    assert module._auth_provider() is None, (
        "CDP credentials were handed to a facilitator that cannot validate them"
    )


def test_the_documented_one_variable_swap_actually_works(monkeypatch):
    """The whole point: with the CDP key pair still mounted, changing only
    X402_FACILITATOR_URL must leave a working, advertised x402 rail."""
    module = _load_cdp(monkeypatch, url="https://facilitator.xpay.sh")
    _install_fake_server(monkeypatch, module)
    assert module.is_configured(), "the rail stopped being advertised"
    assert module.accepts_entry(price="$0.03") is not None


def test_static_headers_still_reach_a_non_coinbase_facilitator(monkeypatch):
    """Ignoring CDP must fall through to the generic credential path, not
    swallow it -- a facilitator that wants a bearer token still gets one."""
    module = _load_cdp(
        monkeypatch,
        url="https://facilitator.example.com",
        auth_headers=json.dumps({"Authorization": "Bearer tok"}),
    )
    provider = module._auth_provider()
    assert provider is not None
    assert provider.get_auth_headers().verify == {"Authorization": "Bearer tok"}


def test_cdp_is_still_used_for_coinbase_hosts(monkeypatch):
    """The fall-through must not disarm CDP where it is the right credential."""
    module = _load_cdp(monkeypatch)
    provider = module._auth_provider()
    assert provider is not None
    assert type(provider).__name__ == "_CdpAuthProvider"


@pytest.mark.parametrize(
    "host",
    [
        "https://api.cdp.coinbase.com/platform/v2/x402",
        "https://coinbase.com/x402",
        "https://user:pw@api.cdp.coinbase.com:443/platform/v2/x402",
    ],
)
def test_coinbase_hosts_are_recognised_through_port_and_userinfo(monkeypatch, host):
    module = _load_cdp(monkeypatch, url=host)
    assert module._host_is_coinbase(host) is True


@pytest.mark.parametrize(
    "host",
    [
        # The lookalike that matters: suffix matching on "coinbase.com" without
        # the leading dot would accept this and hand over the key pair.
        "https://api.cdp.coinbase.com.evil.example/x402",
        "https://notcoinbase.com/x402",
        "https://facilitator.xpay.sh",
    ],
)
def test_lookalike_hosts_never_receive_cdp_credentials(monkeypatch, host):
    module = _load_cdp(monkeypatch, url=host)
    assert module._host_is_coinbase(host) is False
    assert module._auth_provider() is None


# --- Bazaar discovery records must survive the facilitator's own validator ---
#
# The Bazaar half of a 402 is only worth emitting if a facilitator will
# actually catalog it, and a facilitator that validates before cataloging runs
# exactly the check below. These assert against the x402 library's own
# `validate_discovery_extension` rather than against a hand-written expected
# dict, because the thing that matters is not "does this look right to us" but
# "does the indexer accept it". It did not: `declare_discovery_extension`
# leaves `method` to be enriched by machinery this service does not use, so
# every record went out failing its own co-emitted schema.

def _validate_bazaar(extension: dict):
    from x402.extensions.bazaar import validate_discovery_extension

    assert "bazaar" in extension, "x402 is configured, so a record must be emitted"
    return validate_discovery_extension(extension["bazaar"])


def test_a_body_route_discovery_record_passes_the_facilitator_validator(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    extension = module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        output_example={"pass": True},
    )
    result = _validate_bazaar(extension)
    assert result.valid, result.errors


def test_a_body_route_discovery_record_names_the_http_method(monkeypatch):
    """The paid routes are POST-only. A record that omits the method is not
    just schema-invalid -- an agent reading it has no way to know how to
    call the resource it just found."""
    module = _load_x402(monkeypatch)
    extension = module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"},
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    assert extension["bazaar"]["info"]["input"]["method"] == "POST"


def test_an_mcp_tool_discovery_record_passes_the_facilitator_validator(monkeypatch):
    module = _load_x402(monkeypatch)
    extension = module.bazaar_extension_for_mcp_tool(
        tool_name="audit_wcag",
        description="WCAG 2.1 A/AA accessibility audit via axe-core. $0.03 per call.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        example={"url": "https://example.com"},
    )
    result = _validate_bazaar(extension)
    assert result.valid, result.errors


def test_no_discovery_record_is_emitted_when_x402_cannot_settle(monkeypatch):
    """Same fail-closed rule as every other x402 surface: an unpayable
    resource must not be advertised in an index agents shop by capability."""
    module = _load_x402(monkeypatch, facilitator=None, pay_to=None)
    assert module.bazaar_extension_for_body(
        input_example={"url": "https://example.com"}, input_schema={"type": "object"}
    ) == {}
    assert module.bazaar_extension_for_mcp_tool(
        tool_name="audit_wcag", description="d", input_schema={"type": "object"}
    ) == {}


# --- a refused payment must say WHY, in the log Cloud Run keeps ------------
#
# The first real paid call against the deployed node came back as a bare 402
# re-challenge. The facilitator's invalid_reason -- or the exception that
# stopped verify from ever reaching it -- existed for a few milliseconds
# inside this process and was discarded by `except Exception: return None`.
# The Cloud Run log had nothing; the owner had the word "rejected". These pin
# the reason into the log at WARNING, which the default handler emits to
# stderr and Cloud Run captures. The fail-closed return values are asserted
# unchanged in every case: the log is what changed, not the contract.


def _rejecting_verify(server, *, reason, message="the facilitator said no"):
    result = MagicMock()
    result.is_valid = False
    result.invalid_reason = reason
    result.invalid_message = message
    result.payer = "0xpayer"

    async def _verify(*a, **k):
        return result

    server.verify_payment = _verify


def _exploding_verify(server, exc):
    async def _verify(*a, **k):
        raise exc

    server.verify_payment = _verify


def test_a_facilitator_rejection_is_logged_with_its_reason(monkeypatch, caplog):
    module = _load_x402(monkeypatch, facilitator="https://fac.example")
    server = _install_fake_server(monkeypatch, module)
    _rejecting_verify(server, reason="insufficient_funds")

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None

    text = caplog.text
    assert "REJECTED" in text
    assert "insufficient_funds" in text
    assert "the facilitator said no" in text
    assert "https://fac.example" in text
    assert "$0.03" in text


def test_an_unreachable_facilitator_is_logged_as_such_not_as_a_rejection(monkeypatch, caplog):
    """A refusal and an outage need different fixes. Collapsing both into a
    402 is how a week gets spent on the wrong one."""
    module = _load_x402(monkeypatch, facilitator="https://fac.example")
    server = _install_fake_server(monkeypatch, module)
    _exploding_verify(server, ConnectionError("Name or service not known"))

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None

    text = caplog.text
    assert "FAILED before the facilitator could answer" in text
    assert "ConnectionError" in text
    assert "Name or service not known" in text
    assert "REJECTED" not in text


def test_the_legacy_verify_and_settle_path_logs_the_same_reason(monkeypatch, caplog):
    """/audit goes through verify_and_settle_sync, not verify_only_sync. A
    reason logged on one path and not the other splits diagnosability by
    which route the agent happened to call."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _rejecting_verify(server, reason="invalid_signature")

    with caplog.at_level(logging.WARNING):
        assert module.verify_and_settle_sync("signed", price="$0.03") is False

    assert "invalid_signature" in caplog.text


def test_a_refused_settlement_is_logged_after_delivery(monkeypatch, caplog):
    """This is the case where an audit went out unpaid. It must be the
    loudest of all, and it must carry the facilitator's reason."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module, settled=False)

    settle_result = MagicMock()
    settle_result.success = False
    settle_result.error_reason = "authorization_expired"
    settle_result.error_message = "past validBefore"

    async def _settle(*a, **k):
        return settle_result

    server.settle_payment = _settle

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    with caplog.at_level(logging.WARNING):
        assert module.settle_sync(pending) is False

    text = caplog.text
    assert "settle REFUSED" in text
    assert "authorization_expired" in text
    assert "past validBefore" in text


def test_a_valid_payment_logs_no_rejection(monkeypatch, caplog):
    """The guard must not cry wolf: a clean payment produces no REJECTED or
    FAILED line, or the log becomes noise on exactly the day it matters."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is not None

    assert "REJECTED" not in caplog.text
    assert "FAILED" not in caplog.text


def test_the_stripe_mirror_is_off_unless_asked_for(monkeypatch, caplog):
    """On this deployment the pay-to is a self-custody wallet, so mirroring a
    settlement into Stripe cannot succeed -- and the old default-on behaviour
    would have logged, on every real payment, a traceback saying Stripe 'will
    not show it until this transaction hash is recorded'. A log line that is
    false on the one day someone reads it is worse than no line.

    With the key set and the flag absent: no PaymentIntent, no exception,
    one INFO line that says where the money actually is."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.delenv("X402_STRIPE_MIRROR", raising=False)
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)
    _install_fake_server(monkeypatch, module)

    with caplog.at_level(logging.INFO):
        assert module.verify_and_settle_sync("signed-payment", price="$0.03") is True

    assert calls == []
    assert "will not appear in Stripe" in caplog.text
    assert "Traceback" not in caplog.text
    assert "recording it in Stripe failed" not in caplog.text


# --- never advertise an x402 version the facilitator will not verify --------
#
# Found by simulation. Against a facilitator whose /supported lists only the
# legacy v1 name ("base"), the node still sent the v2 PAYMENT-REQUIRED header
# naming eip155:8453. A v2-capable client took the offer and signed for
# eip155:8453; the node then raised SchemeNotFoundError before the facilitator
# was called and failed closed into a bare 402 -- every time, whatever the
# wallet held. The two rejected live attempts had exactly that shape.


def _facilitator_that_supports(server, *versions):
    """Stand in for the cached /supported: a kind exists only for `versions`."""
    server.get_supported_kind = (
        lambda version, network, scheme: object() if version in versions else None
    )


def test_the_v2_header_is_withheld_when_the_facilitator_is_v1_only(monkeypatch, caplog):
    module = _load_x402(monkeypatch, facilitator="https://v1only.example")
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 1)

    with caplog.at_level(logging.WARNING):
        assert module.payment_required_header(price="$0.03") == {}
    # v1 is still offered, so the rail stays payable for v1 clients.
    assert module.accepts_entry(price="$0.03") is not None
    assert "v2 on eip155:8453 will NOT be advertised" in caplog.text
    assert "https://v1only.example" in caplog.text


def test_the_v1_body_is_withheld_when_the_facilitator_is_v2_only(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 2)

    with caplog.at_level(logging.WARNING):
        assert module.accepts_entry(price="$0.03") is None
    assert module.payment_required_header(price="$0.03") != {}
    assert "v1 on base will NOT be advertised" in caplog.text


def test_both_versions_are_offered_when_the_facilitator_supports_both(monkeypatch):
    """The gate must refuse what cannot be verified, not become a third gate
    on the normal case."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)
    _facilitator_that_supports(server, 1, 2)

    assert module.accepts_entry(price="$0.03") is not None
    assert "PAYMENT-REQUIRED" in module.payment_required_header(price="$0.03")


def test_an_unreachable_facilitator_withholds_both_versions_and_says_so(monkeypatch, caplog):
    """Fail-closed: a challenge nobody can pay reads as nobody buying."""
    module = _load_x402(monkeypatch)
    server = _install_fake_server(monkeypatch, module)

    def boom(version, network, scheme):
        raise ConnectionError("facilitator down")

    server.get_supported_kind = boom

    with caplog.at_level(logging.WARNING):
        assert module.payment_required_header(price="$0.03") == {}
        assert module.accepts_entry(price="$0.03") is None
    assert "ConnectionError: facilitator down" in caplog.text


def test_a_legacy_only_facilitator_is_offered_nothing_and_the_log_says_why(monkeypatch, caplog):
    """The library builds every verification's requirements under the CAIP-2
    name and refuses the legacy one outright (parse_price("$0.03", "base")
    raises "Unsupported network format"). So a facilitator listing only
    "base" can be offered nothing -- not even v1 -- or the node takes a
    signature it can never build the requirements to verify. Simulated:
    v1 offered, v1 paid, SchemeNotFoundError for eip155:8453 with the
    facilitator never called. That is the shape of the live rejections."""
    module = _load_x402(monkeypatch, facilitator="https://legacy.example")
    server = _install_fake_server(monkeypatch, module)
    server.get_supported_kind = (
        lambda version, network, scheme: object() if network == "base" else None
    )

    with caplog.at_level(logging.WARNING):
        assert module.accepts_entry(price="$0.03") is None
        assert module.payment_required_header(price="$0.03") == {}
    assert "lists 'base' but not 'eip155:8453'" in caplog.text
    assert "CAIP-2" in caplog.text


# --- verify/settle must work on a thread that hosts a running event loop ----
#
# Playwright's sync API (app/browser_pool.py) keeps a running loop in each
# worker thread for the life of the pooled browser, and anyio reuses those
# threads. The first real paid call against the deployed node died on this:
# the live log read "RuntimeError: asyncio.run() cannot be called from a
# running event loop", verify raised before the facilitator was contacted,
# and the caller saw a bare 402. Reproduced deterministically against a local
# node with MAX_CONCURRENT_AUDITS=1: one audit, then one paid call. These
# reproduce the same condition in-process: the sync entry points are invoked
# from inside a running loop, exactly as on a poisoned worker thread.

import asyncio as _asyncio


def _call_in_running_loop(fn, *args, **kwargs):
    async def _inner():
        return fn(*args, **kwargs)

    return _asyncio.run(_inner())


def test_verify_only_sync_works_inside_a_running_loop(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = _call_in_running_loop(module.verify_only_sync, "signed", price="$0.03")
    assert pending is not None


def test_verify_and_settle_sync_works_inside_a_running_loop(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    assert _call_in_running_loop(
        module.verify_and_settle_sync, "signed", price="$0.03"
    ) is True


def test_settle_sync_works_inside_a_running_loop(monkeypatch):
    """The settle path is the one where this bug costs money directly: the
    audit was already delivered, and a settle that dies on the loop check
    delivers it unpaid."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    assert _call_in_running_loop(module.settle_sync, pending) is True


def test_no_bare_asyncio_run_remains_on_the_payment_path():
    """A new call site written with plain asyncio.run() reintroduces the bug
    on exactly the threads that have ever served an audit. Counted off the
    AST, not grepped, so docstrings and comments cannot confuse it."""
    import ast

    tree = ast.parse(X402_PATH.read_text())
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run_coro_sync")
    ok_lines = set(range(helper.lineno, helper.end_lineno + 1))
    stray = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "asyncio"
        and node.lineno not in ok_lines
    ]
    assert stray == [], f"bare asyncio.run() at lines {stray} -- use _run_coro_sync"


# --- the settlement receipt -------------------------------------------------
#
# x402 spec step 10: after settling, the resource server hands the
# facilitator's settle response back to the payer in PAYMENT-RESPONSE. Until
# this, a paying agent got an audit and no transaction hash -- proof of
# nothing to reconcile against its wallet. Found by simulate-paid-call.py,
# the first thing to read the headers of a paid 200; every test before it
# stopped at the status code.

_RECEIPT_TX = "0x" + "ab" * 32


def _real_settle_response():
    from x402.schemas import SettleResponse

    return SettleResponse(
        success=True, transaction=_RECEIPT_TX, network="eip155:8453", payer="0x" + "11" * 20
    )


def test_settle_sync_keeps_the_settlement_for_the_receipt(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())

    pending = module.verify_only_sync("signed-payment", price="$0.03")
    assert pending.settle_result is None, "nothing to receipt before settlement"
    assert module.settle_sync(pending) is True
    assert pending.settle_result.transaction == _RECEIPT_TX


def test_receipt_headers_decode_with_the_x402_client(monkeypatch):
    """Both header names, same value, and the x402 library's own decoder --
    the one a paying client uses -- reads the transaction back out."""
    from x402.http.utils import decode_payment_response_header

    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    module.settle_sync(pending)

    headers = module.receipt_headers(pending)

    assert set(headers) == {"PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"}
    assert headers["X-PAYMENT-RESPONSE"] == headers["PAYMENT-RESPONSE"]
    decoded = decode_payment_response_header(headers["PAYMENT-RESPONSE"])
    assert decoded.success is True
    assert decoded.transaction == _RECEIPT_TX


def test_no_receipt_for_a_refused_settlement(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settled=False)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.settle_sync(pending) is False
    assert pending.settle_result is None
    assert module.receipt_headers(pending) == {}


def test_no_receipt_before_settlement_and_none_without_a_payment(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    assert module.receipt_headers(pending) == {}
    assert module.receipt_headers(None) == {}


def test_an_unencodable_receipt_is_logged_and_never_raises(monkeypatch, caplog):
    """The money has moved by the time the receipt is built. A receipt that
    cannot be encoded is a bookkeeping gap to log, never a reason to turn a
    paid, delivered audit into a 500."""
    import types

    module = _load_x402(monkeypatch)
    pending = module.PendingPayment(None, None, "$0.03")
    pending.settle_result = types.SimpleNamespace(success=True)  # no model_dump_json

    with caplog.at_level(logging.WARNING):
        assert module.receipt_headers(pending) == {}
    assert "receipt could not be encoded" in caplog.text


# --- one event loop for every facilitator call -------------------------------
#
# The facilitator client keeps one httpx.AsyncClient with pooled keep-alive
# connections, and a pooled connection is bound to the loop that opened it.
# asyncio.run() per call meant a new loop per call, and against a keep-alive
# facilitator at 16 concurrent payers 56 of 96 payments died in this node with
# "Event loop is closed" / "bound to a different event loop" -- facilitator
# never asked. Every facilitator coroutine now runs on one long-lived loop.


def test_every_facilitator_call_runs_on_the_same_loop_from_any_thread(monkeypatch):
    import asyncio
    import threading

    module = _load_x402(monkeypatch)

    async def which_loop():
        return id(asyncio.get_running_loop())

    seen = []
    lock = threading.Lock()

    def worker():
        for _ in range(3):
            loop_id = module._run_coro_sync(which_loop())
            with lock:
                seen.append(loop_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 24
    assert len(set(seen)) == 1, "facilitator coroutines ran on more than one event loop"


def test_the_shared_loop_is_not_the_callers_loop(monkeypatch):
    """The #83 case: a caller thread that already hosts a running loop. The
    facilitator coroutine must run elsewhere, never on that loop."""
    import asyncio

    module = _load_x402(monkeypatch)

    async def which_loop():
        return id(asyncio.get_running_loop())

    async def caller():
        mine = id(asyncio.get_running_loop())
        theirs = module._run_coro_sync(which_loop())
        return mine, theirs

    mine, theirs = asyncio.run(caller())
    assert mine != theirs


def test_a_facilitator_call_that_never_answers_is_bounded(monkeypatch):
    import asyncio
    import time

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_FACILITATOR_CALL_TIMEOUT", 0.2)

    async def hangs():
        await asyncio.sleep(30)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        module._run_coro_sync(hangs())
    assert time.monotonic() - started < 5


# --- the facilitator outage must not become a node outage ---------------------


def test_an_unreachable_facilitator_is_not_re_probed_on_every_request(monkeypatch):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    with pytest.raises(RuntimeError):
        module._get_server()
    with pytest.raises(RuntimeError) as second:
        module._get_server()
    assert boom.initialize.call_count == 1, "the outage was re-probed on the next request"
    assert "not retried" in str(second.value)
    assert "connection refused" in str(second.value), "the original failure is carried in the message"

    # Once the window has passed, it tries again.
    monkeypatch.setattr(module, "_SERVER_RETRY_SECONDS", 0.0)
    with pytest.raises(RuntimeError):
        module._get_server()
    assert boom.initialize.call_count == 2


def test_a_failed_facilitator_still_fails_closed_fast(monkeypatch):
    """During the back-off window the 402 simply carries no x402 -- no
    exception escapes, and nothing waits on the network."""
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module)
    boom = MagicMock()
    boom.register = MagicMock()
    boom.initialize = MagicMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr(module, "x402ResourceServer", MagicMock(return_value=boom))

    assert module.payment_required_header("$0.03") == {}
    assert module.accepts_entry("$0.03") is None
    assert module.verify_only_sync("signed-payment", price="$0.03") is None
    assert boom.initialize.call_count == 1


def test_supported_is_fetched_with_a_short_timeout(monkeypatch):
    """/supported is read under the module lock, so its timeout is how long
    every 402 waits when the facilitator is down. Eight seconds, not the
    library's thirty."""
    module = _load_x402(monkeypatch)
    client = module._FacilitatorClient(module.FacilitatorConfig(url="https://facilitator.example"))
    http = client._get_sync_client()
    try:
        assert http.timeout.connect == 8.0
        assert http.timeout.read == 8.0
    finally:
        http.close()


# --- replay: one signed authorization buys one audit -------------------------


def _payload_with_nonce(nonce: str):
    payload = MagicMock()
    payload.payload = {"authorization": {"nonce": nonce, "from": "0x" + "11" * 20}, "signature": "0xsig"}
    return payload


def _counting_fake_server(monkeypatch, module, **kwargs):
    server = _install_fake_server(monkeypatch, module, **kwargs)
    calls = {"verify": 0}
    original = server.verify_payment

    async def _verify(*a, **k):
        calls["verify"] += 1
        return await original(*a, **k)

    server.verify_payment = _verify
    return calls


def test_a_replayed_authorization_is_refused_before_the_facilitator(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xAA"))

    assert module.verify_only_sync("signed", price="$0.03") is not None
    with caplog.at_level(logging.WARNING):
        assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 1, "the replay reached the facilitator"
    assert "replayed authorization" in caplog.text


def test_a_nonce_is_released_when_the_facilitator_rejects_it(monkeypatch):
    """A retry after a transient rejection or outage is legitimate."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module, valid=False)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xBB"))

    assert module.verify_only_sync("signed", price="$0.03") is None
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 2, "a nonce whose verify failed must be admitted again"


def test_a_nonce_is_released_when_verify_raises(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xCC"))

    def facilitator_down(coro, *a, **k):
        coro.close()  # never awaited on purpose; close it so Python does not warn
        raise RuntimeError("facilitator down")

    monkeypatch.setattr(module, "_run_coro_sync", facilitator_down)

    assert module.verify_only_sync("signed", price="$0.03") is None
    monkeypatch.undo()
    module = _load_x402(monkeypatch)
    _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xCC"))
    assert module.verify_only_sync("signed", price="$0.03") is not None
    _ = calls


def test_a_nonce_stays_spent_after_a_failed_settle(monkeypatch):
    """Settle failed after delivery: one unpaid audit. Re-sending the same
    signature must not buy a second one."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module, settled=False)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xDD"))

    pending = module.verify_only_sync("signed", price="$0.03")
    assert pending is not None
    assert module.settle_sync(pending) is False
    assert module.verify_only_sync("signed", price="$0.03") is None
    assert calls["verify"] == 1


def test_distinct_nonces_are_independent(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payloads = iter([_payload_with_nonce("0x01"), _payload_with_nonce("0x02")])
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: next(payloads))

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("b", price="$0.03") is not None
    assert calls["verify"] == 2


def test_the_replay_guard_is_case_insensitive_on_the_nonce(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payloads = iter([_payload_with_nonce("0xABCD"), _payload_with_nonce("0xabcd")])
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: next(payloads))

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("b", price="$0.03") is None
    assert calls["verify"] == 1


def test_a_payload_without_a_nonce_is_not_blocked(monkeypatch):
    """Other schemes carry no EIP-3009 nonce; the guard must not refuse them."""
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    payload = MagicMock()
    payload.payload = {"something": "else"}
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: payload)

    assert module.verify_only_sync("a", price="$0.03") is not None
    assert module.verify_only_sync("a", price="$0.03") is not None
    assert calls["verify"] == 2


def test_the_legacy_path_has_the_same_replay_guard(monkeypatch):
    module = _load_x402(monkeypatch)
    calls = _counting_fake_server(monkeypatch, module)
    monkeypatch.setattr(module, "decode_payment_signature_header", lambda h: _payload_with_nonce("0xEE"))

    assert module.verify_and_settle_sync("signed", price="$0.03") is True
    assert module.verify_and_settle_sync("signed", price="$0.03") is False
    assert calls["verify"] == 1


# --- every settlement leaves one countable line in the log -------------------


def test_each_settlement_is_logged_with_its_transaction(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settle_response=_real_settle_response())
    pending = module.verify_only_sync("signed-payment", price="$0.03")

    with caplog.at_level(logging.INFO):
        assert module.settle_sync(pending) is True
    lines = [r for r in caplog.records if "x402 SETTLED" in r.getMessage()]
    assert len(lines) == 1
    assert _RECEIPT_TX in lines[0].getMessage()
    assert "$0.03" in lines[0].getMessage()


def test_a_refused_settlement_is_not_logged_as_settled(monkeypatch, caplog):
    module = _load_x402(monkeypatch)
    _install_fake_server(monkeypatch, module, settled=False)
    pending = module.verify_only_sync("signed-payment", price="$0.03")
    with caplog.at_level(logging.INFO):
        module.settle_sync(pending)
    assert "x402 SETTLED" not in caplog.text


# ---------------------------------------------------------------------------
# The MCP transport: payment in, receipt out, one challenge for both transports.
# ---------------------------------------------------------------------------


def _signed_mcp_payload():
    """A real x402 v2 PaymentPayload, signed by a throwaway key against a
    challenge shaped exactly like this node's -- what the x402 MCP client
    puts in `_meta["x402/payment"]`."""
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import register_exact_evm_client
    from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo

    account = Account.from_key("0x" + "2" * 63 + "1")
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(account))
    challenge = PaymentRequired(
        x402Version=2,
        error="payment_required",
        resource=ResourceInfo(url="https://node.example/mcp", description="d", mimeType="application/json"),
        accepts=[
            PaymentRequirements(
                scheme="exact",
                network="eip155:8453",
                asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                amount="30000",
                payTo=VALID_PAY_TO,
                maxTimeoutSeconds=300,
                extra={"name": "USD Coin", "version": "2"},
            )
        ],
    )
    return client.create_payment_payload(challenge), account


def test_a_meta_payment_dict_reencodes_into_the_header_the_verify_path_reads(monkeypatch):
    from x402.http.utils import decode_payment_signature_header

    module = _load_x402(monkeypatch)
    payload, account = _signed_mcp_payload()
    wire = payload.model_dump(by_alias=True)  # what x402.mcp's client sends

    header = module.payment_header_from_meta(wire)
    assert isinstance(header, str) and header
    decoded = decode_payment_signature_header(header)
    assert decoded.payload["signature"] == wire["payload"]["signature"]
    assert decoded.payload["authorization"]["from"].lower() == account.address.lower()
    # The nonce the replay guard keys on survives the round trip.
    assert module._payment_nonce(decoded) == wire["payload"]["authorization"]["nonce"].lower()


def test_a_meta_payment_json_string_is_accepted_too(monkeypatch):
    """The official server accepts the payload as a JSON string as well."""
    from x402.http.utils import decode_payment_signature_header

    module = _load_x402(monkeypatch)
    payload, _ = _signed_mcp_payload()
    document = payload.model_dump_json(by_alias=True)
    header = module.payment_header_from_meta(document)
    assert decode_payment_signature_header(header).payload["signature"] == payload.payload["signature"]


def test_a_base64_header_in_meta_passes_through_untouched(monkeypatch):
    module = _load_x402(monkeypatch)
    from x402.http.utils import encode_payment_signature_header

    payload, _ = _signed_mcp_payload()
    already = encode_payment_signature_header(payload)
    assert module.payment_header_from_meta(already) == already


@pytest.mark.parametrize("junk", [None, "", "   ", 42, 4.2, True, "{not json", "[unterminated"])
def test_meta_payment_garbage_is_none_never_an_exception(monkeypatch, junk):
    module = _load_x402(monkeypatch)
    assert module.payment_header_from_meta(junk) is None


def test_receipt_meta_carries_the_settlement_where_the_mcp_client_reads_it(monkeypatch):
    from x402.mcp.types import MCPToolResult
    from x402.mcp.utils import extract_payment_response_from_meta

    module = _load_x402(monkeypatch)
    pending = module.PendingPayment(None, None, "$0.03")
    pending.settle_result = _real_settle_response()
    meta = module.receipt_meta(pending)
    assert set(meta) == {"x402/payment-response"}
    assert meta["x402/payment-response"]["transaction"] == pending.settle_result.transaction
    # Read back with the library's own extractor -- the consumer's parser.
    receipt = extract_payment_response_from_meta(
        MCPToolResult(content=[], is_error=False, meta=meta)
    )
    assert receipt is not None and receipt.transaction == pending.settle_result.transaction


def test_no_receipt_meta_for_a_refused_or_absent_settlement(monkeypatch):
    from x402.schemas import SettleResponse

    module = _load_x402(monkeypatch)
    assert module.receipt_meta(None) == {}
    pending = module.PendingPayment(None, None, "$0.03")
    assert module.receipt_meta(pending) == {}, "no settlement, no receipt"
    pending.settle_result = SettleResponse(
        success=False, error_reason="insufficient_funds", transaction="", network="eip155:8453"
    )
    assert module.receipt_meta(pending) == {}, "a receipt on a refused settle is a forged proof of payment"


def test_the_mcp_challenge_is_the_header_challenge(monkeypatch):
    """One builder for both transports: the dict the MCP paywall puts in
    structuredContent must be byte-for-byte what the PAYMENT-REQUIRED header
    decodes to, so the two cannot quote different prices or recipients."""
    from x402.http.utils import decode_payment_required_header

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: True)
    kwargs = dict(price="$0.10", resource_url="https://node.example/mcp",
                  description="bundle", extensions={"bazaar": {"info": {}, "schema": {}}})

    as_dict = module.payment_required_v2_dict(**kwargs)
    header = module.payment_required_header(**kwargs)["PAYMENT-REQUIRED"]
    decoded = decode_payment_required_header(header).model_dump(by_alias=True, exclude_none=True)
    assert as_dict == decoded
    assert as_dict["accepts"][0]["amount"] == "100000"
    assert as_dict["accepts"][0]["payTo"] == VALID_PAY_TO
    assert as_dict["resource"]["url"] == "https://node.example/mcp"
    assert "mimeType" in as_dict["resource"] and None not in as_dict["resource"].values()


def test_the_mcp_challenge_is_empty_whenever_the_header_would_be(monkeypatch):
    """Fail-closed together: not configured, or a facilitator that will not
    verify v2, and there is no structured challenge -- never one naming a
    recipient the node cannot settle to."""
    module = _load_x402(monkeypatch, facilitator=None)
    assert module.payment_required_v2_dict("$0.03") == {}
    assert module.payment_required_header("$0.03") == {}

    module = _load_x402(monkeypatch)
    monkeypatch.setattr(module, "_facilitator_supports", lambda version, network: False)
    assert module.payment_required_v2_dict("$0.03") == {}
    assert module.payment_required_header("$0.03") == {}
