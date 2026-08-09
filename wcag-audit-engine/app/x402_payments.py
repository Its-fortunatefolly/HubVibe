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
_requirements = None
_initialized = False


def is_configured() -> bool:
    return bool(_FACILITATOR_URL and _PAY_TO_ADDRESS)


def _get_server() -> x402ResourceServer:
    global _server
    if _server is None:
        facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=_FACILITATOR_URL))
        server = x402ResourceServer(facilitator)
        server.register(_NETWORK, ExactEvmServerScheme())
        _server = server
    return _server


async def _get_requirements():
    global _requirements, _initialized
    server = _get_server()
    if not _initialized:
        await server.initialize()
        _initialized = True
    if _requirements is None:
        config = ResourceConfig(
            scheme="exact",
            network=_NETWORK,
            pay_to=_PAY_TO_ADDRESS,
            price=_PRICE,
        )
        _requirements = server.build_payment_requirements(config)
    return _requirements


def payment_required_body() -> dict:
    """JSON body for a 402 response: what a caller needs to construct a
    valid payment, or an X-API-Key if they'd rather use Stripe billing."""
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": _NETWORK,
        "price": _PRICE,
        "payTo": _PAY_TO_ADDRESS,
        "accepted_payment_header": "X-PAYMENT",
        "alternative": "X-API-Key header (Stripe-based billing) is also accepted",
    }


async def verify_and_settle(payment_header: str) -> bool:
    """Verify a signed X-PAYMENT header and settle it via the facilitator.

    Returns True only if the facilitator confirms both verification and
    settlement succeeded. Never raises past this boundary; any failure
    mode -- malformed header, facilitator rejection, network error --
    returns False.
    """
    if not is_configured():
        return False
    try:
        payload = decode_payment_signature_header(payment_header)
        requirements = await _get_requirements()
        server = _get_server()

        verify_result = await server.verify_payment(payload, requirements[0])
        if not verify_result.is_valid:
            return False

        settle_result = await server.settle_payment(payload, requirements[0])
        return bool(settle_result.success)
    except Exception:
        return False


def verify_and_settle_sync(payment_header: str) -> bool:
    """Sync wrapper for the FastAPI route handler, which is itself sync
    (it calls Playwright's sync API). Any failure -- including there being
    no running event loop to reuse -- fails closed, same as the async path.
    """
    try:
        return asyncio.run(verify_and_settle(payment_header))
    except Exception:
        return False
