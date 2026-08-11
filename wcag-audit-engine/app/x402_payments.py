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

_server: Optional[x402ResourceServer] = None
_requirements_cache: dict = {}

# Reentrant because _get_requirements calls _get_server while holding it.
_LOCK = threading.RLock()


def is_configured() -> bool:
    return bool(_FACILITATOR_URL and _PAY_TO_ADDRESS)


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
            facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=_FACILITATOR_URL))
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
    except Exception:
        # Discovery is an enhancement. It must never be the reason a caller
        # fails to receive a payment challenge it could otherwise act on.
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
    except Exception:
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
