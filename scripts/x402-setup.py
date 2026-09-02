#!/usr/bin/env python3
"""SUPERSEDED. Points at the script that does this now.

Two things were wrong with this file and the name made both hard to see.

It was called x402-setup and its docstring argued for routing x402 through a
Stripe-custodied deposit address. The owner's fact: Stripe does not do x402.
Stripe does MPP. x402 is facilitated elsewhere and its pay-to is a Base
wallet whose key you hold. So it never had a correct use under its own name.

What it actually did -- mint a Stripe crypto deposit address -- is exactly
right for the MPP tempo rail, and scripts/go-live.sh does that inline, on the
tempo network, validated and set and deployed in one pass. A second path to
the same endpoint is a second thing to drift.

This stub stays so that an old handoff entry or shell history lands here and
reads this, rather than getting "No such file" and going looking.
"""

import sys

sys.stderr.write(
    "\n  STOP  scripts/x402-setup.py is superseded. Run instead:\n\n"
    "      bash scripts/go-live.sh\n\n"
    "  It mints the tempo deposit address itself. There is nothing to mint for\n"
    "  x402: Stripe does MPP, not x402, and the x402 pay-to is a wallet you hold.\n\n"
)
sys.exit(1)
