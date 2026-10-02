#!/usr/bin/env bash
# Health check for the HubVibe box, run by cron. One missed buyer is one too
# many, so the node is exercised the way a buyer would use it, every hour:
#
#   1. scripts/verify-live.sh against the public node (40 checks, free).
#   2. A real bundle audit of example.com inside the live container: the
#      browser, axe-core and the plain fetch all have to work.
#   3. scripts/simulate-work-calls.py over the jobs buyers depend on --
#      real providers, real payment gate and ledger code, a stub
#      facilitator (no USDC moves), and its own ledger, purchase book and
#      key store, so no health check ever touches a real record.
#
#   monitor-box.sh            hourly set: only bees whose providers cost $0
#   monitor-box.sh daily      every live bee except image and video generation
#
# One line per run goes to /root/hubvibe-monitor.log. Email goes out through
# the Hostinger Mail API from hubvibe@hubvibe-io.com when
# /root/.hubvibe-monitor.env holds MONITOR_MAIL_TOKEN (a token scoped to that
# one mailbox), MONITOR_MAILBOX_ID and MONITOR_ALERT_TO:
#   - any failed check -> "HubVibe health check FAILED" with the failing lines;
#   - any new outside sale since the last run -> "HubVibe: N new sale(s)".

set -u
MODE="${1:-hourly}"
REPO=/root/HubVibe
LOG=/root/hubvibe-monitor.log
CONTAINER=vps-hubvibe-1
WORK=/tmp/monitor-repo
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

HOURLY_BEES="/work/market/prediction,/work/market/quote,/work/market/stock,/work/email/verify,/work/company/enrich,/work/sanctions/screen,/work/stats/probability,/work/extract/page"

failures=0
note() { echo "$*" >> "$OUT"; }

# 1. The public node, as an agent sees it.
BASE="${MONITOR_BASE:-https://hubvibe-io.com}"
live="$(bash "$REPO/scripts/verify-live.sh" "$BASE" 2>&1)"
live_line="$(echo "$live" | grep -E '[0-9]+ passed, [0-9]+ failed' | tail -1 | tr -s ' ')"
if ! echo "$live_line" | grep -q ' 0 failed'; then
  failures=$((failures + 1))
  note "verify-live: ${live_line:-no summary}"
  echo "$live" | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'FAIL' | head -10 >> "$OUT"
fi

# 2 + 3 run inside the live container, on a fresh copy of the repo's code.
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
if ! echo "$audit" | grep -q '^audit ok'; then
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
  "$CONTAINER" timeout 1500 env -u CLOUD_SHELL -u DEVSHELL_PROJECT_ID \
  python3 scripts/simulate-work-calls.py --only "$only" 2>&1)"
sweep_rc=$?
sweep_line="$(echo "$sweep" | grep -iE 'passed|failed' | tail -1)"
if [ "$sweep_rc" -ne 0 ]; then
  failures=$((failures + 1))
  note "work sweep ($MODE) exit $sweep_rc: ${sweep_line:-no summary}"
  echo "$sweep" | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'FAIL' | head -15 >> "$OUT"
fi
docker exec "$CONTAINER" rm -rf "$WORK" /tmp/monitor-keys.db /tmp/monitor-purchases.db >/dev/null 2>&1

summary="$STAMP $MODE failures=$failures | live: ${live_line:-?} | $audit | sweep: ${sweep_line:-?}"
echo "$summary" >> "$LOG"
if [ "$failures" -gt 0 ]; then
  # The evidence stays on the box even if the email cannot go out.
  sed 's/^/    /' "$OUT" >> "$LOG"
fi

# Sales since the last run, from the purchase book (outside buyers only).
SALES_STATE=/root/.hubvibe-monitor.last-sale
since="$(cat "$SALES_STATE" 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)"
sales="$(docker exec "$CONTAINER" sh -c 'curl -s -m 20 -H "X-API-Key: $PURCHASE_OWNER_KEY" "http://127.0.0.1:8080/owner/purchases.csv"' 2>/dev/null \
  | SINCE="$since" python3 -c '
import csv, os, sys
since = os.environ["SINCE"]
rows = [r for r in csv.DictReader(sys.stdin)
        if r.get("internal") == "0" and r.get("kind") in ("sale", "topup")
        and str(r.get("outcome", "")).startswith("delivered")
        and (r["date_utc"] + "T" + r["time_utc"] + "Z") > since]
for r in rows:
    print("%sT%sZ  %-20s $%s  payer %s  via %s" % (r["date_utc"], r["time_utc"], r["product"],
          r["received_usd"] or r["price_usd"], (r["payer"] or "?")[:12], r.get("user_agent", "")[:40]))
' 2>/dev/null)"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SALES_STATE"

send_mail() {  # subject, body
  [ -f /root/.hubvibe-monitor.env ] || return 0
  # Exported, not just set: the sender is a separate python process and reads
  # them from its environment. Sourced without -a they never reached it, and
  # the 2026-10-02 15:17 failure alert silently did not send.
  set -a
  # shellcheck disable=SC1091
  . /root/.hubvibe-monitor.env
  set +a
  [ -n "${MONITOR_MAIL_TOKEN:-}" ] && [ -n "${MONITOR_MAILBOX_ID:-}" ] && [ -n "${MONITOR_ALERT_TO:-}" ] || return 0
  SUBJECT="$1" BODY="$2" python3 - <<'PY'
import json, os, urllib.request
body = json.dumps({"to": [os.environ["MONITOR_ALERT_TO"]], "displayName": "HubVibe",
                   "subject": os.environ["SUBJECT"], "text": os.environ["BODY"]}).encode()
req = urllib.request.Request(
    "https://api.mail.hostinger.com/api/v1/mailboxes/%s/send" % os.environ["MONITOR_MAILBOX_ID"],
    data=body, method="POST", headers={"Authorization": "Bearer " + os.environ["MONITOR_MAIL_TOKEN"],
                                       "Content-Type": "application/json", "User-Agent": "hubvibe-monitor"})
urllib.request.urlopen(req, timeout=30)
PY
  local rc=$?
  [ "$rc" -eq 0 ] || echo "$STAMP alert email FAILED to send (python exit $rc): $1" >> "$LOG"
}

if [ "$failures" -gt 0 ]; then
  send_mail "HubVibe health check FAILED" "$summary

$(cat "$OUT")"
fi
if [ -n "$sales" ]; then
  count="$(echo "$sales" | grep -c .)"
  send_mail "HubVibe: $count new sale(s)" "$sales"
fi
exit 0
