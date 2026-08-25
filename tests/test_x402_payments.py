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


def _install_fake_server(monkeypatch, module, *, valid=True, settled=True):
    """Stand in for x402ResourceServer with the REAL library's shape.

    initialize() is deliberately a plain MagicMock (sync, returns a
    non-awaitable) because that is exactly what the real method is -- an
    AsyncMock here would hide the very bug this file exists to catch.
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
    body = module.payment_required_body(price="$0.03")

    assert body["payTo"] == VALID_PAY_TO
    assert body["accepted_payment_header"] == "X-PAYMENT"
    assert body["price"] == "$0.03"

    entry = module.accepts_entry(price="$0.03")
    assert entry["protocol"] == "x402"
    assert entry["pay_to"] == VALID_PAY_TO


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
    module = _load_x402(monkeypatch)
    calls = _capture_payment_intents(monkeypatch, module)

    result, requirements = _settlement(amount="4000")  # $0.004
    module.record_settlement_in_stripe(result, requirements)

    assert calls == []


def test_settle_sync_records_after_a_successful_settle(monkeypatch):
    """Both settle paths must record -- settle_sync is the one the paid
    routes actually use (verify first, deliver, then settle)."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
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
