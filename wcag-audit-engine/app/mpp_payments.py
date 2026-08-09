"""MPP (Machine Payments Protocol) support for the /audit endpoint.

Implements the "Payment" HTTP authentication scheme
(https://github.com/tempoxyz/mpp-specs, draft-httpauth-payment-00) directly
in Python. There is no official Python SDK -- only the Node `mppx` package
-- so this hand-implements the wire protocol against the published spec
(core scheme + draft-stripe-charge-00 + draft-tempo-charge-00). Validate an
actual running server against the reference implementation with:

    npx mppx@latest validate http://localhost:$PORT

Two methods are offered, matching Stripe's own MPP docs
(https://docs.stripe.com/payments/machine/mpp):

- "stripe" (draft-stripe-charge-00): fiat via a single-use Shared Payment
  Token (SPT, `spt_...`). The server creates a PaymentIntent with
  `shared_payment_granted_token=<spt>` and confirms it immediately. Stripe
  enforces single-use on the token itself at the API level, so this module
  doesn't need its own replay ledger for that guarantee -- the in-process
  `_used_credentials` set below is defense in depth, not the real backstop.
- "tempo" (draft-tempo-charge-00): USDC on the Tempo network, "push" mode
  only -- the caller broadcasts their own signed transaction and hands us
  the resulting hash. "pull" mode (server broadcasts a pre-signed
  transaction on the payer's behalf) and the zero-amount EIP-712 "proof"
  credential type are NOT implemented; a request using either fails closed
  rather than silently granting access.

Fails closed everywhere, same pattern as x402_payments.py:
- A method with missing configuration is never offered and never accepted.
- Any exception, expired challenge, broken HMAC binding, wrong method,
  amount/recipient/token mismatch, failed RPC call, or non-succeeded
  PaymentIntent all resolve to "invalid" -- there is no path where an error
  results in access being granted.
- A credential (SPT or tx hash) can only grant access once per process;
  this in-memory set is best-effort under Cloud Run's multi-instance
  scaling (same caveat as the existing rate limiter in app/main.py), which
  is exactly why Stripe's own single-use SPT enforcement -- not this set --
  is what actually protects the stripe method.

Requires, at deploy time (all optional; each method is only offered if its
own vars are fully present):

Stripe SPT (fiat):
- STRIPE_SECRET_KEY               (already required by billing.py)
- MPP_STRIPE_NETWORK_PROFILE_ID   Stripe Business Network Profile ID
- MPP_STRIPE_PRICE_CENTS          default "3" ($0.03)
- MPP_STRIPE_CURRENCY             default "usd"
- MPP_STRIPE_API_VERSION          default "2026-05-27.preview"

Tempo (crypto):
- MPP_TEMPO_RPC_URL               Tempo JSON-RPC endpoint
- MPP_TEMPO_TOKEN_ADDRESS         TIP-20 USDC contract address
- MPP_TEMPO_RECIPIENT_ADDRESS     wallet address that receives payment
- MPP_TEMPO_CHAIN_ID              default 4217
- MPP_TEMPO_PRICE_BASE_UNITS      default "30000" ($0.03 at 6 decimals)

Shared:
- MPP_REALM                       default "wcag-audit-engine"
- MPP_CHALLENGE_TTL_SECONDS       default "300"
"""

import base64
import calendar
import hashlib
import hmac
import json
import os
import time
from typing import Optional

import httpx
import stripe

_MPP_API_VERSION = os.environ.get("MPP_STRIPE_API_VERSION", "2026-05-27.preview")
# Per spec, realm SHOULD match the server's own hostname. If MPP_REALM isn't
# set, callers (main.py) pass the request's actual Host header per call
# instead -- this fallback only applies to standalone/CLI use of this module.
_REALM_FALLBACK = os.environ.get("MPP_REALM", "wcag-audit-engine")
_CHALLENGE_TTL_SECONDS = int(os.environ.get("MPP_CHALLENGE_TTL_SECONDS", "300"))

_STRIPE_NETWORK_PROFILE_ID = os.environ.get("MPP_STRIPE_NETWORK_PROFILE_ID")
_STRIPE_PRICE_CENTS = os.environ.get("MPP_STRIPE_PRICE_CENTS", "3")
_STRIPE_CURRENCY = os.environ.get("MPP_STRIPE_CURRENCY", "usd")

_TEMPO_RPC_URL = os.environ.get("MPP_TEMPO_RPC_URL")
_TEMPO_CHAIN_ID = int(os.environ.get("MPP_TEMPO_CHAIN_ID", "4217"))
_TEMPO_TOKEN_ADDRESS = os.environ.get("MPP_TEMPO_TOKEN_ADDRESS")
_TEMPO_RECIPIENT_ADDRESS = os.environ.get("MPP_TEMPO_RECIPIENT_ADDRESS")
_TEMPO_PRICE_BASE_UNITS = os.environ.get("MPP_TEMPO_PRICE_BASE_UNITS", "30000")

# keccak256("Transfer(address,address,uint256)") -- standard ERC-20/TIP-20 event topic.
_TRANSFER_EVENT_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Best-effort, single-instance replay guard -- see module docstring.
_used_credentials: set = set()


def _secret_key() -> Optional[bytes]:
    stripe_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe_key:
        return None
    return hmac.new(stripe_key.encode(), b"mpp-challenge-signing", hashlib.sha256).digest()


def stripe_configured() -> bool:
    return bool(_secret_key() and stripe.api_key and _STRIPE_NETWORK_PROFILE_ID)


def tempo_configured() -> bool:
    return bool(
        _secret_key() and _TEMPO_RPC_URL and _TEMPO_TOKEN_ADDRESS and _TEMPO_RECIPIENT_ADDRESS
    )


def is_configured() -> bool:
    return stripe_configured() or tempo_configured()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _canonical_json(obj: dict) -> str:
    # A pragmatic JCS (RFC 8785) approximation: sorted keys, no whitespace.
    # Every value we serialize here is a string, int, bool, or nested object
    # of the same shapes -- this isn't a general JCS implementation, just
    # enough to produce a byte-stable serialization of our own challenges
    # (which is all that matters, since the same code both writes and
    # re-derives the HMAC over it).
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rfc3339(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


def _parse_rfc3339(value: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _challenge_id(realm, method, intent, request_b64, expires, digest, opaque) -> str:
    secret = _secret_key()
    if secret is None:
        raise ValueError("MPP not configured -- no STRIPE_SECRET_KEY to derive a signing key from")
    parts = [realm or "", method or "", intent or "", request_b64 or "", expires or "", digest or "", opaque or ""]
    mac = hmac.new(secret, "|".join(parts).encode(), hashlib.sha256).digest()
    return _b64url_encode(mac)


def _build_challenge(realm: str, method: str, intent: str, request_obj: dict) -> dict:
    request_b64 = _b64url_encode(_canonical_json(request_obj).encode())
    expires = _rfc3339(time.time() + _CHALLENGE_TTL_SECONDS)
    challenge_id = _challenge_id(realm, method, intent, request_b64, expires, None, None)
    return {
        "id": challenge_id,
        "realm": realm,
        "method": method,
        "intent": intent,
        "request": request_b64,
        "expires": expires,
    }


def _www_authenticate_header(challenge: dict) -> str:
    parts = [
        f'id="{challenge["id"]}"',
        f'realm="{challenge["realm"]}"',
        f'method="{challenge["method"]}"',
        f'intent="{challenge["intent"]}"',
        f'expires="{challenge["expires"]}"',
        f'request="{challenge["request"]}"',
    ]
    return "Payment " + ", ".join(parts)


def www_authenticate_headers(realm: Optional[str] = None) -> list:
    """One WWW-Authenticate: Payment header per configured method, for a 402
    response -- lets the caller pick whichever method it can fulfill.

    `realm` should be the request's own Host header; per spec it SHOULD
    match the server's hostname, and binding the challenge to it means a
    challenge issued on one hostname can never be replayed against another
    deployment that happens to share the same signing secret.
    """
    realm = realm or _REALM_FALLBACK
    headers = []
    if stripe_configured():
        challenge = _build_challenge(
            realm,
            "stripe",
            "charge",
            {
                "amount": _STRIPE_PRICE_CENTS,
                "currency": _STRIPE_CURRENCY,
                "methodDetails": {
                    "networkId": _STRIPE_NETWORK_PROFILE_ID,
                    "paymentMethodTypes": ["card", "link"],
                },
            },
        )
        headers.append(_www_authenticate_header(challenge))
    if tempo_configured():
        challenge = _build_challenge(
            realm,
            "tempo",
            "charge",
            {
                "amount": _TEMPO_PRICE_BASE_UNITS,
                "currency": _TEMPO_TOKEN_ADDRESS,
                "recipient": _TEMPO_RECIPIENT_ADDRESS,
                "methodDetails": {"chainId": _TEMPO_CHAIN_ID, "supportedModes": ["push"]},
            },
        )
        headers.append(_www_authenticate_header(challenge))
    return headers


def _verify_challenge_binding(challenge: dict, expected_realm: Optional[str] = None) -> bool:
    try:
        expected_id = _challenge_id(
            challenge.get("realm"),
            challenge.get("method"),
            challenge.get("intent"),
            challenge.get("request"),
            challenge.get("expires"),
            challenge.get("digest"),
            challenge.get("opaque"),
        )
    except ValueError:
        return False
    if not hmac.compare_digest(expected_id, str(challenge.get("id", ""))):
        return False
    if challenge.get("realm") != (expected_realm or _REALM_FALLBACK):
        return False
    expiry_epoch = _parse_rfc3339(str(challenge.get("expires", "")))
    if expiry_epoch is None:
        return False
    return time.time() <= expiry_epoch


def _verify_stripe(challenge: dict, payload: dict) -> bool:
    if not stripe_configured():
        return False
    spt = payload.get("spt")
    if not spt or not isinstance(spt, str) or not spt.startswith("spt_"):
        return False
    if spt in _used_credentials:
        return False
    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        intent = stripe.PaymentIntent.create(
            amount=int(request_obj["amount"]),
            currency=request_obj["currency"],
            shared_payment_granted_token=spt,
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"challenge_id": challenge["id"]},
            idempotency_key=f'{challenge["id"]}_{spt}',
            stripe_version=_MPP_API_VERSION,
        )
    except Exception:
        return False
    if intent.get("status") != "succeeded":
        return False
    _used_credentials.add(spt)
    return True


def _tempo_rpc(method: str, params: list) -> Optional[dict]:
    resp = httpx.post(
        _TEMPO_RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        return None
    return body.get("result")


def _receipt_matches(receipt: dict, request_obj: dict) -> bool:
    token_address = str(request_obj["currency"]).lower()
    recipient = str(request_obj["recipient"]).lower()
    expected_amount = int(request_obj["amount"])
    for log in receipt.get("logs", []):
        if str(log.get("address", "")).lower() != token_address:
            continue
        topics = log.get("topics", [])
        if not topics or str(topics[0]).lower() != _TRANSFER_EVENT_TOPIC0:
            continue
        if len(topics) < 3:
            continue
        log_recipient = "0x" + str(topics[2])[-40:]
        if log_recipient.lower() != recipient:
            continue
        try:
            amount = int(str(log.get("data", "0x0")), 16)
        except ValueError:
            continue
        if amount >= expected_amount:
            return True
    return False


def _verify_tempo(challenge: dict, payload: dict) -> bool:
    if not tempo_configured():
        return False
    if payload.get("type") != "hash":
        # "transaction" (pull mode) and "proof" (zero-amount EIP-712) are
        # not implemented -- fail closed rather than guess at validity.
        return False
    tx_hash = payload.get("hash")
    if not tx_hash or not isinstance(tx_hash, str):
        return False
    if tx_hash in _used_credentials:
        return False
    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
        receipt = _tempo_rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception:
        return False
    if not receipt:
        return False
    if str(receipt.get("status")) not in ("0x1", "1"):
        return False
    if not _receipt_matches(receipt, request_obj):
        return False
    _used_credentials.add(tx_hash)
    return True


def verify_and_settle_sync(authorization_header: str, realm: Optional[str] = None) -> bool:
    """Verify + settle an `Authorization: Payment <base64url-json>` header.

    `realm` should be the request's own Host header, matching whatever was
    passed to www_authenticate_headers() when the challenge was issued.

    Returns True only once, for a genuinely valid and not-yet-used
    credential against a still-binding, unexpired challenge we issued.
    Never raises past this boundary.
    """
    try:
        decoded = json.loads(_b64url_decode(authorization_header))
        challenge = decoded["challenge"]
        payload = decoded["payload"]
        if not _verify_challenge_binding(challenge, realm):
            return False
        method = challenge.get("method")
        if method == "stripe":
            return _verify_stripe(challenge, payload)
        if method == "tempo":
            return _verify_tempo(challenge, payload)
        return False
    except Exception:
        return False
