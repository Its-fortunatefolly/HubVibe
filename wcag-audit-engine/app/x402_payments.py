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
"""

import asyncio
import json
import logging
import os
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


def is_configured() -> bool:
    return bool(_FACILITATOR_URL and _PAY_TO_ADDRESS)


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


def _auth_provider():
    """The facilitator auth provider for this deployment, or None.

    A malformed X402_FACILITATOR_AUTH_HEADERS is raised rather than ignored:
    silently dropping credentials would leave x402 advertised and every
    payment rejected by the facilitator, which looks identical to "nobody is
    buying" and could go unnoticed indefinitely.
    """
    if _CDP_API_KEY_ID and _CDP_API_KEY_SECRET:
        return _CdpAuthProvider(_CDP_API_KEY_ID, _CDP_API_KEY_SECRET, _FACILITATOR_URL or "")

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
) -> dict:
    """Bazaar discovery data for a JSON-body route, or {} when x402 is off.

    The Bazaar is x402's discovery index: facilitators catalog resources by
    reading this extension off their 402 responses, and agents shopping for a
    capability search that index. Without it a paid endpoint is reachable only
    by someone who already knows the URL, which is the opposite of the point.

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

        return declare_discovery_extension(
            input=input_example,
            input_schema=input_schema,
            body_type="json",
            output=OutputConfig(example=output_example) if output_example else None,
        )
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


def accepts_entry(price: Optional[str] = None) -> Optional[dict]:
    """This method's entry for the 402's machine-readable `accepts` list, or
    None when x402 isn't configured."""
    if not is_configured():
        return None
    return {
        "protocol": "x402",
        "scheme": "exact",
        "network": _NETWORK,
        "price": price or _PRICE,
        "pay_to": _PAY_TO_ADDRESS,
        "send_via_header": "X-PAYMENT",
    }


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

        return bool(asyncio.run(_run()).success)
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
