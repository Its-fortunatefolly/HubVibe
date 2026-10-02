#!/usr/bin/env bash
# The checks a buyer depends on, against ONE running copy of the node.
# The hourly monitor runs them on the live copy (scripts/monitor-box.sh);
# the deploy gate runs them on a new build before it takes a single request
# (scripts/deploy-box.sh). One definition of "working", used both ways.
#
#   box-checks.sh CONTAINER BASE_URL [hourly|daily]
#
#   1. scripts/verify-live.sh against BASE_URL (40 checks, free).
#   2. A real bundle audit of example.com inside CONTAINER: the browser,
#      axe-core and the plain fetch all have to work.
#   3. scripts/simulate-work-calls.py inside CONTAINER over the jobs buyers
#      depend on -- real providers, real payment gate and ledger code, a
#      stub facilitator (no USDC moves), and its own ledger, purchase book
#      and key store, so no check ever touches a real record.
#      hourly: only bees whose providers cost $0; daily: every live bee
#      except image and video generation.
#
# stdout: one summary line, then the failing lines (if any).
# exit:   the number of checks that failed (0 = all passed).

set -u
CONTAINER="${1:?usage: box-checks.sh CONTAINER BASE_URL [hourly|daily]}"
BASE="${2:?usage: box-checks.sh CONTAINER BASE_URL [hourly|daily]}"
MODE="${3:-hourly}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK=/tmp/monitor-repo
DETAILS="$(mktemp)"
trap 'rm -f "$DETAILS"' EXIT

HOURLY_BEES="/work/market/prediction,/work/market/quote,/work/market/stock,/work/email/verify,/work/company/enrich,/work/sanctions/screen,/work/stats/probability,/work/extract/page"

failures=0
note() { echo "$*" >> "$DETAILS"; }

# 1. The node, as an agent sees it.
live="$(bash "$REPO/scripts/verify-live.sh" "$BASE" 2>&1)"
live_line="$(grep -E '[0-9]+ passed, [0-9]+ failed' <<< "$live" | tail -1 | tr -s ' ')"
if ! grep -q ' 0 failed' <<< "$live_line"; then
  failures=$((failures + 1))
  note "verify-live: ${live_line:-no summary}"
  sed 's/\x1b\[[0-9;]*m//g' <<< "$live" | grep -E 'FAIL' | head -10 >> "$DETAILS"
fi

# 2 + 3 run inside the container, on a fresh copy of the repo's code.
docker exec "$CONTAINER" rm -rf "$WORK" >/dev/null 2>&1
docker exec "$CONTAINER" mkdir -p "$WORK" >/dev/null 2>&1
docker cp "$REPO/scripts" "$CONTAINER:$WORK/scripts" >/dev/null 2>&1
docker cp "$REPO/wcag-audit-engine" "$CONTAINER:$WORK/wcag-audit-engine" >/dev/null 2>&1

audit="$(docker exec -w /app "$CONTAINER" timeout 60 python3 -c '
import time, app.main as m
t = time.monotonic()
axe, perf, shared = m._bundle_inputs("https://example.com/")
assert isinstance(axe.get("violations"), list) and perf.get("status") == "ok"
print("audit ok %.1fs" % (time.monotonic() - t))
' 2>&1 | grep -v '^INFO' | tail -2)"
if ! grep -q '^audit ok' <<< "$audit"; then
  failures=$((failures + 1))
  note "bundle audit of example.com failed: $audit"
fi

only="$HOURLY_BEES"
if [ "$MODE" = "daily" ]; then
  only="$(docker exec "$CONTAINER" python3 -c '
import json, urllib.request
d = json.load(urllib.request.urlopen("http://127.0.0.1:8080/work", timeout=20))
skip = {"/work/video/generate", "/work/image/generate"}
print(",".join(w["path"] for w in d["workers"] if w["path"] not in skip))
' 2>/dev/null)"
fi
sweep="$(docker exec -w "$WORK" \
  -e KEY_STORE_SQLITE_PATH=/tmp/monitor-keys.db \
  -e PURCHASE_BOOK_PATH=/tmp/monitor-purchases.db \
  -e PURCHASE_ALERT_WEBHOOK= \
  -e A2A_TASKS_PATH= \
  "$CONTAINER" timeout 1500 env -u CLOUD_SHELL -u DEVSHELL_PROJECT_ID \
  python3 scripts/simulate-work-calls.py --only "$only" 2>&1)"
sweep_rc=$?
sweep_line="$(grep -iE 'passed|failed' <<< "$sweep" | tail -1)"
if [ "$sweep_rc" -ne 0 ]; then
  failures=$((failures + 1))
  note "work sweep ($MODE) exit $sweep_rc: ${sweep_line:-no summary}"
  sed 's/\x1b\[[0-9;]*m//g' <<< "$sweep" | grep -E 'FAIL' | head -15 >> "$DETAILS"
fi
docker exec "$CONTAINER" rm -rf "$WORK" /tmp/monitor-keys.db /tmp/monitor-purchases.db >/dev/null 2>&1

echo "failures=$failures | live: ${live_line:-?} | $audit | sweep: ${sweep_line:-?}"
cat "$DETAILS"
exit "$failures"
