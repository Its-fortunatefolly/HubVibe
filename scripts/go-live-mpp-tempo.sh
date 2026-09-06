#!/usr/bin/env bash
# SUPERSEDED. This script no longer does anything except point at the one
# that does.
#
# scripts/go-live.sh mints the Stripe tempo deposit address, validates it,
# sets it, and deploys -- alongside the x402 rail, in a single revision. This
# file stays so that an old handoff entry or shell history lands here and
# reads this, rather than getting "No such file" and going looking.
printf '\n  \033[31mSTOP\033[0m  scripts/go-live-mpp-tempo.sh is superseded. Run instead:\n\n'
printf '      bash scripts/go-live.sh\n\n'
printf '  That turns on every rail that can settle, in one deploy. To do only tempo:\n\n'
printf '      RAILS=tempo bash scripts/go-live.sh\n\n'
exit 1
