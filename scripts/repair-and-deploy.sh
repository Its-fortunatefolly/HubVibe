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
# Read once as JSON and reuse it. `flattened` pads names with alignment
# spaces, so `grep 'name: STRIPE_SECRET_KEY'` never matched -- CURRENT_SECRET
# came back empty on every run, the script concluded the repair was always
# needed, and each invocation minted a pointless revision. The service reached
# revision 62 this way, and the docstring's promise that re-running "does not
# create pointless revisions" was quietly false.
SVC_JSON=/tmp/hv_svc.json
gcloud run services describe "$SERVICE" --region="$REGION" \
  --format=json > "$SVC_JSON" 2>/dev/null
[ -s "$SVC_JSON" ] || die "could not read service $SERVICE in $REGION"

# env_secret <ENV_VAR> -> the secret backing it, or empty.
env_secret() {
  python3 -c '
import json, sys
try:
    svc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == sys.argv[2]:
        ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
        if ref.get("name"):
            print(ref["name"])
        break
' "$SVC_JSON" "$1" 2>/dev/null
}

CURRENT_SECRET=$(env_secret STRIPE_SECRET_KEY)

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
  if grep -q "$host" "$SVC_JSON"; then
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
  # The service just changed; the cached JSON is stale for preflight.
  gcloud run services describe "$SERVICE" --region="$REGION" \
    --format=json > "$SVC_JSON" 2>/dev/null
else
  step "Configuration is already correct -- nothing to repair"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- preflight ----------------------------------------------------------
#
# Everything below checks live infrastructure that the code cannot check for
# itself, and that has silently been wrong before. Each of these cost real
# money or real downtime, and none of them were caught by tests, by a deploy,
# or by verify-live.sh:
#
#   * no Firestore database existed for the project, so every authenticated
#     call 500'd -- for an unknown length of time, while the checker showed
#     28/28 passing
#   * a secret held a hand-written test string where a Stripe key belonged,
#     which reads back fine and fails at the API instead of at startup
#   * min-instances=1 on a browser-sized container burned ~$137/month against
#     approximately zero paid traffic
#   * a malformed pay-to address would make the service advertise an x402
#     rail that can never settle
#
# The pattern behind all four: nothing verified the deployed environment, so
# drift accumulated invisibly and only surfaced by accident. They are checked
# here, before the deploy, because a deploy into a broken environment produces
# a healthy-looking revision that cannot take money.

step "Preflight: checking the live environment"

PREFLIGHT_FAILED=0

# 1. Firestore. billing.lookup_key hits it on every keyed request.
if gcloud firestore databases describe --database='(default)' \
     --format='value(name)' >/dev/null 2>&1; then
  ok "Firestore (default) database exists"
else
  warn "no Firestore (default) database. Every API-key call will fail to"
  warn "authenticate, and checkout cannot store issued keys. Create it with:"
  warn "  gcloud firestore databases create --location=$REGION"
  PREFLIGHT_FAILED=1
fi

# 2. Secrets: readable AND the right shape. Delegated to repair-secrets.sh in
#    report mode so there is one implementation of "is this secret sane".
if [ -x "$SCRIPT_DIR/repair-secrets.sh" ] || [ -f "$SCRIPT_DIR/repair-secrets.sh" ]; then
  if bash "$SCRIPT_DIR/repair-secrets.sh" 2>/dev/null | grep -qE 'NOT READABLE|WRONG SHAPE'; then
    warn "one or more secrets are unreadable or hold the wrong kind of value."
    warn "Run:  bash scripts/repair-secrets.sh          (to see what)"
    warn "Then: bash scripts/repair-secrets.sh --apply  (to fix it)"
    PREFLIGHT_FAILED=1
  else
    ok "every referenced secret is readable and correctly shaped"
  fi
fi

# 3. The x402 pay-to address. An EVM address is 0x plus exactly 40 hex
#    characters. One character short still looks right at a glance, and the
#    service would advertise a crypto rail while every settlement fails --
#    indistinguishable, from outside, from nobody buying.
#    Parsed out of JSON rather than grepped from `flattened` output. That
#    format pads names with alignment spaces, so a `grep 'name: X'` pattern
#    matches nothing and the check silently skips -- which is worse than not
#    having the check at all, because the output then looks like it passed.
PAY_TO=$(python3 -c '
import json, sys
try:
    svc = json.load(open("/tmp/hv_svc.json"))
except Exception:
    sys.exit()
spec = svc.get("spec", {}).get("template", {}).get("spec", {})
container = (spec.get("containers") or [{}])[0]
for entry in container.get("env") or []:
    if entry.get("name") == "X402_PAY_TO_ADDRESS":
        # A secret-backed value cannot be read here. Say so, rather than
        # reporting a pass that was never actually checked.
        print(entry["value"] if "value" in entry else "__FROM_SECRET__")
        break
' 2>/dev/null)

if grep -q 'X402_FACILITATOR_URL' /tmp/hv_svc.json 2>/dev/null; then
  HAS_FACILITATOR=1
else
  HAS_FACILITATOR=0
fi

if [ "$PAY_TO" = "__FROM_SECRET__" ]; then
  warn "X402_PAY_TO_ADDRESS comes from Secret Manager, so its shape was NOT"
  warn "checked here. Verify by hand that it is 0x + 40 hex characters."
elif [ -n "$PAY_TO" ]; then
  if [ "$PAY_TO" = "0x0000000000000000000000000000000000000000" ]; then
    # Shape-valid, unownable. This exact value shipped once: it passes the
    # 40-hex check below, the app advertised x402 as live, and USDC's
    # contract reverts transfers to address(0) -- no payment could ever
    # arrive, and from our side that is indistinguishable from no demand.
    warn "X402_PAY_TO_ADDRESS is the ZERO ADDRESS (0x + 40 zeros). It is"
    warn "well-formed but unownable: USDC transfers to address(0) revert."
    warn "Set a real recipient address you control."
    PREFLIGHT_FAILED=1
  elif printf '%s' "$PAY_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
    ok "X402_PAY_TO_ADDRESS is a well-formed EVM address"
  else
    warn "X402_PAY_TO_ADDRESS is NOT a valid EVM address (needs 0x + exactly"
    warn "40 hex chars; this one has $(( ${#PAY_TO} - 2 ))). Every x402"
    warn "settlement would fail while the rail is advertised as live."
    PREFLIGHT_FAILED=1
  fi
elif [ "$HAS_FACILITATOR" -eq 1 ]; then
  warn "an x402 facilitator is configured but X402_PAY_TO_ADDRESS is not set."
  warn "The service would advertise a crypto rail with no destination."
  PREFLIGHT_FAILED=1
else
  ok "x402 is not configured (no rail advertised, nothing to check)"
fi

# 4. Idle billing. Not fatal, but it is pure loss at low volume and nothing
#    else reports it.
MIN_SCALE=$(gcloud run services describe "$SERVICE" --region="$REGION" \
  --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])' 2>/dev/null)
if [ -n "$MIN_SCALE" ] && [ "$MIN_SCALE" -gt 0 ] 2>/dev/null; then
  warn "min-instances=$MIN_SCALE: you are paying to keep an instance warm"
  warn "24/7. On a browser-sized container that is real money against zero"
  warn "traffic. Set to 0 with: gcloud run services update $SERVICE --region=$REGION --min-instances=0"
fi

if [ "$PREFLIGHT_FAILED" -ne 0 ]; then
  die "preflight found problems above. Nothing was deployed and the running
        revision is untouched. Fix them and re-run -- deploying into a broken
        environment produces a revision that looks healthy but cannot take money."
fi
ok "preflight passed"

step "Deploying the current source"
gcloud run deploy "$SERVICE" --source="$SOURCE_DIR" --region="$REGION" \
  || die "deploy failed. The previous revision keeps serving; fix the error above and re-run."
ok "deployed"

# SKIP_VERIFY exists so a deploy and its verification can be run separately --
# verify-live.sh makes real network calls with retries, which is right after a
# deploy but wrong when something else is driving this script.
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  step "Skipping live verification (SKIP_VERIFY=1)"
  echo "  Run it yourself with: bash scripts/verify-live.sh"
else
  step "Verifying the live service"
  bash "$SCRIPT_DIR/verify-live.sh"
fi
