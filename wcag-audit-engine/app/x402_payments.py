"""x402 machine-payment support for the /audit endpoint.

Lets a caller pay per-request with a signed on-chain payment instead of a
Stripe API key. Verification and settlement are delegated entirely to a
facilitator service via the official `x402` package -- nothing here
hand-rolls cryptographic signature checking. That matters: a bug in this
module can only ever cause a real payment to be wrongly rejected, never
cause a fake payment to be wrongly accepted, because the facilitator (not
this code) is the one authority that decides whether a payment is valid.

Fails closed everywhere:
- Not configured (no facilitator URL / pay-to address) -> not usable at all.
- Any exception during verification -> treated as invalid.
- Facilitator says invalid, or settlement fails -> treated as invalid.
There is no code path where an error or missing configuration results in
access being granted.

Requires, at deploy time:
- X402_FACILITATOR_URL   the facilitator's base URL, which does the actual
                          verify+settle on your behalf (e.g. your payment
                          processor's x402 facilitator endpoint)
- X402_PAY_TO_ADDRESS    the wallet address that receives payment
- X402_NETWORK           CAIP-2 network id (default: "eip155:8453", Base mainnet)
- X402_PRICE             default: "$0.03"

Optional, and worth having: when STRIPE_SECRET_KEY is set and the pay-to
address is a Stripe-custodied deposit address (scripts/x402-setup.py mints
one), every settled payment is also recorded as a Stripe PaymentIntent in
transaction_verification mode -- the pattern from Stripe's machine-payments
sample. That is what makes on-chain revenue show up in the Stripe balance,
reporting, and payouts instead of accumulating invisibly on an address.
Recording failures never fail the payment; the money moved on-chain already.
"""

import asyncio
import json
import logging
import os
import re
import threading
from typing import Optional

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.utils import decode_payment_signature_header
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import ResourceConfig

_FACILITATOR_URL = os.environ.get("X402_FACILITATOR_URL")
_PAY_TO_ADDRESS = os.environ.get("X402_PAY_TO_ADDRESS")
_NETWORK = os.environ.get("X402_NETWORK", "eip155:8453")
_PRICE = os.environ.get("X402_PRICE", "$0.03")

# Headers sent with every facilitator call, as a JSON object, e.g.
#   {"Authorization": "Bearer sk_live_..."}
#
# Most hosted facilitators authenticate the resource server rather than
# serving anonymously -- the free public one at x402.org is testnet-only, and
# a mainnet facilitator that settles real money necessarily knows who is
# asking. Without this there was no way to point this service at any of them,
# so x402 could only ever have been switched on against a facilitator that
# wanted no credentials at all.
_FACILITATOR_AUTH_HEADERS = os.environ.get("X402_FACILITATOR_AUTH_HEADERS")

# Coinbase CDP credentials. CDP takes precedence over the static headers
# above when both are set, because it is the more specific configuration --
# nobody sets a CDP key pair by accident.
_CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID")
_CDP_API_KEY_SECRET = os.environ.get("CDP_API_KEY_SECRET")

_server: Optional[x402ResourceServer] = None
_requirements_cache: dict = {}

# Reentrant because _get_requirements calls _get_server while holding it.
_LOCK = threading.RLock()


_pay_to_warned = False


def _warn_pay_to_malformed(address: str) -> None:
    """Say so, once and loudly. A silent fail-closed here looks exactly like
    'x402 was never configured', which is the wrong diagnosis and sends the
    next person hunting for a missing env var that is actually present."""
    global _pay_to_warned
    if _pay_to_warned:
        return
    _pay_to_warned = True
    logging.getLogger(__name__).error(
        "X402_PAY_TO_ADDRESS is set but is not a valid %s address (needs 0x "
        "+ exactly 40 hex characters; this one has %d). x402 will NOT be "
        "advertised on payment challenges until this is corrected -- "
        "advertising it would invite agents to pay into an address that "
        "cannot receive.",
        _NETWORK,
        max(len(address) - 2, 0),
    )


def _warn_pay_to_burn_address() -> None:
    global _pay_to_warned
    if _pay_to_warned:
        return
    _pay_to_warned = True
    logging.getLogger(__name__).error(
        "X402_PAY_TO_ADDRESS is the zero address (0x + 40 zeros). It is "
        "well-formed but unownable: USDC transfers to address(0) revert, so "
        "no payment could ever arrive. x402 will NOT be advertised until a "
        "real recipient address is set."
    )


def _pay_to_is_usable() -> bool:
    """Shape-check the recipient before we ever advertise the rail.

    `bool(_PAY_TO_ADDRESS)` was the whole test, so ANY truthy string turned
    x402 on. That is not hypothetical: a deployment once ran with a 16-hex
    address while advertising x402 as live, so every agent that found the
    service via the Bazaar built a payment to an address that could not
    receive it -- indistinguishable, from this side, from nobody buying.

    scripts/repair-and-deploy.sh preflights this, but only for a PLAIN env
    var: a value supplied via Secret Manager is explicitly not shape-checked
    there. This is the check that holds regardless of where the value came
    from, which is the only place it can be guaranteed.

    Only EVM (eip155:*) networks are checked -- the 0x+40-hex form is an EVM
    address format, and asserting it against a non-EVM network would fail
    closed on a correctly configured deployment.
    """
    if not _PAY_TO_ADDRESS:
        return False
    if not _NETWORK.startswith("eip155:"):
        return True
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", _PAY_TO_ADDRESS):
        # Shape-valid is not payable. The zero address is 0x + 40 hex and
        # passes every format check, but address(0) is unownable and USDC's
        # contract reverts transfers to it -- it is the canonical way to
        # satisfy a shape gate with a value that can never receive money.
        # This is not hypothetical: a deployment ran with exactly this,
        # advertising x402 as live while every settlement was impossible.
        if set(_PAY_TO_ADDRESS[2:]) == {"0"}:
            _warn_pay_to_burn_address()
            return False
        return True
    _warn_pay_to_malformed(_PAY_TO_ADDRESS)
    return False


def is_configured() -> bool:
    return bool(_FACILITATOR_URL and _pay_to_is_usable())


class _StaticAuthProvider:
    """Sends a fixed set of headers on every facilitator call.

    Implements the x402 AuthProvider protocol. The library asks separately
    for verify / settle / supported / bazaar headers; a bearer token or API
    key is the same on all four, so the same dict is returned for each.

    This deliberately does NOT cover facilitators that sign a fresh
    credential per request (Coinbase CDP mints a short-lived JWT from an
    Ed25519 key). Those need their own SDK's header generator, which the
    library accepts via CreateHeadersAuthProvider -- see the README. Faking
    it with a static header would produce a facilitator that rejects every
    payment, which fails closed but silently, and that is the single worst
    outcome for a payment rail.
    """

    __slots__ = ("_headers",)

    def __init__(self, headers: dict):
        self._headers = dict(headers)

    def get_auth_headers(self):
        from x402.http.facilitator_client_base import AuthHeaders

        return AuthHeaders(
            verify=dict(self._headers),
            settle=dict(self._headers),
            supported=dict(self._headers),
            bazaar=dict(self._headers),
        )


# The paths the x402 client actually calls on a facilitator, and the methods
# it uses, read from the library rather than assumed -- CDP signs each request
# against its own method and path, so a wrong guess here authenticates nothing.
_FACILITATOR_ENDPOINTS = {
    "verify": ("POST", "/verify"),
    "settle": ("POST", "/settle"),
    "supported": ("GET", "/supported"),
    "bazaar": ("GET", "/discovery/resources"),
}


class _CdpAuthProvider:
    """Coinbase CDP auth: a fresh JWT per endpoint, signed from the API key.

    CDP binds each token to the exact method, host and path being called, so
    unlike a bearer token these headers cannot be computed once and reused
    across endpoints. That is precisely why the x402 AuthProvider protocol
    asks for verify / settle / supported / bazaar separately.

    CDP is the facilitator worth having: it settles on mainnet, and it is
    what gets a resource listed in the x402 Bazaar, which is how agents find
    a service by capability instead of by URL.
    """

    __slots__ = ("_key_id", "_key_secret", "_host", "_base_path")

    def __init__(self, key_id: str, key_secret: str, base_url: str):
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if not parsed.netloc:
            raise ValueError(f"X402_FACILITATOR_URL is not a valid URL: {base_url!r}")
        self._key_id = key_id
        self._key_secret = key_secret
        self._host = parsed.netloc
        # CDP's facilitator lives under a path prefix
        # (/platform/v2/x402), and the JWT covers the FULL path, so the
        # prefix has to be included or every call is rejected.
        self._base_path = parsed.path.rstrip("/")

    def _headers_for(self, method: str, path: str) -> dict:
        from cdp.auth.utils.http import GetAuthHeadersOptions, get_auth_headers

        return get_auth_headers(
            GetAuthHeadersOptions(
                api_key_id=self._key_id,
                api_key_secret=self._key_secret,
                request_method=method,
                request_host=self._host,
                request_path=self._base_path + path,
            )
        )

    def get_auth_headers(self):
        from x402.http.facilitator_client_base import AuthHeaders

        signed = {
            name: self._headers_for(method, path)
            for name, (method, path) in _FACILITATOR_ENDPOINTS.items()
        }
        return AuthHeaders(**signed)


# Hosts a Coinbase CDP key pair can actually sign for. A CDP token is a JWT
# minted against Coinbase's own key and bound to the request host; it is not a
# shared secret any other facilitator could validate. Sending one anywhere
# else is meaningless at best and a 401 at worst.
_CDP_HOST_SUFFIX = ".coinbase.com"


def _host_is_coinbase(url: str) -> bool:
    """True when `url` names a Coinbase host.

    Raises on a URL with no host at all. That is a different fault from
    "pointed somewhere else on purpose": there is nothing to bind a JWT to
    and nothing for the facilitator client to call either, so it stays a
    loud construction-time failure rather than being downgraded into the
    quiet fall-through that a deliberate facilitator swap deserves.
    """
    from urllib.parse import urlparse

    netloc = urlparse(url).netloc
    if not netloc:
        raise ValueError(f"X402_FACILITATOR_URL is not a valid URL: {url!r}")
    host = netloc.split("@")[-1].split(":")[0].lower()
    return host == "coinbase.com" or host.endswith(_CDP_HOST_SUFFIX)


_cdp_mismatch_warned = False


def _warn_cdp_ignored(url: str) -> None:
    global _cdp_mismatch_warned
    if _cdp_mismatch_warned:
        return
    _cdp_mismatch_warned = True
    logging.getLogger(__name__).warning(
        "CDP credentials are set but X402_FACILITATOR_URL points at %s, which "
        "is not a Coinbase host. A CDP token is bound to Coinbase's own host "
        "and cannot be validated by anyone else, so it is being ignored. This "
        "deployment is talking to that facilitator with "
        "X402_FACILITATOR_AUTH_HEADERS, or keyless if that is unset.",
        url,
    )


def _auth_provider():
    """The facilitator auth provider for this deployment, or None.

    A malformed X402_FACILITATOR_AUTH_HEADERS is raised rather than ignored:
    silently dropping credentials would leave x402 advertised and every
    payment rejected by the facilitator, which looks identical to "nobody is
    buying" and could go unnoticed indefinitely.

    CDP credentials are used ONLY against a Coinbase host. The handoff bills
    switching away from CDP as "one env var" -- point X402_FACILITATOR_URL at
    a keyless facilitator and redeploy -- and that was not true while this
    branch was unconditional: the CDP key pair stays mounted on the service,
    so every call to the new facilitator went out signed with a JWT bound to
    a host Coinbase never issued for. Whether that 401s or is ignored is the
    third party's choice, not ours, and the failure mode if it 401s is the
    worst one this file knows: x402 still advertised on every 402, every
    payment rejected, indistinguishable from nobody buying. Ignoring the
    credentials instead makes the documented one-variable swap actually work,
    and says so in the log rather than deciding it silently.
    """
    if _CDP_API_KEY_ID and _CDP_API_KEY_SECRET:
        if _host_is_coinbase(_FACILITATOR_URL or ""):
            return _CdpAuthProvider(
                _CDP_API_KEY_ID, _CDP_API_KEY_SECRET, _FACILITATOR_URL or ""
            )
        _warn_cdp_ignored(_FACILITATOR_URL or "")

    if not _FACILITATOR_AUTH_HEADERS:
        return None
    headers = json.loads(_FACILITATOR_AUTH_HEADERS)
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise ValueError(
            "X402_FACILITATOR_AUTH_HEADERS must be a JSON object of string headers, "
            'e.g. {"Authorization": "Bearer ..."}'
        )
    return _StaticAuthProvider(headers)


def _get_server() -> x402ResourceServer:
    """Build and initialize the resource server once, then reuse it.

    `x402ResourceServer.initialize()` is a SYNCHRONOUS method (it performs
    blocking HTTP calls to the facilitator to discover supported
    scheme/network kinds). It must not be awaited: `await` on the None it
    returns raises TypeError, and because verify_and_settle deliberately
    catches every exception and fails closed, that TypeError would be
    swallowed into a plain "payment invalid". The visible symptom is x402
    being fully configured and yet rejecting 100% of payments with no
    diagnostic anywhere -- which is exactly what this code did before.

    The server is only cached after initialize() succeeds, so a facilitator
    that is briefly unreachable results in a retry on the next request
    rather than a permanently poisoned server object.
    """
    global _server
    with _LOCK:
        if _server is None:
            facilitator = HTTPFacilitatorClient(
                FacilitatorConfig(url=_FACILITATOR_URL, auth_provider=_auth_provider())
            )
            server = x402ResourceServer(facilitator)
            server.register(_NETWORK, ExactEvmServerScheme())
            server.initialize()
            _server = server
        return _server


def _get_requirements(price: str):
    """Cached per price -- a $0.03 payment must never satisfy a $0.10
    challenge, so each price gets its own requirements object rather than
    sharing one global cache across every route."""
    with _LOCK:
        server = _get_server()
        if price not in _requirements_cache:
            config = ResourceConfig(
                scheme="exact",
                network=_NETWORK,
                pay_to=_PAY_TO_ADDRESS,
                price=price,
            )
            _requirements_cache[price] = server.build_payment_requirements(config)
        return _requirements_cache[price]


def payment_required_body(price: Optional[str] = None) -> dict:
    """The x402 half of a 402 body, or `{}` when x402 can't actually settle.

    Returning the x402 shape unconditionally -- which is what this used to do
    -- actively misleads a paying agent on a deployment where x402 isn't
    configured: it advertises X-PAYMENT as an accepted header and then names
    `payTo: null` as the recipient. A conforming client either hard-errors or
    builds a payment to a null address, and either way the caller can't buy
    and we can't sell. If x402 isn't live here, say nothing about x402 rather
    than something false; the MPP challenges on the same 402 still give the
    caller a real way to pay.
    """
    if not is_configured():
        return {}
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": _NETWORK,
        "price": price or _PRICE,
        "payTo": _PAY_TO_ADDRESS,
        "accepted_payment_header": "X-PAYMENT",
    }


_bazaar_warned = False


def _warn_bazaar_unavailable(exc: Exception) -> None:
    """Say so, once, when x402 is live but discovery can't be built.

    The extras that back the Bazaar (jsonschema, idna, via
    x402[evm,extensions]) are easy to lose from a requirements file, and the
    handlers below swallow the resulting ImportError so that a payment
    challenge still goes out. Keeping the sale is the right trade; making it
    silent is not. Swallowed quietly, this feature can be dead in production
    forever while every test on a developer machine passes, because dev
    environments usually have those packages transitively. That is not
    hypothetical -- it is how this shipped the first time, and only a clean
    CI environment caught it.

    Logged once rather than per request: a paid endpoint under load would
    otherwise bury the logs.
    """
    global _bazaar_warned
    if _bazaar_warned:
        return
    _bazaar_warned = True
    logging.getLogger(__name__).warning(
        "x402 is configured but Bazaar discovery could not be built (%s: %s). "
        "Payments still work; this node will not be indexed by facilitators, "
        "so agents cannot find it by capability. Install x402[evm,extensions].",
        type(exc).__name__,
        exc,
    )


def bazaar_extension_for_body(
    input_example: dict,
    input_schema: dict,
    output_example: Optional[dict] = None,
    method: str = "POST",
) -> dict:
    """Bazaar discovery data for a JSON-body route, or {} when x402 is off.

    The Bazaar is x402's discovery index: facilitators catalog resources by
    reading this extension off their 402 responses, and agents shopping for a
    capability search that index. Without it a paid endpoint is reachable only
    by someone who already knows the URL, which is the opposite of the point.

    `method` is filled in here rather than left to the library. The library
    documents it as "NOT passed to this function -- inferred from the route
    key or enriched by bazaar_resource_server_extension at runtime", and this
    service uses neither: it hand-builds its 402 so that one challenge can
    carry x402, MPP and the API-key rail together. So nothing ever enriched
    it, and every 402 went out with an `info.input` that omitted `method`
    while the `schema` shipped alongside it in the same object declared
    `method` required. The Bazaar's own facilitator-side validator
    (`validate_discovery_extension`) rejects that record --
    `input: 'method' is a required property` -- so a facilitator that
    validates before cataloguing indexed nothing. The rail settled fine and
    the discovery half was silently dead, which is the exact failure this
    extension exists to prevent.

    Gated on is_configured() for the same reason every other x402 surface is:
    the index is reached through a facilitator, so with no facilitator
    configured there is nothing to be indexed by, and publishing discovery
    data for an unpayable resource would list a service that cannot take the
    payment it advertises.
    """
    if not is_configured():
        return {}
    try:
        from x402.extensions.bazaar import OutputConfig, declare_discovery_extension

        extension = declare_discovery_extension(
            input=input_example,
            input_schema=input_schema,
            body_type="json",
            output=OutputConfig(example=output_example) if output_example else None,
        )
        # setdefault, not assignment: if a future library version starts
        # emitting the method itself, the library's value wins rather than
        # being overwritten by this default.
        extension["bazaar"]["info"]["input"].setdefault("method", method)
        return extension
    except Exception as exc:
        # Discovery is an enhancement. It must never be the reason a caller
        # fails to receive a payment challenge it could otherwise act on --
        # but it must not vanish quietly either.
        _warn_bazaar_unavailable(exc)
        return {}


def bazaar_extension_for_mcp_tool(
    tool_name: str,
    description: str,
    input_schema: dict,
    example: Optional[dict] = None,
) -> dict:
    """Bazaar discovery data for a paid MCP tool, or {} when x402 is off.

    Indexed under the Bazaar's "mcp" resource type, so an agent can find the
    tool by capability rather than having to already know this server exists.
    """
    if not is_configured():
        return {}
    try:
        from x402.extensions.bazaar import (
            DeclareMcpDiscoveryConfig,
            declare_mcp_discovery_extension,
        )

        return declare_mcp_discovery_extension(
            DeclareMcpDiscoveryConfig(
                tool_name=tool_name,
                description=description,
                input_schema=input_schema,
                transport="streamable-http",
                example=example,
            )
        )
    except Exception as exc:
        _warn_bazaar_unavailable(exc)
        return {}


# CAIP-2 -> the legacy name v1 clients register their schemes under. A v1
# body naming "eip155:8453" resolves to no registered scheme and the client
# gives up with NoMatchingRequirementsError, which looks like "this client
# can't do Base" rather than "this server used the wrong vocabulary".
_V1_NETWORK_NAMES = {
    "eip155:8453": "base",
    "eip155:84532": "base-sepolia",
    "eip155:1": "ethereum",
    "eip155:137": "polygon",
    "eip155:43114": "avalanche",
}

# How long a signed authorization stays valid. The x402 spec requires the
# server to state this; clients bake it into the EIP-712 payload they sign.
_MAX_TIMEOUT_SECONDS = 300

# Names this service by in the Bazaar. `service_name` and `tags` are the
# fields an agent shopping the index by capability actually matches on, and
# the facilitator validates them (printable ASCII, <=32 chars, <=5 tags).
_SERVICE_NAME = "HubVibe Site Audits"
_SERVICE_TAGS = ["accessibility", "wcag", "seo", "security", "performance"]


def _priced_asset(price: str):
    """Resolve "$0.03" to the concrete asset, atomic amount and EIP-712 extra.

    Done locally through the scheme's own price parser rather than through the
    facilitator, so building a challenge never depends on the facilitator being
    reachable at that instant. The numbers are the same ones the facilitator
    will check the signature against, because they come from the same library.
    """
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    return ExactEvmServerScheme().parse_price(price, _NETWORK)


def accepts_entry(price: Optional[str] = None, resource_url: Optional[str] = None,
                  description: Optional[str] = None) -> Optional[dict]:
    """One spec-shaped x402 v1 `accepts[]` entry, or None when x402 is off.

    This used to return a dict of our own invention -- `protocol`, `price`,
    `pay_to`, `send_via_header` -- and that made the paid rail **unpayable by
    any conforming client**. A client hands the 402 body to the x402 library,
    which validates `accepts[]` against `PaymentRequirementsV1`; ours was
    missing four required fields (`maxAmountRequired`, `resource`,
    `maxTimeoutSeconds`, `asset`) and spelled `payTo` as `pay_to`. The client
    raised a ValidationError before producing any signature, so the failure
    never even reached the facilitator: nothing to reject, nothing logged,
    nothing to see from this side. `verify-live.sh` was green throughout,
    because it asked whether the 402 mentions x402, not whether the 402 can
    be paid.

    That is the whole reason revenue is zero and it is not a demand problem:
    any agent that ever tried to buy would have bounced in the client library.

    `network` is the legacy name here, not CAIP-2. v1 clients register their
    schemes under "base"; a v1 body naming "eip155:8453" matches nothing.
    """
    if not is_configured():
        return None
    resolved = price or _PRICE
    try:
        priced = _priced_asset(resolved)
    except Exception as exc:
        # Fail closed: an entry we cannot price correctly is an entry no
        # client can pay, and advertising it is the thing this module exists
        # to never do.
        _warn_unpayable_challenge(exc)
        return None
    network = _V1_NETWORK_NAMES.get(_NETWORK)
    if network is None:
        return None
    return {
        "scheme": "exact",
        "network": network,
        "maxAmountRequired": priced.amount,
        "resource": resource_url or "",
        "description": description or "HubVibe site audit",
        "mimeType": "application/json",
        "payTo": _PAY_TO_ADDRESS,
        "maxTimeoutSeconds": _MAX_TIMEOUT_SECONDS,
        "asset": priced.asset,
        "extra": priced.extra,
    }


_unpayable_warned = False


def _warn_unpayable_challenge(exc: Exception) -> None:
    global _unpayable_warned
    if _unpayable_warned:
        return
    _unpayable_warned = True
    logging.getLogger(__name__).error(
        "x402 is configured but a payable challenge could not be built (%s: "
        "%s). x402 will be omitted from the 402 rather than advertised in a "
        "shape no client can pay.",
        type(exc).__name__,
        exc,
    )


def payment_required_header(
    price: Optional[str] = None,
    resource_url: Optional[str] = None,
    description: Optional[str] = None,
    extensions: Optional[dict] = None,
) -> dict:
    """The x402 **v2** challenge, as the `PAYMENT-REQUIRED` header, or `{}`.

    v2 does not put the challenge in the body at all. The client checks for
    this header first and only falls back to parsing the body as v1 -- so
    without it, every v2 client is served the v1 path whether or not it wants
    it, and the v2 `extensions` slot (where the Bazaar discovery record
    actually belongs in v2) has nowhere to live.

    This is also the only place a service can name itself for the index:
    `ResourceInfo.service_name` and `.tags` are what an agent shopping the
    Bazaar by capability matches against. In the v1 body there is no field
    for either, so a v1-only node is at best an anonymous row.
    """
    if not is_configured():
        return {}
    resolved = price or _PRICE
    try:
        from x402.http.utils import encode_payment_required_header
        from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo

        priced = _priced_asset(resolved)
        challenge = PaymentRequired(
            x402Version=2,
            error="payment_required",
            resource=ResourceInfo(
                url=resource_url or "",
                description=description or "HubVibe site audit",
                mimeType="application/json",
                serviceName=_SERVICE_NAME,
                tags=list(_SERVICE_TAGS),
            ),
            accepts=[
                PaymentRequirements(
                    scheme="exact",
                    network=_NETWORK,
                    asset=priced.asset,
                    amount=priced.amount,
                    payTo=_PAY_TO_ADDRESS,
                    maxTimeoutSeconds=_MAX_TIMEOUT_SECONDS,
                    extra=priced.extra,
                )
            ],
            extensions=extensions or None,
        )
        return {"PAYMENT-REQUIRED": encode_payment_required_header(challenge)}
    except Exception as exc:
        # Same trade as the Bazaar extension: never let the richer path break
        # the challenge. A v1 body still goes out and is still payable.
        _warn_unpayable_challenge(exc)
        return {}


class PendingPayment:
    """An x402 payment that has been verified but NOT yet settled.

    x402 deliberately splits these: verification proves the payment is valid
    and the funds are committed, settlement is what actually moves them. Doing
    both up front means a caller whose audit then fails to run has paid for
    nothing -- and audits do fail routinely at volume, because the sites being
    audited go down and time out. Holding the verified payment until the audit
    has actually produced a result is what makes "you are only charged for an
    audit that ran" true for machine payers, not just for subscribers.
    """

    __slots__ = ("payload", "requirements", "price")

    def __init__(self, payload, requirements, price: str):
        self.payload = payload
        self.requirements = requirements
        self.price = price


def verify_only_sync(payment_header: str, price: Optional[str] = None):
    """Verify a payment without moving any money.

    Returns a PendingPayment handle to settle later, or None if the payment is
    invalid -- the same fail-closed contract as everything else here: any
    exception, any facilitator rejection, any missing configuration all
    resolve to None.
    """
    if not is_configured():
        return None
    resolved_price = price or _PRICE
    try:
        payload = decode_payment_signature_header(payment_header)
        requirements = _get_requirements(resolved_price)
        server = _get_server()

        async def _run():
            return await server.verify_payment(payload, requirements[0])

        if not asyncio.run(_run()).is_valid:
            return None
        return PendingPayment(payload, requirements, resolved_price)
    except Exception:
        return None


# CAIP-2 network id -> the name Stripe's crypto transaction_verification
# expects. Deliberately tiny: recording is only attempted on networks this
# map names, because guessing a network name would create a PaymentIntent
# that verifies against the wrong chain -- worse than not recording at all.
_STRIPE_NETWORK_NAMES = {"eip155:8453": "base"}

# Pinned to the same preview version app/mpp_payments.py already targets;
# the crypto payment_method surface only exists behind it.
_STRIPE_RECORD_API_VERSION = "2026-05-27.preview"

_record_disabled_logged = False


def record_settlement_in_stripe(settle_result, requirements) -> None:
    """Mirror a settled on-chain payment into Stripe as a PaymentIntent.

    This is the pattern from Stripe's own machine-payments sample: after the
    facilitator settles USDC on-chain, create a PaymentIntent in
    `transaction_verification` mode carrying the transaction hash. Stripe
    verifies the transaction against the deposit address and the payment
    shows up in the same balance, reporting, and payouts as every card
    charge. Without this, x402 revenue lands on the deposit address but the
    Stripe account reads zero -- earning and looking dead at the same time,
    which this project can no longer afford to confuse.

    Never raises, and its outcome must never influence the settle result:
    by the time this runs the money has already moved on-chain, so a
    recording failure is a bookkeeping gap to log loudly, not a payment
    failure to report to the caller. Recording is also idempotent by
    construction -- the idempotency key is the transaction hash, so a retry
    or a double call cannot double-count revenue.

    Silently does nothing when Stripe is not configured, when the network
    has no known Stripe name, or when the settled amount rounds below one
    cent -- each of those is a deployment where recording cannot be correct.
    """
    global _record_disabled_logged
    try:
        tx_hash = getattr(settle_result, "transaction", None)
        if not tx_hash or not getattr(settle_result, "success", False):
            return

        stripe_key = os.environ.get("STRIPE_SECRET_KEY")
        if not stripe_key:
            if not _record_disabled_logged:
                _record_disabled_logged = True
                logging.getLogger(__name__).warning(
                    "x402 settlement succeeded but STRIPE_SECRET_KEY is not "
                    "set, so on-chain revenue will NOT appear in Stripe. The "
                    "USDC is on the pay-to address; only the bookkeeping is "
                    "missing."
                )
            return

        network_name = _STRIPE_NETWORK_NAMES.get(_NETWORK)
        if not network_name:
            if not _record_disabled_logged:
                _record_disabled_logged = True
                logging.getLogger(__name__).warning(
                    "x402 settled on %s, which has no known Stripe "
                    "transaction_verification network name -- settlements "
                    "will not be mirrored into Stripe.",
                    _NETWORK,
                )
            return

        # requirements.amount is atomic USDC units (6 decimals):
        # $0.01 == 10_000 units. Stripe wants integer cents.
        amount_cents = round(int(requirements.amount) / 10_000)
        if amount_cents < 1:
            return

        import stripe as _stripe

        intent = _stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            confirm=True,
            payment_method_data={"type": "crypto"},
            payment_method_types=["crypto"],
            payment_method_options={
                "crypto": {
                    "mode": "transaction_verification",
                    "transaction_verification_options": {
                        "network": network_name,
                        "transaction_hash": tx_hash,
                    },
                }
            },
            idempotency_key=tx_hash,
            api_key=stripe_key,
            stripe_version=_STRIPE_RECORD_API_VERSION,
        )
        logging.getLogger(__name__).info(
            "recorded x402 settlement in Stripe: %s, %d cents, tx %s",
            getattr(intent, "id", "<no id>"),
            amount_cents,
            tx_hash,
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "x402 settlement succeeded on-chain but recording it in Stripe "
            "failed. Revenue is on the pay-to address; Stripe will not show "
            "it until this transaction hash is recorded."
        )


def settle_sync(pending) -> bool:
    """Capture a previously verified payment. Call only after delivering.

    A False here means we delivered an audit we were not paid for, which is
    the deliberately-chosen lesser evil: the alternative ordering charges for
    work that was never delivered. Callers should surface it rather than
    swallow it, so the gap stays visible instead of silent.
    """
    if pending is None:
        return False
    try:
        server = _get_server()

        async def _run():
            return await server.settle_payment(pending.payload, pending.requirements[0])

        result = asyncio.run(_run())
        if result.success:
            record_settlement_in_stripe(result, pending.requirements[0])
        return bool(result.success)
    except Exception:
        return False


async def verify_and_settle(payment_header: str, price: Optional[str] = None) -> bool:
    """Verify a signed X-PAYMENT header and settle it via the facilitator.

    `price` must match whatever price the caller was actually challenged
    with for this specific route -- defaults to the deploy-wide X402_PRICE
    for callers (like the original /audit route) that don't vary price.

    Returns True only if the facilitator confirms both verification and
    settlement succeeded. Never raises past this boundary; any failure
    mode -- malformed header, facilitator rejection, network error --
    returns False.
    """
    if not is_configured():
        return False
    try:
        payload = decode_payment_signature_header(payment_header)
        requirements = _get_requirements(price or _PRICE)
        server = _get_server()

        verify_result = await server.verify_payment(payload, requirements[0])
        if not verify_result.is_valid:
            return False

        settle_result = await server.settle_payment(payload, requirements[0])
        if settle_result.success:
            record_settlement_in_stripe(settle_result, requirements[0])
        return bool(settle_result.success)
    except Exception:
        return False


def verify_and_settle_sync(payment_header: str, price: Optional[str] = None) -> bool:
    """Sync wrapper for the FastAPI route handler, which is itself sync
    (it calls Playwright's sync API). Any failure -- including there being
    no running event loop to reuse -- fails closed, same as the async path.
    """
    try:
        return asyncio.run(verify_and_settle(payment_header, price))
    except Exception:
        return False
