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
# One line per run goes to /root/hubvibe-monitor.log. When anything fails and
# /root/.hubvibe-monitor.env holds MONITOR_SMTP_USER, MONITOR_SMTP_PASSWORD and
# MONITOR_ALERT_TO, the owner gets an email with the failing lines.

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
live="$(bash "$REPO/scripts/verify-live.sh" https://hubvibe-io.com 2>&1)"
live_line="$(echo "$live" | grep -E '[0-9]+ passed, [0-9]+ failed' | tail -1 | tr -s ' ')"
if ! echo "$live_line" | grep -q ' 0 failed'; then
  failures=$((failures + 1))
  note "verify-live: ${live_line:-no summary}"
  echo "$live" | grep -E 'FAIL' | head -10 >> "$OUT"
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
  echo "$sweep" | grep -E 'FAIL' | head -15 >> "$OUT"
fi
docker exec "$CONTAINER" rm -rf "$WORK" /tmp/monitor-keys.db /tmp/monitor-purchases.db >/dev/null 2>&1

summary="$STAMP $MODE failures=$failures | live: ${live_line:-?} | $audit | sweep: ${sweep_line:-?}"
echo "$summary" >> "$LOG"

if [ "$failures" -gt 0 ] && [ -f /root/.hubvibe-monitor.env ]; then
  # shellcheck disable=SC1091
  . /root/.hubvibe-monitor.env
  if [ -n "${MONITOR_SMTP_USER:-}" ] && [ -n "${MONITOR_SMTP_PASSWORD:-}" ] && [ -n "${MONITOR_ALERT_TO:-}" ]; then
    SUMMARY="$summary" DETAILS="$(cat "$OUT")" python3 - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
msg = EmailMessage()
msg["Subject"] = "HubVibe health check FAILED"
msg["From"] = os.environ["MONITOR_SMTP_USER"]
msg["To"] = os.environ["MONITOR_ALERT_TO"]
msg.set_content(os.environ["SUMMARY"] + "\n\n" + os.environ["DETAILS"] + "\n")
with smtplib.SMTP_SSL(os.environ.get("MONITOR_SMTP_HOST", "smtp.hostinger.com"), 465,
                      context=ssl.create_default_context(), timeout=30) as smtp:
    smtp.login(os.environ["MONITOR_SMTP_USER"], os.environ["MONITOR_SMTP_PASSWORD"])
    smtp.send_message(msg)
PY
  fi
fi
exit 0
