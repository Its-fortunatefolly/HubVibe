#!/usr/bin/env bash
# Health check for the HubVibe box, run by cron. One missed buyer is one too
# many, so the node is exercised the way a buyer would use it, every hour,
# by scripts/box-checks.sh -- the same checks the deploy gate runs on a new
# build before it may take traffic:
#
#   1. scripts/verify-live.sh against the public node (40 checks, free).
#   2. A real bundle audit of example.com inside the live container.
#   3. scripts/simulate-work-calls.py over the jobs buyers depend on, with
#      a stub facilitator and its own records (no USDC, no real rows).
#
#   monitor-box.sh            hourly set: only bees whose providers cost $0
#   monitor-box.sh daily      every live bee except image and video generation
#
# The live container is whichever copy (blue or green) serves buyers right
# now (scripts/box-lib.sh). Cron runs this under the lock deploys take, so
# a deploy never switches copies in the middle of a check.
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
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# shellcheck source=scripts/box-lib.sh
. "$REPO/scripts/box-lib.sh"

BASE="${MONITOR_BASE:-https://hubvibe-io.com}"
if CONTAINER="$(hv_live_container)"; then
  result="$(bash "$REPO/scripts/box-checks.sh" "$CONTAINER" "$BASE" "$MODE")"
  failures=$?
else
  CONTAINER=""
  failures=1
  result="failures=1 | no copy of the node is running (active colors: '$(hv_active_colors)')"
fi
summary="$STAMP $MODE $(head -1 <<< "$result")"
details="$(tail -n +2 <<< "$result")"

echo "$summary" >> "$LOG"
if [ "$failures" -gt 0 ] && [ -n "$details" ]; then
  # The evidence stays on the box even if the email cannot go out.
  sed 's/^/    /' <<< "$details" >> "$LOG"
fi

# Sales since the last run, from the purchase book (outside buyers only).
SALES_STATE=/root/.hubvibe-monitor.last-sale
since="$(cat "$SALES_STATE" 2>/dev/null || date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)"
sales=""
if [ -n "$CONTAINER" ]; then
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
fi

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

$details"
fi
if [ -n "$sales" ]; then
  count="$(grep -c . <<< "$sales")"
  send_mail "HubVibe: $count new sale(s)" "$sales"
fi
exit 0
