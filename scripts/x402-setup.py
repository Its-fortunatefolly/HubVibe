#!/usr/bin/env python3
"""Mint a Stripe-custodied crypto deposit address.

DO NOT USE THIS FOR x402. **Stripe does not do x402 -- Stripe does MPP.**
x402 has to be facilitated somewhere else entirely, and its pay-to address is
a Base wallet whose key you hold, with USDC that stays on-chain. An earlier
version of this file argued the opposite ("why route x402 through Stripe
rather than your own wallet") and named itself after x402; both were wrong,
and the second one made the first one hard to notice. The name is kept only
because `git log` is easier to follow than a rename.

What this IS still right for: **MPP tempo**, which is a Stripe rail. Stripe
mints the deposit address, custodies what lands on it, and offramps it into
the Stripe balance, so there are no keys to hold and the money reconciles
with every card charge. `scripts/go-live.sh` does this for you, on the tempo
network, as part of one deploy -- prefer it over running this by hand.

What this does NOT do: it does not make this service accept anything. The
deployment needs the recipient env var set, which is the go-live script's
job. This only obtains the address.

Stripe's crypto deposit-address API is a preview surface pinned by API
version and gated on stablecoins being enabled for the account. If this
reports that the endpoint or a parameter is unrecognised, that is the signal
that stablecoins are not enabled yet -- not a bug in this script, and not a
reason to ask Stripe about x402, which they do not offer.

Usage:
    STRIPE_SECRET_KEY=sk_live_... python scripts/x402-setup.py --network tempo
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
        default="tempo",
        help=(
            "Chain the deposit address should live on. Defaults to tempo, the "
            "MPP rail Stripe actually offers. `base` was the old default, for "
            "an x402 pay-to -- Stripe does not do x402, so that was never a "
            "destination this account could serve."
        ),
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
                "A 400/404 here almost always means stablecoins are not enabled\n"
                "on this Stripe account, or the network name differs for your\n"
                "account. That is an entitlement, not something code can work\n"
                "around: enable Stablecoins and Crypto in Payment methods\n"
                "settings, then re-run. Do NOT ask Stripe to enable x402 --\n"
                "Stripe does MPP; x402 is facilitated elsewhere.",
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
    print("This is the MPP tempo recipient. Set it on the deployment:")
    print()
    print(f"  MPP_TEMPO_RECIPIENT_ADDRESS={address}")
    print()
    print("Or let the go-live script mint, validate, set and deploy it in one:")
    print("  bash scripts/go-live.sh")
    print()
    print("NOT an x402 pay-to. Stripe does MPP; x402 is facilitated elsewhere")
    print("and settles to a Base wallet whose key you hold.")
    print()
    print("Then confirm the rail actually went live:")
    print("  bash scripts/verify-live.sh")
    print()
    print("The manifest at /.well-known/agent.json should list \"mpp-tempo\"")
    print("under payment.methods. If not, the var did not reach the container.")
    return 0
