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
expect_status() {
  local method="$1" path="$2" expected="$3" desc="${4:-$path}"
  local code
  if [ "$method" = "POST" ]; then
    code=$(curl -sS -m 45 -o /dev/null -w '%{http_code}' -X POST "$BASE$path" \
      -H 'Content-Type: application/json' -d '{"url":"https://example.com"}' 2>/dev/null)
  else
    code=$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "$BASE$path" 2>/dev/null)
  fi
  if [ "$code" = "$expected" ]; then
    pass "$desc -> $code"
  else
    fail "$desc -> got $code, expected $expected"
  fi
}

echo
echo "HubVibe live verification: $BASE"
echo

echo "Service up"
expect_status GET /healthz 200 "GET /healthz"
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
