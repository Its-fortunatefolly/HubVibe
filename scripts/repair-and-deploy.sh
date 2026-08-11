#!/usr/bin/env bash
# Repair the Cloud Run config, deploy, and verify -- in one short command.
#
# This exists because the repair is three long gcloud invocations, and long
# commands pasted into a phone terminal land on a line that already has text
# in it and run together into garbage. `bash scripts/repair-and-deploy.sh` is
# short enough to type by hand, and does the steps in an order where a failure
# stops before it can make things worse.
#
# Safe to re-run. Every step checks the current state first and skips itself
# when there is nothing to do, so running it twice does not create pointless
# revisions or undo the first run.
#
# Usage:  bash scripts/repair-and-deploy.sh
#         STRIPE_SECRET_NAME=Other-Secret bash scripts/repair-and-deploy.sh

set -uo pipefail

SERVICE="${SERVICE:-hubvibe}"
REGION="${REGION:-us-south1}"
SOURCE_DIR="${SOURCE_DIR:-wcag-audit-engine}"

# The Secret Manager secret holding the Stripe secret key. A revision was once
# deployed pointing at a secret name that does not exist, which does not break
# the running instance but makes every future deploy fail at revision
# creation -- and would fail the next cold start too, because Cloud Run
# re-resolves secrets whenever an instance boots.
STRIPE_SECRET_NAME="${STRIPE_SECRET_NAME:-SECRET_STRIPE_KEY}"

# Placeholder values that must never reach a live revision: a facilitator URL
# that does not resolve would make the service advertise an x402 rail and
# Bazaar discovery it cannot actually settle.
PLACEHOLDER_HOSTS="your-facilitator.example"

step()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()    { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn()  { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()   { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

command -v gcloud >/dev/null 2>&1 || die "gcloud is not on PATH. Run this in Cloud Shell."

step "Checking the Stripe secret exists before pointing anything at it"
if gcloud secrets describe "$STRIPE_SECRET_NAME" >/dev/null 2>&1; then
  ok "secret $STRIPE_SECRET_NAME exists"
else
  printf '\n  Available secrets:\n'
  gcloud secrets list --format='value(name)' 2>/dev/null | sed 's/^/    /'
  die "no secret named $STRIPE_SECRET_NAME. Re-run as:
        STRIPE_SECRET_NAME=<one of the above> bash scripts/repair-and-deploy.sh"
fi

step "Reading the service's current configuration"
SPEC=$(gcloud run services describe "$SERVICE" --region="$REGION" \
  --format="flattened(spec.template.spec.containers[0].env)" 2>/dev/null)
[ -n "$SPEC" ] || die "could not read service $SERVICE in $REGION"

CURRENT_SECRET=$(printf '%s\n' "$SPEC" \
  | grep -A5 'name: STRIPE_SECRET_KEY' \
  | grep 'secretKeyRef.name:' | head -1 | awk '{print $2}')

if [ -n "$CURRENT_SECRET" ]; then
  echo "  STRIPE_SECRET_KEY currently references: $CURRENT_SECRET"
else
  echo "  STRIPE_SECRET_KEY is not currently a Secret Manager reference"
fi

# Build the repair as a single revision, so a half-applied fix is impossible.
UPDATE_ARGS=()

if [ "$CURRENT_SECRET" != "$STRIPE_SECRET_NAME" ]; then
  UPDATE_ARGS+=("--update-secrets=STRIPE_SECRET_KEY=${STRIPE_SECRET_NAME}:latest")
  warn "will repoint STRIPE_SECRET_KEY at $STRIPE_SECRET_NAME"
else
  ok "STRIPE_SECRET_KEY already points at $STRIPE_SECRET_NAME"
fi

STALE_VARS=""
for host in $PLACEHOLDER_HOSTS; do
  if printf '%s\n' "$SPEC" | grep -q "$host"; then
    STALE_VARS="X402_FACILITATOR_URL,X402_PAY_TO_ADDRESS"
    warn "found placeholder $host -- will remove the x402 variables"
  fi
done
[ -n "$STALE_VARS" ] && UPDATE_ARGS+=("--remove-env-vars=$STALE_VARS")

if [ ${#UPDATE_ARGS[@]} -gt 0 ]; then
  step "Applying the configuration repair"
  gcloud run services update "$SERVICE" --region="$REGION" "${UPDATE_ARGS[@]}" \
    || die "config repair failed -- nothing was deployed, the running revision is untouched"
  ok "configuration repaired"
else
  step "Configuration is already correct -- nothing to repair"
fi

step "Deploying the current source"
gcloud run deploy "$SERVICE" --source="$SOURCE_DIR" --region="$REGION" \
  || die "deploy failed. The previous revision keeps serving; fix the error above and re-run."
ok "deployed"

step "Verifying the live service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$SCRIPT_DIR/verify-live.sh"
