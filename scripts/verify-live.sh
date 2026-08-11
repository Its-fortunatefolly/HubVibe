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

# A null payTo is worse than no x402 at all: it tells a paying agent to send
# funds nowhere. Absent is correct when x402 is not configured.
if printf '%s' "$CHALLENGE" | grep -q '"payTo": *null'; then
  fail "402 advertises x402 with payTo:null -- agents would pay nowhere"
else
  pass "402 does not advertise an unpayable x402 rail"
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
  pass "402 carries x402 Bazaar discovery data (indexable by facilitators)"
else
  echo "  NOTE  no Bazaar discovery on the 402 -- expected while x402 is off."
  echo "        Set X402_FACILITATOR_URL and X402_PAY_TO_ADDRESS to turn on"
  echo "        both the x402 rail and Bazaar indexing. Until then agents can"
  echo "        only find this node via the MCP registry, not by capability."
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
echo "-----------------------------------------------"
printf '  %d passed, %d failed\n' "$PASSES" "$FAILURES"
echo "-----------------------------------------------"
echo

[ "$FAILURES" -eq 0 ] || exit 1
