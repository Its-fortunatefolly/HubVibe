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
#     bash scripts/first-paid-call.sh
#
# The paying wallet is read from HUBVIBE_WALLET_KEY, or from
# ~/.hubvibe-wallet-key if that variable is empty -- an exported variable does
# not survive a Cloud Shell reconnect, and the file does. Have no Base wallet?
#
#     bash scripts/first-paid-call.sh --new-wallet
#
# generates one, saves it mode 600, and prints the address to fund. It needs
# USDC only, NO ETH: x402 signs the transfer off-chain and the facilitator
# pays the gas.
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

command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH."

# ---------------------------------------------------------------------------
# Resolve the paying wallet.
#
# An exported variable does not survive a Cloud Shell reconnect, and Cloud
# Shell drops idle sessions in minutes. `export HUBVIBE_WALLET_KEY=...` then
# running this some minutes later is the normal way to arrive here with an
# empty variable and no idea why -- the shell looks identical either way.
# So the key also persists in a file, and the file is what makes this
# repeatable instead of a thing that works once.
#
# What this wallet actually needs is narrower than it sounds, and the wrong
# belief here is what stalls people: **USDC only, no ETH.** x402's exact-EVM
# scheme signs an EIP-3009 authorization off-chain -- the client never touches
# an RPC, never broadcasts, and pays no gas (verified against the library:
# its client module has no provider, no send_raw_transaction, one sign call).
# The facilitator submits the transfer and pays the gas. So a wallet holding a
# dollar of USDC on Base and zero ETH is a fully working payer.
# ---------------------------------------------------------------------------

# ${HOME:-} because set -u makes a bare $HOME fatal in an environment that
# does not set it -- cron, a bare `env -i`, some CI runners. Dying on an
# unbound variable before the wallet message prints is the least useful
# possible failure here.
WALLET_FILE="${HUBVIBE_WALLET_FILE:-${HOME:-/tmp}/.hubvibe-wallet-key}"

new_wallet() {
  local generated
  generated=$(python3 -c '
from eth_account import Account
a = Account.create()
# Normalise to 0x-prefixed. HexBytes.hex() dropped the prefix in newer
# eth-account, and an unprefixed key works here but is rejected by plenty of
# other tooling the owner may paste it into.
key = a.key.hex()
print("%s\t%s" % (key if key.startswith("0x") else "0x" + key, a.address))
' 2>/dev/null) || die "could not generate a wallet -- is eth-account installed?"
  printf '%s' "${generated%%$'\t'*}" > "$WALLET_FILE"
  chmod 600 "$WALLET_FILE"
  printf '%s' "${generated##*$'\t'}"
}

step "Checking the client dependencies are installed"
if ! python3 -c 'import x402, eth_account, httpx' 2>/dev/null; then
  warn "installing the x402 client extras"
  pip install --quiet "x402[evm,extensions]" eth-account httpx \
    || die "could not install the x402 client extras"
fi
ok "x402 client is importable"

# Explicit and never implicit: a script that quietly mints a wallet when it
# cannot find one would send the owner funding a fresh address every time the
# real key went missing.
if [ "${1:-}" = "--new-wallet" ]; then
  if [ -r "$WALLET_FILE" ] && [ -z "${HUBVIBE_FORCE_NEW_WALLET:-}" ]; then
    die "$WALLET_FILE already exists. Refusing to overwrite a key that may hold
        funds. Set HUBVIBE_FORCE_NEW_WALLET=1 to replace it."
  fi
  ADDRESS=$(new_wallet)
  printf '\n  \033[1mNew Base wallet created.\033[0m Key saved to %s (mode 600).\n\n' "$WALLET_FILE"
  printf '      address: \033[1m%s\033[0m\n\n' "$ADDRESS"
  printf '  Send it USDC on Base -- $1 is plenty for a $0.03 call. NO ETH NEEDED:\n'
  printf '  x402 signs the transfer off-chain and the facilitator pays the gas.\n\n'
  printf '  Then re-run:  bash scripts/first-paid-call.sh\n\n'
  exit 0
fi

if [ -n "${HUBVIBE_WALLET_KEY:-}" ]; then
  ok "wallet key from HUBVIBE_WALLET_KEY"
elif [ -r "$WALLET_FILE" ]; then
  HUBVIBE_WALLET_KEY=$(cat "$WALLET_FILE")
  export HUBVIBE_WALLET_KEY
  ok "wallet key from $WALLET_FILE"
else
  printf '\n  \033[31mSTOP\033[0m  No paying wallet.\n\n'
  printf '  HUBVIBE_WALLET_KEY is empty and %s does not exist.\n\n' "$WALLET_FILE"
  printf '  If you DID export it: the export was lost. Cloud Shell drops env on\n'
  printf '  reconnect, and an idle tab reconnects silently. Re-export and re-run\n'
  printf '  in the SAME shell, or better, save it once so this stops recurring:\n\n'
  printf '      printf %%s "0xYOUR_KEY" > %s && chmod 600 %s\n\n' "$WALLET_FILE" "$WALLET_FILE"
  printf '  If you do NOT have a Base wallet, make one here -- it needs USDC only,\n'
  printf '  no ETH, because the facilitator pays the gas:\n\n'
  printf '      bash scripts/first-paid-call.sh --new-wallet\n\n'
  exit 1
fi

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

# accepts[] is the x402 spec array as of #61: spec-shaped entries only, no
# `protocol` key, and payTo not pay_to. Reading the pre-#61 names here found
# nothing and reported "does not advertise x402" about a node that does.
if body.get("x402Version") != 1:
    print("FAIL\tthe 402 body does not say x402Version:1, so no v1 client will "
          "read accepts[] at all. The deployed revision predates #61 -- run "
          "scripts/repair-and-deploy.sh first.")
    sys.exit()

accepts = body.get("accepts") or []
entry = next((a for a in accepts if a.get("scheme") == "exact" and a.get("payTo")), None)
if entry is None:
    print("FAIL\tno payable x402 entry in accepts[]. Present: %s"
          % (json.dumps(accepts)[:200] or "nothing"))
    sys.exit()

missing = sorted({"maxAmountRequired", "asset", "maxTimeoutSeconds", "network"} - set(entry))
if missing:
    print("FAIL\taccepts[0] is missing %s -- a conforming client raises before "
          "signing, so this rail cannot be paid. Deploy #61." % ", ".join(missing))
    sys.exit()

pay_to = entry["payTo"]
if set(pay_to[2:]) == {"0"}:
    print("FAIL\tpayTo is the zero address -- USDC reverts transfers to it. "
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

print("OK\t%s\t%s\t%s\t%s\t%s" % (body.get("price"), pay_to, entry["network"],
                                    entry["asset"], entry["maxAmountRequired"]))
')

case "$PREFLIGHT" in
  FAIL*) die "$(printf '%s' "$PREFLIGHT" | cut -f2-)" ;;
esac

PRICE=$(printf '%s' "$PREFLIGHT" | cut -f2)
PAY_TO=$(printf '%s' "$PREFLIGHT" | cut -f3)
NETWORK=$(printf '%s' "$PREFLIGHT" | cut -f4)
ASSET=$(printf '%s' "$PREFLIGHT" | cut -f5)
AMOUNT=$(printf '%s' "$PREFLIGHT" | cut -f6)
ok "x402 advertised: $PRICE to $PAY_TO on $NETWORK"
ok "the Bazaar record on this 402 is well-formed and will survive validation"

# ---------------------------------------------------------------------------
# Does the wallet actually hold the asset this challenge asks for?
#
# Without this, an unfunded or wrongly-funded wallet fails deep inside the
# facilitator, and what comes back is a settlement error that reads like the
# rail is broken. It is not: it is an empty wallet, or USDC sent on Ethereum
# instead of Base, or the funds sitting at a different address than the key
# signs for. Those are minutes to fix and hours to diagnose from a 402.
#
# Read straight off a public Base RPC -- balanceOf(address) is selector
# 0x70a08231 -- so this asks the chain rather than trusting anything local.
# ---------------------------------------------------------------------------

step "Checking the paying wallet holds enough USDC on Base"
BAL=$(python3 -c '
import json, os, sys, urllib.request
from eth_account import Account

asset = sys.argv[1]
need  = int(sys.argv[2])
try:
    addr = Account.from_key(os.environ["HUBVIBE_WALLET_KEY"].strip()).address
except Exception as exc:
    print("FAIL|the wallet key is not a valid EVM private key (%s). It must be "
          "0x + 64 hex characters." % type(exc).__name__)
    sys.exit()

body = json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[
    {"to": asset, "data": "0x70a08231" + addr[2:].rjust(64, "0").lower()}, "latest"]}).encode()
try:
    req = urllib.request.Request(os.environ.get("BASE_RPC", "https://mainnet.base.org"),
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        result = json.load(r).get("result")
    balance = int(result, 16) if result and result != "0x" else 0
except Exception as exc:
    # Not fatal. A balance we could not read is not a balance we know to be
    # wrong, and refusing to try on an RPC hiccup would be its own dead end.
    print("SKIP|%s|could not reach the Base RPC (%s); proceeding without the check"
          % (addr, type(exc).__name__))
    sys.exit()

verdict = "OK" if balance >= need else "FAIL"
print("%s|%s|%.6f" % (verdict, addr, balance / 1_000_000))
' "$ASSET" "$AMOUNT" 2>/dev/null)

case "$BAL" in
  OK*)
    ok "$(printf '%s' "$BAL" | cut -d'|' -f2) holds \$$(printf '%s' "$BAL" | cut -d'|' -f3) USDC"
    ;;
  SKIP*)
    warn "$(printf '%s' "$BAL" | cut -d'|' -f3)"
    ;;
  FAIL\|0x*)
    WALLET_ADDR=$(printf '%s' "$BAL" | cut -d'|' -f2)
    HAVE=$(printf '%s' "$BAL" | cut -d'|' -f3)
    printf '  \033[31mSTOP\033[0m  the paying wallet is short.\n\n'
    printf '        address: \033[1m%s\033[0m\n' "$WALLET_ADDR"
    printf '        holds:   $%s USDC on Base\n' "$HAVE"
    printf '        needs:   $%s\n\n' "$(python3 -c "print('%.2f' % ($AMOUNT/1000000))")"
    printf '  Send USDC to that address ON BASE. No ETH is required -- x402 signs\n'
    printf '  off-chain and the facilitator pays the gas. USDC sent on Ethereum\n'
    printf '  mainnet or another chain will not show up here.\n\n'
    exit 1
    ;;
  *)
    die "$(printf '%s' "$BAL" | cut -d'|' -f2-)"
    ;;
esac

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
    # Capped: the client raises with the whole 402 body attached, schema and
    # all, and a screenful of JSON buries the one line that says why.
    detail = " ".join(str(exc).split())
    if len(detail) > 300:
        detail = detail[:300] + " ...[truncated]"
    print("FAIL\t%s: %s" % (type(exc).__name__, detail))
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
