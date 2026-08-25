#!/usr/bin/env bash
# Turn the x402 machine-payment rail on, end to end, in one command.
#
#   bash scripts/go-live-x402.sh
#
# Everything the rail needs, done in order, each step skipped when it is
# already correct:
#
#   1. pay-to address  -- reuse the one on the service, or mint a
#                         Stripe-custodied deposit address if there is none
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
NEED_ADDRESS=0
if [ "$PAY_TO" = "__FROM_SECRET__" ]; then
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
elif printf '%s' "$PAY_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
  ok "already set and well-formed (${PAY_TO:0:6}...${PAY_TO: -4})"
else
  warn "is NOT 0x + 40 hex (this one has $(( ${#PAY_TO} - 2 )) chars). Replacing."
  NEED_ADDRESS=1
fi

if [ "$NEED_ADDRESS" -eq 1 ]; then
  step "   Minting a Stripe-custodied deposit address"
  # Custodied by Stripe on purpose: what lands there is swept into the same
  # balance and payout as every card charge, and there are no keys to hold.
  if [ -z "${STRIPE_SECRET_KEY:-}" ]; then
    STRIPE_SECRET_KEY="$(gcloud secrets versions access latest \
      --secret="$STRIPE_SECRET_NAME" 2>/dev/null)"
  fi
  [ -n "${STRIPE_SECRET_KEY:-}" ] \
    || die "no Stripe key: not in the environment and $STRIPE_SECRET_NAME is unreadable"

  MINTED="$(STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY" \
    python3 "$REPO_ROOT/scripts/x402-setup.py" --network base 2>&1)"
  PAY_TO="$(printf '%s' "$MINTED" | grep -oiE '0x[0-9a-f]{40}' | head -1)"

  if [ -z "$PAY_TO" ]; then
    printf '%s\n' "$MINTED" | sed 's/^/      /'
    echo
    die "could not mint an address. If the output above says the endpoint or a
        parameter is unrecognised, Stripe has not enabled crypto deposit
        addresses / x402 on this account yet -- ask Stripe support to turn on
        machine payments. Until then, set X402_PAY_TO_ADDRESS to a Base
        address from any self-custodial EVM wallet and re-run:
          X402_PAY_TO_ADDRESS=0x... bash scripts/go-live-x402.sh"
  fi
  ok "minted ${PAY_TO:0:6}...${PAY_TO: -4}"
fi

# An override always wins, so a self-custodial address can be supplied by hand.
if [ -n "${X402_PAY_TO_ADDRESS:-}" ]; then
  printf '%s' "$X402_PAY_TO_ADDRESS" | grep -qiE '^0x[0-9a-f]{40}$' \
    || die "X402_PAY_TO_ADDRESS from the environment is not 0x + 40 hex"
  PAY_TO="$X402_PAY_TO_ADDRESS"
  ok "using the address supplied in the environment"
fi

step "2. Facilitator -- the service that verifies and settles"
if [ "$CUR_FACILITATOR" = "$FACILITATOR" ]; then
  ok "already $FACILITATOR"
else
  warn "changing from ${CUR_FACILITATOR:-<unset>} to $FACILITATOR"
fi

step "3. Applying"
UPDATE_ARGS=()
[ "$CUR_FACILITATOR" != "$FACILITATOR" ] && UPDATE_ARGS+=("X402_FACILITATOR_URL=$FACILITATOR")
# Written as an explicit if: `A || B && C` in bash parses left-to-right as
# `(A || B) && C`, which happens to be right here and would not survive
# someone reordering it.
if [ "$NEED_ADDRESS" -eq 1 ] || [ -n "${X402_PAY_TO_ADDRESS:-}" ]; then
  UPDATE_ARGS+=("X402_PAY_TO_ADDRESS=$PAY_TO")
fi

if [ ${#UPDATE_ARGS[@]} -eq 0 ]; then
  ok "nothing to change -- no revision minted"
else
  IFS=','; JOINED="${UPDATE_ARGS[*]}"; unset IFS
  gcloud run services update "$SERVICE" --region="$REGION" \
    --update-env-vars="$JOINED" \
    || die "update failed -- the running revision is untouched"
  ok "deployed"
fi

# The CDP key pair can stay mounted. Since the fix in #55, CDP credentials are
# used only against a Coinbase host and ignored anywhere else, so they no
# longer sign requests to a facilitator that cannot validate them.
step "4. Verifying against the live service"
bash "$REPO_ROOT/scripts/verify-live.sh"
