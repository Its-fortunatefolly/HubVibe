#!/usr/bin/env bash
# Make the first real x402 payment to this node, and see whether it lands in
# the Bazaar index.
#
# WHY THIS EXISTS
#
# The Bazaar spec is explicit about how a resource gets catalogued:
#
#     "When a facilitator receives a PaymentPayload containing the `bazaar`
#      extension, it should: 1. Validate the `info` field against the
#      provided `schema`  2. Extract the discovery information"
#
# That is the ONLY ingestion path. There is no registration endpoint, no
# submit form, no crawler. `/discovery/resources` is read-only -- it lists
# what payments have already taught the facilitator about. The x402 client
# library does its half automatically (client_base._merge_extensions copies
# the server's declared extensions into the payment payload), so the chain is:
#
#     our 402 declares the extension
#       -> a paying client echoes it in the payment payload
#         -> the facilitator validates it and catalogs the resource
#           -> other agents find us in /discovery/resources
#
# Every link after the first requires a payment to actually happen. This node
# has taken zero payments, ever. So it has never been catalogued, and could
# not have been, on ANY facilitator -- swapping facilitators does not fix
# that. An unpaid resource is an uncatalogued resource by construction.
#
# Which is a deadlock: agents find us by capability only if we are indexed,
# and we are indexed only once someone pays. Nobody breaks that from the
# outside. This script breaks it from the inside, for $0.03, by being the
# first payer ourselves.
#
# It does two things nothing else has done:
#   1. Proves the settle side end to end. The handoff has said "settlement is
#      unproven until the first real agent payment" since the rail went live.
#      If settlement is broken, every agent that ever arrives bounces silently
#      and we would read it as no demand -- the single most expensive way this
#      business can be wrong.
#   2. Registers the node in the facilitator's Bazaar index, if that
#      facilitator runs one.
#
# Usage:
#     export HUBVIBE_WALLET_KEY=0x...        # funded with USDC on Base
#     bash scripts/first-paid-call.sh
#
# Optional:
#     TARGET_URL   the site to audit         (default https://example.com)
#     ROUTE        which paid route          (default /audit/wcag -- cheapest)
#     FACILITATOR  facilitator base URL      (default https://facilitator.xpay.sh)
#     BASE         the node under test       (default the live Cloud Run URL)

set -uo pipefail

BASE="${BASE:-https://hubvibe-831480473793.us-south1.run.app}"
ROUTE="${ROUTE:-/audit/wcag}"
TARGET_URL="${TARGET_URL:-https://example.com}"
FACILITATOR="${FACILITATOR:-https://facilitator.xpay.sh}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

# ---------------------------------------------------------------------------
# Preflight. Every check below is here to avoid spending money on a call that
# cannot accomplish what it is being spent for. This is the one payment that
# bootstraps discovery; burning it on a stale revision buys nothing back.
# ---------------------------------------------------------------------------

[ -n "${HUBVIBE_WALLET_KEY:-}" ] || die "HUBVIBE_WALLET_KEY is not set. Export a funded
        Base wallet private key (USDC). Nothing was attempted."

command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

step "Checking the client dependencies are installed"
if ! python3 -c 'import x402, eth_account, httpx' 2>/dev/null; then
  warn "installing the x402 client extras"
  pip install --quiet "x402[evm,extensions]" eth-account httpx \
    || die "could not install the x402 client extras"
fi
ok "x402 client is importable"

step "Reading the live 402 challenge from $BASE$ROUTE"
CHALLENGE=$(curl -sS -m 30 -X POST "$BASE$ROUTE" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"$TARGET_URL\"}" 2>/dev/null)
[ -n "$CHALLENGE" ] || die "no response from $BASE$ROUTE"

# One python pass over the challenge: it has to answer four questions, and
# reading it four times invites the four answers to disagree.
PREFLIGHT=$(printf '%s' "$CHALLENGE" | python3 -c '
import json, sys

try:
    body = json.load(sys.stdin)
except Exception:
    print("FAIL\tthe response was not JSON -- is the node up?")
    sys.exit()

accepts = body.get("accepts") or []
x402 = next((a for a in accepts if a.get("protocol") == "x402"), None)
if x402 is None:
    print("FAIL\tthe live 402 does not advertise x402, so there is no rail to pay. "
          "Advertised: %s" % ([a.get("protocol") for a in accepts] or "nothing"))
    sys.exit()

pay_to = x402.get("pay_to") or ""
if set(pay_to[2:]) == {"0"}:
    print("FAIL\tpay_to is the zero address -- USDC reverts transfers to it. "
          "Nothing would arrive.")
    sys.exit()

info = (((body.get("extensions") or {}).get("bazaar") or {}).get("info") or {}).get("input")
if not info:
    print("FAIL\tthe 402 carries no Bazaar discovery record, so this payment would "
          "settle but index nothing. Deploy first.")
    sys.exit()
if info.get("type") == "http" and not info.get("method"):
    print("FAIL\tthe Bazaar record names no HTTP method, so the facilitator will "
          "discard it on validation and the payment buys no index entry. The "
          "deployed revision predates #52 -- run scripts/repair-and-deploy.sh first.")
    sys.exit()

print("OK\t%s\t%s\t%s" % (body.get("price"), pay_to, x402.get("network")))
')

case "$PREFLIGHT" in
  FAIL*) die "$(printf '%s' "$PREFLIGHT" | cut -f2-)" ;;
esac

PRICE=$(printf '%s' "$PREFLIGHT" | cut -f2)
PAY_TO=$(printf '%s' "$PREFLIGHT" | cut -f3)
NETWORK=$(printf '%s' "$PREFLIGHT" | cut -f4)
ok "x402 advertised: $PRICE to $PAY_TO on $NETWORK"
ok "the Bazaar record on this 402 is well-formed and will survive validation"

# ---------------------------------------------------------------------------
# Baseline the index BEFORE paying, so "we appeared" is a measured change
# rather than an assumption. A facilitator with no index answers 404 here;
# that is a real answer and the script keeps going -- proving settlement is
# worth the $0.03 on its own.
# ---------------------------------------------------------------------------

step "Baselining the facilitator's Bazaar index"
BEFORE=$(curl -sS -m 30 "$FACILITATOR/discovery/resources" 2>/dev/null)
INDEX_LIVE=yes
if printf '%s' "$BEFORE" | grep -qi 'not found'; then
  INDEX_LIVE=no
  warn "$FACILITATOR serves no /discovery/resources -- it settles payments and"
  warn "runs no index. This payment will prove settlement but cannot register"
  warn "the node anywhere. To get indexed, settle through a facilitator that"
  warn "runs a Bazaar."
else
  BEFORE_COUNT=$(printf '%s' "$BEFORE" | grep -o "$PAY_TO" | wc -l | tr -d ' ')
  ok "index reachable; entries already naming our pay-to address: $BEFORE_COUNT"
fi

# ---------------------------------------------------------------------------
# The payment. Exactly one attempt -- no retry loop anywhere near a signature.
# A retried payment is a double charge, and this script exists to establish
# trust in the rail, not to spend twice proving it.
# ---------------------------------------------------------------------------

step "Paying for one real call ($PRICE)"
PAID=$(HUBVIBE_BASE_URL="$BASE" \
       HUBVIBE_MAX_PRICE_USD=0.15 \
       HUBVIBE_BUDGET_USD=0.15 \
       ROUTE="$ROUTE" \
       TARGET_URL="$TARGET_URL" \
       python3 -c '
import json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "wcag-audit-engine", "integrations"))
from hubvibe_tollbooth import HubVibeTollbooth

route = os.environ["ROUTE"].rsplit("/", 1)[-1]
booth = HubVibeTollbooth.from_env()
try:
    result = booth.audit(os.environ["TARGET_URL"], endpoint=route)
except Exception as exc:
    print("FAIL\t%s: %s" % (type(exc).__name__, exc))
    sys.exit()
print("OK\t%.4f\t%s" % (booth.spent_usd, json.dumps(result)[:400]))
' 2>&1)

case "$PAID" in
  FAIL*)
    printf '  \033[31mSTOP\033[0m  the payment did not go through:\n'
    printf '%s\n' "$PAID" | cut -f2- | sed 's/^/        /'
    printf '\n  This is the answer worth having. Settlement was never proven\n'
    printf '  before now, and an agent hitting this would have bounced in\n'
    printf '  silence -- which reads as nobody buying. Fix this before\n'
    printf '  spending any effort on demand.\n'
    exit 1
    ;;
esac

SPENT=$(printf '%s' "$PAID" | cut -f2)
ok "settled \$$SPENT and the audit returned a result"
printf '%s\n' "$PAID" | cut -f3 | sed 's/^/        /'

# ---------------------------------------------------------------------------
# Did the payment register us?
# ---------------------------------------------------------------------------

step "Re-reading the Bazaar index"
if [ "$INDEX_LIVE" = no ]; then
  warn "skipped -- this facilitator runs no index"
  printf '\n  \033[1mFIRST PAID CALL: SETTLED.\033[0m Revenue is no longer zero and the\n'
  printf '  settle path is proven. Capability discovery still needs a\n'
  printf '  facilitator that runs a Bazaar; one payment through such a\n'
  printf '  facilitator is all it then takes to get listed.\n'
  exit 0
fi

sleep 5
AFTER=$(curl -sS -m 30 "$FACILITATOR/discovery/resources" 2>/dev/null)
AFTER_COUNT=$(printf '%s' "$AFTER" | grep -o "$PAY_TO" | wc -l | tr -d ' ')

if [ "$AFTER_COUNT" -gt "${BEFORE_COUNT:-0}" ]; then
  printf '\n  \033[1;32mINDEXED.\033[0m %s entries now name our pay-to address (was %s).\n' \
    "$AFTER_COUNT" "${BEFORE_COUNT:-0}"
  printf '  An agent shopping the Bazaar by capability can now find this node.\n'
else
  warn "no new entry yet (still $AFTER_COUNT). Indexing may lag; re-check with:"
  warn "  curl -s $FACILITATOR/discovery/resources | grep -c $PAY_TO"
fi
