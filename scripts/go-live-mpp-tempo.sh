#!/usr/bin/env bash
# Turn the MPP tempo rail on: mint a Stripe crypto deposit address, set it,
# deploy. One short line, because the alternative is three commands with an
# address copy-pasted between them on a phone.
#
# This is the Stripe rail that works at these prices. MPP has two methods and
# both are Stripe -- the protocol was co-authored by Stripe and Tempo:
#
#   stripe : Shared Payment Tokens (cards). Minimum charge 0.50 USD.
#   tempo  : USDC on Tempo. Minimum 0.01 USD, offramped by Stripe into the
#            Stripe balance.
#
# Every route here is $0.03-$0.10, so `stripe` is below its floor and `tempo`
# is the one that can actually take the money. Which makes the deposit address
# the whole job -- and it must be a STRIPE-managed one: a self-custody wallet
# would settle on-chain and lose the offramp that makes this a Stripe rail at
# all.
#
# Usage:  bash scripts/go-live-mpp-tempo.sh
#         MPP_TEMPO_RECIPIENT_ADDRESS=0x... bash scripts/go-live-mpp-tempo.sh
#
# The second form skips minting and uses an address you already have.

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
PROJECT="${PROJECT:-resolver-time}"
STRIPE_SECRET_NAME="${STRIPE_SECRET_NAME:-SECRET_STRIPE_KEY}"

# The deposit-address API is preview-only and version-pinned. An older version
# 404s the endpoint, which reads like "crypto is not enabled on this account"
# rather than "you asked the wrong API version" -- a wrong diagnosis that has
# already cost this project days once.
STRIPE_API_VERSION="${STRIPE_API_VERSION:-2026-07-29.preview}"
TEMPO_NETWORK="${TEMPO_NETWORK:-tempo}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

# --- the address -------------------------------------------------------------

ADDRESS="${MPP_TEMPO_RECIPIENT_ADDRESS:-}"

if [ -n "$ADDRESS" ]; then
  step "Using the address from the environment"
else
  step "Minting a Stripe crypto deposit address on $TEMPO_NETWORK"

  # Read the key from Secret Manager rather than having it typed or pasted:
  # a live Stripe secret in shell history is a credential leak with a long
  # tail, and this script is meant to be run from a phone.
  STRIPE_KEY=$(gcloud secrets versions access latest \
    --secret="$STRIPE_SECRET_NAME" --project="$PROJECT" 2>/dev/null)
  [ -n "$STRIPE_KEY" ] || die "could not read secret $STRIPE_SECRET_NAME. Is the name right?
        List them with:  gcloud secrets list --project=$PROJECT"

  RESPONSE=$(curl -sS -m 30 https://api.stripe.com/v1/crypto/deposit_addresses \
    -u "$STRIPE_KEY:" \
    -H "Stripe-Version: $STRIPE_API_VERSION" \
    -d "network=$TEMPO_NETWORK" 2>&1)

  ADDRESS=$(printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit()
if "error" in body:
    err = body["error"]
    print("ERROR\t%s" % (err.get("message") or err.get("type") or "unknown"))
    sys.exit()
# The address may be returned flat or nested under the network name; take
# whichever is there rather than assuming one shape of a preview API.
addr = body.get("address")
if not addr:
    for value in body.values():
        if isinstance(value, dict) and value.get("address"):
            addr = value["address"]
            break
print(addr or "")
' 2>/dev/null)

  case "$ADDRESS" in
    ERROR*)
      printf '\n'
      warn "Stripe refused: $(printf '%s' "$ADDRESS" | cut -f2-)"
      warn ""
      warn "If it says the endpoint does not exist, crypto/stablecoins are not"
      warn "enabled on this account yet -- enable them in the Dashboard first."
      warn "If it mentions the API version, override it:"
      warn "  STRIPE_API_VERSION=<newer> bash scripts/go-live-mpp-tempo.sh"
      die "no deposit address was minted. Nothing was changed."
      ;;
    "")
      printf '\n  Raw response (first 400 chars):\n    %s\n\n' "$(printf '%s' "$RESPONSE" | head -c 400)"
      die "could not read an address out of Stripe's response. Nothing was changed."
      ;;
  esac
fi

# --- the same shape gate the app and the preflight apply ---------------------
#
# Checked here so a bad value never reaches a revision at all. A truncated
# paste of exactly this kind -- 39 hex characters instead of 40 -- sat on this
# service advertising an unsettleable rail until the protocol's own reference
# client caught it.
if [ "$ADDRESS" = "0x0000000000000000000000000000000000000000" ]; then
  die "Stripe returned the zero address. That cannot receive; refusing to set it."
fi
if ! printf '%s' "$ADDRESS" | grep -qiE '^0x[0-9a-f]{40}$'; then
  die "the address is not 0x + 40 hex characters (this one has $(( ${#ADDRESS} - 2 ))):
        $ADDRESS
        Refusing to set it -- the rail would be advertised and unsettleable."
fi
ok "deposit address is well-formed: $ADDRESS"

# --- set it ------------------------------------------------------------------

step "Setting MPP_TEMPO_RECIPIENT_ADDRESS on $SERVICE"
gcloud run services update "$SERVICE" --project="$PROJECT" --region="$REGION" \
  --update-env-vars="MPP_TEMPO_RECIPIENT_ADDRESS=$ADDRESS" \
  || die "could not set the variable. Nothing was deployed."
ok "set"

# --- deploy ------------------------------------------------------------------
#
# Config alone is not a deploy: `gcloud run services update` mints a revision
# carrying the SAME container image, so a fix can be merged, the config
# correct, and the container still serving old code. repair-and-deploy.sh
# deploys source and preflights the environment first.
step "Deploying the current source"
printf '  NOTE  env vars alone do not ship code; this deploys the image too\n'
exec bash "$REPO_ROOT/scripts/repair-and-deploy.sh"
