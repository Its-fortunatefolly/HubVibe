#!/usr/bin/env bash
# Turn the x402 machine-payment rail on, end to end, in one command.
#
#   bash scripts/go-live-x402.sh
#
# Everything the rail needs, done in order, each step skipped when it is
# already correct:
#
#   1. pay-to address  -- a Base wallet whose key you hold, supplied by hand
#                         or already on the service. Nothing is minted here:
#                         Stripe does MPP, not x402.
#   2. facilitator     -- point at a keyless one that needs no business review
#   3. deploy          -- one revision, only if something actually changed
#   4. verify          -- the live checks, including the paid path
#
# Why this exists: the two variables were being set by hand across several
# long gcloud invocations, and the failure modes are silent. Setting one
# without the other leaves x402 inert. A malformed address leaves the rail
# advertised and unpayable, which looks exactly like nobody buying. This does
# both, in the right order, and refuses to deploy a state it knows is broken.
#
# Safe to re-run. A correct deployment mints no revision.

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"

# Keyless, Base mainnet, zero fee. Chosen because it needs no business
# verification: the Coinbase CDP facilitator is gated on a review that asks
# for a DBA this business does not have, so CDP is unavailable, not pending.
FACILITATOR="${X402_FACILITATOR:-https://facilitator.xpay.sh}"
STRIPE_SECRET_NAME="${STRIPE_SECRET_NAME:-SECRET_STRIPE_KEY}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

# Well-formed addresses that must never be advertised. Shape is not ownership,
# and that distinction has already cost this project once at the zero address:
# 0x2b3b... is 0x + 40 hex, passes every gate in this repo, sat deployed on
# this service as X402_PAY_TO_ADDRESS -- and the owner does not recognise it.
# 0x32b0... is the test-suite constant, which exists to make the rail
# inspectable locally and whose key nobody holds. Without this, "already set
# and well-formed" keeps either one advertised forever.
UNAFFIRMED_ADDRESSES="
0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256
0x32b08c5e927c69877d0fcab35618c265674922bc
"

is_unaffirmed() {
  local needle candidate
  needle="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  for candidate in $UNAFFIRMED_ADDRESSES; do
    [ "$needle" = "$(printf '%s' "$candidate" | tr 'A-Z' 'a-z')" ] && return 0
  done
  return 1
}

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SVC_JSON="$(mktemp)"
trap 'rm -f "$SVC_JSON"' EXIT

step "Reading the live service"
# --format=json, never flattened: flattened pads names with alignment spaces,
# so every grep against it silently matches nothing. That shipped three times.
gcloud run services describe "$SERVICE" --region="$REGION" --format=json > "$SVC_JSON" 2>/dev/null \
  || die "cannot read $SERVICE in $REGION. Wrong project, or not authenticated?"
ok "read $SERVICE in $REGION"

read_env() {
  python3 -c '
import json, sys
name = sys.argv[1]
spec = json.load(open(sys.argv[2]))["spec"]["template"]["spec"]
for c in spec.get("containers", []):
    for e in c.get("env") or []:
        if e.get("name") == name:
            print(e["value"] if "value" in e else "__FROM_SECRET__")
            sys.exit(0)
' "$1" "$SVC_JSON" 2>/dev/null
}

PAY_TO="$(read_env X402_PAY_TO_ADDRESS)"
CUR_FACILITATOR="$(read_env X402_FACILITATOR_URL)"

step "1. Pay-to address -- where the USDC actually lands"

# An address supplied by hand wins outright, and is settled FIRST. It used to
# be applied after the mint step, which was harmless while that step could
# produce an address -- now that the step can only fail (Stripe does not do
# x402), checking it second would reject a perfectly good wallet before ever
# reading it.
if [ -n "${X402_PAY_TO_ADDRESS:-}" ]; then
  printf '%s' "$X402_PAY_TO_ADDRESS" | grep -qiE '^0x[0-9a-f]{40}$' \
    || die "X402_PAY_TO_ADDRESS from the environment is not 0x + 40 hex"
  # Supplying it by hand is not affirmation -- a paste is how it got here.
  ! is_unaffirmed "$X402_PAY_TO_ADDRESS" \
    || die "X402_PAY_TO_ADDRESS is an address nobody here holds the key to. Refusing."
  ok "using the address supplied in the environment"
  PAY_TO="$X402_PAY_TO_ADDRESS"
  SUPPLIED_BY_HAND=1
else
  SUPPLIED_BY_HAND=0
fi

NEED_ADDRESS=0
if [ "$SUPPLIED_BY_HAND" -eq 1 ]; then
  : # already settled above
elif [ "$PAY_TO" = "__FROM_SECRET__" ]; then
  warn "comes from Secret Manager, so its shape cannot be checked from here."
  warn "An EVM address is public -- keep it a plain env var so the guard applies."
elif [ -z "$PAY_TO" ]; then
  warn "not set -- x402 cannot be advertised without a destination"
  NEED_ADDRESS=1
elif [ "$PAY_TO" = "0x0000000000000000000000000000000000000000" ]; then
  # Shape-valid and unownable: USDC reverts transfers to address(0). This
  # exact value shipped once and passed every check.
  warn "is the ZERO ADDRESS -- well-formed but unownable. Replacing."
  NEED_ADDRESS=1
elif is_unaffirmed "$PAY_TO"; then
  warn "is an address NOBODY HERE HOLDS THE KEY TO. Replacing."
  NEED_ADDRESS=1
elif printf '%s' "$PAY_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
  ok "already set and well-formed (${PAY_TO:0:6}...${PAY_TO: -4})"
else
  warn "is NOT 0x + 40 hex (this one has $(( ${#PAY_TO} - 2 )) chars). Replacing."
  NEED_ADDRESS=1
fi

if [ "$NEED_ADDRESS" -eq 1 ]; then
  # This used to mint a Stripe-custodied deposit address here. It no longer
  # does, and the reason is a fact from the owner, not a preference:
  #
  #   STRIPE DOES NOT DO x402. Stripe does MPP. x402 is facilitated elsewhere.
  #
  # So the old fallback led nowhere, and it failed in the most expensive way
  # available: its error told you to "ask Stripe support to turn on machine
  # payments / x402", which is advice for a product Stripe does not sell. A
  # session following it burns days on a support thread that cannot resolve.
  # This project has already lost weeks to exactly that shape of wrong
  # diagnosis with the CDP business review.
  #
  # x402 on Base means a wallet whose key you hold, and money that stays
  # on-chain. Stripe's crypto deposit addresses are still exactly right for
  # the OTHER rail -- MPP tempo mints one and Stripe offramps it into the
  # Stripe balance. That is what scripts/go-live.sh uses them for.
  step "   x402 needs a self-custody Base address"
  warn "Stripe does not do x402 -- it does MPP. There is nothing to mint here."
  die "set a Base address whose key you hold and re-run:
          X402_PAY_TO_ADDRESS=0x... bash scripts/go-live-x402.sh
        or use the one command that does both rails, which already defaults
        to the affirmed wallet:
          bash scripts/go-live.sh"
fi

step "2. Facilitator -- the service that verifies and settles"
if [ "$CUR_FACILITATOR" = "$FACILITATOR" ]; then
  ok "already $FACILITATOR"
else
  warn "changing from ${CUR_FACILITATOR:-<unset>} to $FACILITATOR"
fi

step "3. Setting the variables"
UPDATE_ARGS=()
[ "$CUR_FACILITATOR" != "$FACILITATOR" ] && UPDATE_ARGS+=("X402_FACILITATOR_URL=$FACILITATOR")
# Written as an explicit if: `A || B && C` in bash parses left-to-right as
# `(A || B) && C`, which happens to be right here and would not survive
# someone reordering it.
if [ "$NEED_ADDRESS" -eq 1 ] || [ -n "${X402_PAY_TO_ADDRESS:-}" ]; then
  UPDATE_ARGS+=("X402_PAY_TO_ADDRESS=$PAY_TO")
fi

if [ ${#UPDATE_ARGS[@]} -eq 0 ]; then
  ok "both variables already correct"
else
  IFS=','; JOINED="${UPDATE_ARGS[*]}"; unset IFS
  gcloud run services update "$SERVICE" --region="$REGION" \
    --update-env-vars="$JOINED" \
    || die "setting the variables failed -- the running revision is untouched"
  ok "variables set"
fi

# The CDP key pair can stay mounted. Since #55, CDP credentials are used only
# against a Coinbase host and ignored anywhere else, so they no longer sign
# requests to a facilitator that cannot validate them.

# 4. Deploy the SOURCE, not just the variables.
#
# This step is why the first version of this script was wrong, and the way it
# was wrong is the one this repo keeps relearning: `services update
# --update-env-vars` mints a revision carrying the SAME container image. The
# variables change, the code does not. A service can therefore report every
# variable correct while running an image from before the fixes those
# variables are meant to activate -- and the only visible symptom was
# verify-live.sh failing checks that pass locally, which reads as a broken
# checker rather than a stale deploy.
#
# It happened here: the discovery-contract checks added in #48 failed against
# a service whose variables were all correct, because the running image
# predated #48. Green config is not a deploy.
#
# repair-and-deploy.sh already does the source deploy, with a preflight that
# refuses to deploy into a broken environment, so hand off rather than keep a
# second copy of that logic -- the copy that drifts is always the one running.
step "4. Deploying the current source"
warn "env vars alone do not ship code; this deploys the image too"
exec bash "$REPO_ROOT/scripts/repair-and-deploy.sh"
