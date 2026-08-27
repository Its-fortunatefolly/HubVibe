#!/usr/bin/env bash
# Verify a deployed HubVibe node end to end, from outside.
#
# Every check here hits the real HTTPS URL, because "the code is on main" and
# "the endpoint answers correctly in production" have repeatedly turned out to
# be different things on this service -- a route that passed every local test
# still 500'd in the container because its data file was never COPYed into the
# image. Green tests do not prove a deploy.
#
# Usage:  bash scripts/verify-live.sh [BASE_URL]
# Exit:   0 if everything expected is reachable and correct, 1 otherwise.

set -uo pipefail

BASE="${1:-https://hubvibe-831480473793.us-south1.run.app}"
FAILURES=0
PASSES=0

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASSES=$((PASSES + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

# expect_status <method> <path> <expected> [description]
#
# Retries before failing. This script is usually run seconds after a deploy,
# which is exactly when Cloud Run is still migrating traffic to the new
# revision -- the first request or two can land mid-cutover and come back 404
# or 503 from the frontend even though the route is fine. A checker that
# reports a scary false failure in its most common usage window is a checker
# people learn to ignore, so a result only counts as a failure if it persists.
expect_status() {
  local method="$1" path="$2" expected="$3" desc="${4:-$path}"
  local code attempt

  for attempt in 1 2 3; do
    if [ "$method" = "POST" ]; then
      code=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$BASE$path" \
        -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
    else
      code=$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "$BASE$path" 2>/dev/null)
    fi

    if [ "$code" = "$expected" ]; then
      if [ "$attempt" -eq 1 ]; then
        pass "$desc -> $code"
      else
        pass "$desc -> $code (after $attempt attempts; first was transient)"
      fi
      return
    fi
    [ "$attempt" -lt 3 ] && sleep 3
  done

  fail "$desc -> got $code, expected $expected (persisted over 3 attempts)"
}

echo
echo "HubVibe live verification: $BASE"
echo

echo "Service up"
# /health, not /healthz: Cloud Run's frontend reserves /healthz and answers
# it with its own 404 before the request reaches the container.
expect_status GET /health 200 "GET /health"
expect_status GET / 200 "GET / (landing page)"

echo
echo "Discovery surface (how agents find and price this node)"
expect_status GET /.well-known/agent.json 200 "GET /.well-known/agent.json"
expect_status GET /llms.txt 200 "GET /llms.txt"
expect_status GET /mcp.json 200 "GET /mcp.json"
expect_status GET /openapi.json 200 "GET /openapi.json"
expect_status GET /docs 200 "GET /docs"
expect_status GET /robots.txt 200 "GET /robots.txt"
expect_status GET /sitemap.xml 200 "GET /sitemap.xml"

echo
echo "Link-preview assets (a link with no card is a link nobody clicks)"
expect_status GET /favicon.svg 200 "GET /favicon.svg"
expect_status GET /og-image.png 200 "GET /og-image.png"

echo
echo "Paid routes fail closed (402 = route exists AND demands payment)"
for route in /audit /audit/wcag /audit/seo /audit/security /audit/performance; do
  expect_status POST "$route" 402 "POST $route"
done
expect_status POST /audit/bundle 402 "POST /audit/bundle"

echo
echo "402 challenge is machine-actionable"
CHALLENGE=$(curl -sS -m 30 -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
# Headers separately: the v2 challenge is not in the body at all.
CHALLENGE_HEADERS=$(curl -sS -m 30 -D - -o /dev/null -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)

if printf '%s' "$CHALLENGE" | grep -q '"price_usd"'; then
  pass "402 body carries price_usd"
else
  fail "402 body missing price_usd -- agents cannot budget the call"
fi

if printf '%s' "$CHALLENGE" | grep -q '"accepts"'; then
  pass "402 body carries an accepts[] list"
else
  fail "402 body missing accepts[] -- agents cannot pick a payment rail"
fi

# Carrying an accepts[] and carrying a PAYABLE accepts[] are different facts,
# and for as long as this rail has been advertised only the first was true.
# `accepts[]` held a shape of our own invention -- `protocol`/`price`/`pay_to`
# -- missing four fields PaymentRequirementsV1 requires. A client validates
# EVERY entry and raises before signing, so the failure never reached the
# facilitator: nothing rejected, nothing logged, and from this side identical
# to nobody wanting to buy. Every check above stayed green throughout.
#
# Field names rather than the x402 library, because this runs in Cloud Shell
# against the deployed node where the library is not installed. The unit tests
# drive the real client; this catches the shape regressing in production.
PAYABLE=$(printf '%s' "$CHALLENGE" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    print("FAIL|the 402 body is not JSON"); sys.exit()

if body.get("x402Version") != 1:
    print("FAIL|402 body does not say x402Version:1 -- a v1 client will not read "
          "accepts[] at all"); sys.exit()

required = {"scheme", "network", "maxAmountRequired", "resource",
            "maxTimeoutSeconds", "asset", "payTo"}
accepts = body.get("accepts") or []
if not accepts:
    print("FAIL|accepts[] is empty -- no rail an x402 client can pay"); sys.exit()

for i, entry in enumerate(accepts):
    missing = sorted(required - set(entry))
    if missing:
        print("FAIL|accepts[%d] is missing %s -- a conforming client raises a "
              "ValidationError before signing, so this rail is advertised and "
              "unpayable" % (i, ", ".join(missing)))
        sys.exit()
    if entry.get("protocol") or entry.get("price"):
        print("FAIL|accepts[%d] carries non-spec keys -- a non-x402 rail in "
              "accepts[] fails validation for the whole challenge" % i)
        sys.exit()
print("OK|accepts[] is spec-shaped: %d payable x402 entr%s"
      % (len(accepts), "y" if len(accepts) == 1 else "ies"))
')
case "$PAYABLE" in
  OK*)   pass "$(printf '%s' "$PAYABLE" | cut -d'|' -f2)" ;;
  *)     fail "$(printf '%s' "$PAYABLE" | cut -d'|' -f2)" ;;
esac

# The v2 half. A v2 client reads this header FIRST and only falls back to the
# body, so a node without it serves every modern client the legacy path -- and
# has nowhere to put the v2 extensions slot or the service name the Bazaar
# indexes by.
if printf '%s' "$CHALLENGE_HEADERS" | grep -qi '^payment-required:'; then
  pass "402 carries the v2 PAYMENT-REQUIRED challenge header"
else
  fail "402 has no PAYMENT-REQUIRED header -- v2 clients fall back to v1, and the service cannot name itself for the Bazaar index"
fi

# A null payTo is worse than no x402 at all: it tells a paying agent to send
# funds nowhere. Absent is correct when x402 is not configured.
if printf '%s' "$CHALLENGE" | grep -q '"payTo": *null'; then
  fail "402 advertises x402 with payTo:null -- agents would pay nowhere"
else
  pass "402 does not advertise an unpayable x402 rail"
fi

# accepts[] and the manifest's payment.methods describe the same fact, and
# they used to disagree: the manifest listed stripe_api_key while accepts[]
# -- the array an agent actually iterates -- omitted it, so a CI pipeline
# holding a pre-funded key could not learn from the challenge that its key
# was spendable here.
MANIFEST=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null)
if printf '%s' "$MANIFEST" | grep -q 'stripe_api_key'; then
  if printf '%s' "$CHALLENGE" | grep -q '"api_key"'; then
    pass "402 accepts[] offers the API-key rail the manifest advertises"
  else
    fail "manifest lists stripe_api_key but the 402's accepts[] omits it -- an agent reading the challenge cannot find the rail"
  fi
else
  pass "no API-key rail advertised anywhere (consistent: Stripe is not configured)"
fi

if curl -sS -m 30 -D - -o /dev/null -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null \
  | grep -qi '^www-authenticate:.*Payment'; then
  pass "MPP WWW-Authenticate: Payment challenge present"
else
  fail "no MPP WWW-Authenticate challenge -- no live machine payment rail"
fi

echo
echo "MCP endpoint (what the official registry lists as a remote server)"
MCP_INIT=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{}}}' 2>/dev/null)
if printf '%s' "$MCP_INIT" | grep -q '"serverInfo"'; then
  pass "MCP initialize handshake responds"
else
  fail "MCP initialize did not respond with serverInfo"
fi
# 2026-07-28 is a modern-only version; returning it here makes every real
# client refuse the connection, so assert we never do.
if printf '%s' "$MCP_INIT" | grep -q '"protocolVersion": *"2026'; then
  fail "MCP returned a non-handshake protocol version -- clients will refuse"
else
  pass "MCP returned a usable handshake protocol version"
fi
MCP_TOOLS=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' 2>/dev/null)
if printf '%s' "$MCP_TOOLS" | grep -q 'audit_bundle'; then
  pass "MCP tools/list advertises the audit tools"
else
  fail "MCP tools/list did not return the audit tools"
fi

# Tool definition quality, checked against the deployed container rather
# than the repo. An agent decides whether to spend money here from the tool
# definition alone: without an outputSchema it cannot tell, before paying,
# whether the response is a shape its pipeline can consume.
if printf '%s' "$MCP_TOOLS" | python3 -c '
import json, sys
try:
    tools = json.load(sys.stdin)["result"]["tools"]
except Exception:
    sys.exit(1)
if not tools:
    sys.exit(1)
for tool in tools:
    if not tool.get("outputSchema") or not tool.get("annotations"):
        sys.exit(1)
    schema = tool.get("inputSchema") or {}
    if not (schema.get("required") or schema.get("anyOf")):
        sys.exit(1)
sys.exit(0)
' 2>/dev/null; then
  pass "every MCP tool declares input constraints, an outputSchema and annotations"
else
  fail "MCP tools are under-specified -- agents drop connections rather than guess a contract"
fi

# /mcp.json is what the registry points at; /mcp is what a client calls.
# Two copies of a schema drift, and the one that drifts is the one agents read.
if python3 -c '
import json, sys, urllib.request
base = sys.argv[1]
try:
    with urllib.request.urlopen(base + "/mcp.json", timeout=30) as handle:
        manifest = {t["name"]: t for t in json.load(handle)["tools"]}
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as handle:
        live = {t["name"]: t for t in json.load(handle)["result"]["tools"]}
except Exception:
    sys.exit(1)
if set(manifest) != set(live):
    sys.exit(1)
for name, tool in live.items():
    for field in ("inputSchema", "outputSchema", "annotations", "title"):
        if manifest[name].get(field) != tool.get(field):
            sys.exit(1)
sys.exit(0)
' "$BASE" 2>/dev/null; then
  pass "/mcp.json and /mcp advertise identical tool contracts"
else
  fail "/mcp.json has drifted from /mcp -- the registry points agents at a stale contract"
fi

# The MCP paywall has to be readable by a machine. It used to stringify the
# 402 into the middle of an English sentence, so the price could only be
# recovered by scraping prose -- useless to the agents that would pay it.
MCP_PAY=$(curl -sS -m 30 -X POST "$BASE/mcp" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"audit_bundle","arguments":{"url":"https://example.com"}}}' 2>/dev/null)
if printf '%s' "$MCP_PAY" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    text = body["result"]["content"][0]["text"]
    challenge = json.loads(text)
except Exception:
    sys.exit(1)
sys.exit(0 if challenge.get("price_usd") == 0.10 else 1)
' 2>/dev/null; then
  pass "MCP paywall parses as JSON and quotes \$0.10 for the bundle"
else
  fail "MCP paywall is not machine-parseable -- an agent cannot read the price"
fi

echo
echo "Human plans: the manifest must only offer what checkout can sell"
PLANS=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null)
if printf '%s' "$PLANS" | grep -q 'included_calls_per_month'; then
  fail "manifest still advertises the retired included-calls plan"
else
  pass "manifest does not advertise the retired plan"
fi
# No f-strings here: the quoting needed to reach into a dict inside one is a
# SyntaxError before Python 3.12, and this script has to run wherever it is
# pasted, not only on the newest interpreter.
printf '%s' "$PLANS" | python3 -c '
import json, sys
try:
    tiers = json.load(sys.stdin)["pricing"]["human_plans"]["tiers"]
except Exception:
    print("  (no human_plans block -- deploy predates the pricing fix)")
    sys.exit(0)
if not tiers:
    print("  no tiers offered -- either no Stripe plan Price IDs are set on")
    print("  this node, or STRIPE_SECRET_KEY is not a usable Stripe key.")
    print("  Nothing is for sale to humans here.")
else:
    for t in tiers:
        print("  offers %s: $%s/%s" % (t["id"], t["usd"], t["interval"]))
' 2>/dev/null || echo "  (could not parse pricing block)"

# The three tiers and the stripe_api_key rail all come from the same Stripe
# credential. A node that offers plans but omits the rail (or the reverse) has
# a half-configured Stripe and will fail somewhere a customer can see.
OFFERS_PLANS=$(printf '%s' "$PLANS" | grep -c '"interval"' || true)
OFFERS_RAIL=$(printf '%s' "$PLANS" | grep -c 'stripe_api_key' || true)
if [ "$OFFERS_PLANS" -gt 0 ] && [ "$OFFERS_RAIL" -eq 0 ]; then
  fail "manifest offers human plans but not the stripe_api_key rail -- Stripe is half-configured"
elif [ "$OFFERS_PLANS" -eq 0 ] && [ "$OFFERS_RAIL" -gt 0 ]; then
  fail "manifest advertises the stripe_api_key rail but sells no plan -- Stripe is half-configured"
else
  pass "Stripe plans and the Stripe rail agree with each other"
fi

echo
echo "Machine discovery: is this node findable by agents that would pay it?"
DISC=$(curl -sS -m 30 -X POST "$BASE/audit/bundle" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
if printf '%s' "$DISC" | grep -q '"bazaar"'; then
  # Presence was the whole check, and presence is not indexability. The
  # record shipped for months without `info.input.method` while the schema
  # emitted beside it declared method required, so the Bazaar's own
  # facilitator-side validator rejected every one -- and this line passed
  # every time, including the 34/34 of 2026-08-25. A grep for the word
  # "bazaar" cannot tell a catalogued record from a discarded one.
  #
  # Checked with python rather than the x402 library because this script
  # runs in Cloud Shell against the deployed node, where the library is not
  # installed. `method` is the field that actually broke; the unit tests in
  # tests/test_x402_payments.py run the full validator.
  if printf '%s' "$DISC" | python3 -c '
import json, sys
try:
    info = json.load(sys.stdin)["extensions"]["bazaar"]["info"]["input"]
except Exception:
    sys.exit(1)
# mcp-type records carry no method by design; only body records need one.
sys.exit(0 if info.get("type") != "http" or info.get("method") else 1)
' 2>/dev/null; then
    pass "402 carries x402 Bazaar discovery data (indexable by facilitators)"
  else
    fail "402 carries a Bazaar record that names no HTTP method -- a validating facilitator discards it, so this node is not indexable by capability"
  fi
else
  echo "  NOTE  no Bazaar discovery on the 402 -- expected while x402 is off."
  echo "        Set X402_FACILITATOR_URL and X402_PAY_TO_ADDRESS to turn on"
  echo "        both the x402 rail and Bazaar indexing. Until then agents can"
  echo "        only find this node via the MCP registry, not by capability."
fi

# /audit is the shortest and most guessable paid path on this service. It is
# an alias of /audit/wcag with no catalog row of its own, which once left it
# as the single paid route absent from capability discovery.
ALIAS=$(curl -sS -m 30 -X POST "$BASE/audit" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
NAMED=$(curl -sS -m 30 -X POST "$BASE/audit/wcag" \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
if printf '%s\n%s' "$ALIAS" "$NAMED" | python3 -c '
import json, sys
raw = sys.stdin.read().splitlines()
try:
    alias, named = json.loads(raw[0]), json.loads(raw[1])
except Exception:
    sys.exit(1)
sys.exit(0 if alias.get("extensions") == named.get("extensions") else 1)
' 2>/dev/null; then
  pass "/audit advertises the same capability as the route it aliases"
else
  fail "/audit is discoverable differently from /audit/wcag -- a paid path agents cannot find by capability"
fi

# A manifest that describes its inputs only as prose ("string (required)")
# is one a request generator cannot act on.
if printf '%s' "$MANIFEST" | python3 -c '
import json, sys
try:
    endpoints = json.load(sys.stdin)["endpoints"]
except Exception:
    sys.exit(1)
if not endpoints:
    sys.exit(1)
for endpoint in endpoints:
    schema = endpoint.get("input_schema") or {}
    if schema.get("type") != "object" or "properties" not in schema:
        sys.exit(1)
    if (endpoint.get("output_schema") or {}).get("type") != "object":
        sys.exit(1)
sys.exit(0)
' 2>/dev/null; then
  pass "agent.json publishes parseable JSON Schemas for every endpoint"
else
  fail "agent.json describes its endpoints only in prose -- crawlers and request generators cannot use it"
fi

echo
echo "Live payment rails advertised by the manifest"
METHODS=$(curl -sS -m 30 "$BASE/.well-known/agent.json" 2>/dev/null \
  | tr ',' '\n' | sed -n '/"methods"/,/]/p' | tr -d ' "' | tr '\n' ' ')
if [ -n "${METHODS// /}" ]; then
  echo "  $METHODS"
else
  echo "  (could not parse; check $BASE/.well-known/agent.json by hand)"
fi

echo
echo "The paid path: can a caller who CAN pay actually get an audit?"
# Everything above this point only ever proves that an UNauthenticated call is
# refused with a 402. That is the cheap half of the contract, and on its own it
# is close to worthless: this service returned HTTP 500 to every authenticated
# caller for an unknown length of time -- no Firestore database had ever been
# created, so the API key lookup raised on every keyed request -- while this
# script reported 28/28 passing. The revenue path was dead and nothing said so.
#
# This check costs real money ($0.03), which is why it is opt-in rather than
# always-on. But a skipped check must be loud: silence is exactly what let the
# outage live.
# Resolve a key rather than demanding one. This check used to SKIP on every
# run, because it needed an export that a fresh shell drops -- so the one
# check that answers "can this service take money" was, in practice, never
# run. See scripts/lib-api-key.sh.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-api-key.sh
[ -f "$_LIB_DIR/lib-api-key.sh" ] && . "$_LIB_DIR/lib-api-key.sh"

PAID_KEY=""
if declare -f hv_resolve_api_key >/dev/null 2>&1; then
  hv_resolve_api_key && PAID_KEY="$HV_API_KEY"""
fi

if [ -z "$PAID_KEY" ]; then
  echo "  SKIP  the paid path is NOT verified -- no usable API key."
  echo "        ${HV_KEY_PROBLEM:-no key could be resolved}"
  echo "        This is the check that matters most: everything above only"
  echo "        proves unauthenticated calls are refused. Override with:"
  echo "          HUBVIBE_API_KEY=your_key bash scripts/verify-live.sh"
else
  echo "  using $HV_KEY_SOURCE"
  PAID_BODY=$(mktemp)
  PAID_CODE=$(curl -sS -m 90 -o "$PAID_BODY" -w '%{http_code}' \
    -X POST "$BASE/audit/wcag" \
    -H "X-API-Key: $PAID_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com"}' 2>/dev/null) || PAID_CODE="000"

  case "$PAID_CODE" in
    200)
      # A 200 is necessary but not sufficient: the result has to actually be
      # an audit. A 200 carrying an error object would still be a dead path.
      if grep -q '"pass"' "$PAID_BODY"; then
        pass "authenticated /audit/wcag -> 200 with a real audit result"
      else
        fail "authenticated /audit/wcag -> 200 but the body carries no audit result"
      fi
      ;;
    500)
      fail "authenticated /audit/wcag -> 500. THE PAID PATH IS DEAD. This is a
        server-side fault, not a payment problem -- the service raised an
        unhandled exception. Get the traceback:
          gcloud logging read 'resource.labels.service_name=hubvibe AND severity>=ERROR' --project=resolver-time --freshness=1h --limit=5"
      ;;
    402)
      fail "authenticated /audit/wcag -> 402. The key was not accepted. Either
        it is not a real key, or the key store cannot be reached (check the
        logs for a Firestore error -- a dead key store now degrades to 402
        rather than 500, which is correct but still means no subscriber can
        authenticate)."
      ;;
    502)
      echo "  NOTE  authenticated /audit/wcag -> 502: auth worked, but the audit"
      echo "        itself could not run against example.com. Nothing was billed."
      echo "        The paid path is alive; the target site is the problem."
      ;;
    *)
      fail "authenticated /audit/wcag -> $PAID_CODE (expected 200)"
      ;;
  esac
  rm -f "$PAID_BODY"
fi

echo
echo "-----------------------------------------------"
printf '  %d passed, %d failed\n' "$PASSES" "$FAILURES"
echo "-----------------------------------------------"
echo

[ "$FAILURES" -eq 0 ] || exit 1
