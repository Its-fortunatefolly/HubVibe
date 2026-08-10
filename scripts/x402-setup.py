#!/usr/bin/env python3
"""Mint a Stripe-custodied crypto deposit address to use as X402_PAY_TO_ADDRESS.

Why route x402 through Stripe rather than your own wallet: x402 settles in
USDC on-chain, but the pay-to address does not have to be a wallet you
custody. Stripe can issue a deposit address, take custody of what lands on
it, and sweep it into your normal Stripe balance -- so x402 revenue shows up
in the same reporting, payouts, and reconciliation as every other Stripe
charge, and there are no keys for you to hold. This is the same pattern the
MPP/Tempo path in app/mpp_payments.py already uses; this script just does it
for the Base network that x402 uses.

What this does NOT do: it does not make this service accept x402. Two env
vars have to be set on the deployment for that (see the end of the output),
and x402 also needs a facilitator, which is a separate service that performs
the actual verify/settle handshake. This script only obtains the address.

Stripe's crypto deposit-address and x402 APIs are preview features that
require enablement on your account and are US-business gated. If this script
reports that the endpoint or parameter is unrecognised, that is the signal
that the feature is not enabled on your account yet -- not a bug in this
script. Ask Stripe support to enable machine payments / x402, then re-run.

Usage:
    STRIPE_SECRET_KEY=sk_live_... python scripts/x402-setup.py
    STRIPE_SECRET_KEY=sk_live_... python scripts/x402-setup.py --network base
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

STRIPE_API = "https://api.stripe.com/v1/crypto/deposit_addresses"
# Stripe's machine-payments endpoints are preview surfaces and are pinned by
# API version; this matches the version app/mpp_payments.py already targets.
DEFAULT_API_VERSION = "2026-05-27.preview"


def create_deposit_address(secret_key: str, network: str, api_version: str) -> dict:
    body = urllib.parse.urlencode({"network": network}).encode()
    request = urllib.request.Request(
        STRIPE_API,
        data=body,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": api_version,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        default="base",
        help="Chain the deposit address should live on (x402 uses base).",
    )
    parser.add_argument("--api-version", default=DEFAULT_API_VERSION)
    args = parser.parse_args()

    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    if not secret_key:
        print("STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        print("Run with: STRIPE_SECRET_KEY=sk_live_... python scripts/x402-setup.py", file=sys.stderr)
        return 2

    if secret_key.startswith("sk_test_"):
        print("Note: this is a TEST key, so the address returned is a test-mode address.\n")

    try:
        result = create_deposit_address(secret_key, args.network, args.api_version)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"Stripe returned HTTP {exc.code}:\n{detail}\n", file=sys.stderr)
        if exc.code in (400, 404):
            print(
                "A 400/404 here almost always means the crypto deposit-address\n"
                "preview is not enabled on this Stripe account, or the network\n"
                "name is different for your account. This is an account\n"
                "entitlement, not something code can work around: ask Stripe to\n"
                "enable machine payments / x402 for the account, then re-run.",
                file=sys.stderr,
            )
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach Stripe: {exc}", file=sys.stderr)
        return 1

    address = result.get("address") or result.get("id")
    print(json.dumps(result, indent=2))
    print()
    if not address:
        print("Could not find an address field in that response -- inspect the JSON above.")
        return 1

    print("=" * 70)
    print(f"Deposit address ({args.network}): {address}")
    print("=" * 70)
    print()
    print("Set BOTH of these on the deployment; x402 stays inert with only one:")
    print()
    print(f"  X402_PAY_TO_ADDRESS={address}")
    print("  X402_FACILITATOR_URL=<your facilitator's base URL>")
    print()
    print("The facilitator is a separate service that verifies and settles the")
    print("payment. This service never validates a signature itself -- see the")
    print("module docstring in app/x402_payments.py for why that split matters.")
    print()
    print("Then redeploy and confirm x402 actually went live:")
    print("  bash scripts/verify-live.sh")
    print()
    print("The manifest at /.well-known/agent.json should then list \"x402\" under")
    print("payment.methods. If it does not, the vars did not reach the container.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
