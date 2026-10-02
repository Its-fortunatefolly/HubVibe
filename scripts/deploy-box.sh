#!/usr/bin/env bash
# Deploy to the box with no downtime, and with no build in front of a buyer
# that has not first passed the checks the hourly monitor runs.
#
#   bash scripts/deploy-box.sh              deploy the commit checked out here
#   bash scripts/deploy-box.sh rollback     put the previous build back in front
#   bash scripts/deploy-box.sh status       which build serves, which stands by
#
# The node is defined twice, as hubvibe-blue and hubvibe-green
# (deploy/vps/docker-compose.yml). One serves buyers. A deploy:
#
#   1. builds the commit as the OTHER color; the live copy is not touched;
#   2. starts it beside the live copy, standing by (its /ready says 503, so
#      Caddy sends it nothing);
#   3. gates it: its /health must report this commit, then scripts/
#      box-checks.sh runs against it -- verify-live's 40 checks, a real
#      browser audit, and the hourly bee sweep on real providers. Any
#      failure stops the new copy; buyers never saw it;
#   4. switches: both copies ready for a moment, then only the new one.
#      Caddy moves traffic at its next probe, and requests already running
#      on the old copy finish there;
#   5. proves it from outside: the public /health must answer from the new
#      build five times in a row, then verify-live must pass on the public
#      URL. If either fails, traffic goes straight back to the old copy,
#      which is still running, and the new one is stopped;
#   6. retires the old copy gracefully: it finishes its in-flight calls and
#      its handed-back jobs (up to 300 s). Its image stays, so `rollback`
#      can put it back through the same gate.
#
# Any change to what runs goes through here -- code, .env, or the Caddyfile
# (Caddy is reloaded in place from the file, gracefully, before the switch).
# One run at a time, and never during a health check: both take the lock
# the monitor cron uses. Every run leaves one line in /root/hubvibe-deploy.log.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/box-lib.sh
. "$REPO/scripts/box-lib.sh"

ACTION="${1:-deploy}"
STARTED=$SECONDS
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say()    { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()     { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
note()   { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; }
record() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$HV_DEPLOY_LOG"; }
die()    { printf '  \033[31mSTOP\033[0m  %s\n' "$*"; record "$ACTION FAILED: $*"; exit 1; }

if [ "${CLOUD_SHELL:-}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
  die "this runs ON the box (ssh root@the-box, then cd /root/HubVibe). Nothing was changed."
fi
command -v docker >/dev/null 2>&1 || die "docker is not on PATH: run this on the box"
DOMAIN="$(hv_domain)"
[ -n "$DOMAIN" ] || die "no DOMAIN in $HV_COMPOSE_DIR/.env"
# Where buyers reach the node; overridable only to rehearse on a copy.
PUBLIC="${HV_PUBLIC_URL:-https://$DOMAIN}"

# "status build color" from a node's /health, or nothing.
health_of() {
  curl -s -m 10 "$1/health" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print(d.get("status") or "-", d.get("build") or "-", d.get("color") or "-")' 2>/dev/null
}

# The copy's own URL once its /health is ok and reports BUILD (any build when
# BUILD is empty). Fails if the container stops, crashes or 4 minutes pass.
wait_up() {
  local container="$1" want="$2" deadline=$((SECONDS + 240)) ip st bd cl restarts
  # A copy that crashes while starting is restarted by Docker (restart:
  # unless-stopped) and looks alive between crashes; one restart is a fail.
  restarts="$(docker inspect -f '{{.RestartCount}}' "$container" 2>/dev/null || echo 0)"
  while [ "$SECONDS" -lt "$deadline" ]; do
    hv_running "$container" || return 1
    [ "$(docker inspect -f '{{.RestartCount}}' "$container" 2>/dev/null || echo 0)" = "$restarts" ] || return 1
    ip="$(hv_ip_of "$container")"
    if [ -n "$ip" ]; then
      read -r st bd cl <<< "$(health_of "http://$ip:8080")"
      if [ "${st:-}" = ok ] && { [ -z "$want" ] || [ "${bd:-}" = "$want" ]; }; then
        echo "http://$ip:8080"
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

# True once five public /health answers in a row come from COLOR (and BUILD,
# when given): buyers are reaching it through Caddy.
public_on() {
  local color="$1" build="$2" streak=0 tries=0 st bd cl
  while [ "$tries" -lt 90 ]; do
    read -r st bd cl <<< "$(health_of "$PUBLIC")"
    if [ "${cl:-}" = "$color" ] && { [ -z "$build" ] || [ "${bd:-}" = "$build" ]; }; then
      streak=$((streak + 1))
      [ "$streak" -ge 5 ] && return 0
    else
      streak=0
    fi
    tries=$((tries + 1))
    sleep 1
  done
  return 1
}

# Requests Caddy has in flight to COLOR right now.
upstream_busy() {
  hv_compose exec -T caddy wget -qO- http://127.0.0.1:2019/reverse_proxy/upstreams 2>/dev/null \
    | python3 -c '
import json, sys
want = "hubvibe-%s:8080" % sys.argv[1]
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
print(sum(int(r.get("num_requests") or 0) for r in rows if r.get("address") == want))' "$1"
}

# Load deploy/vps/Caddyfile into the running Caddy: validated first, then a
# graceful reload (requests in flight finish under the config they started
# with; an unchanged file is a no-op). Read from stdin because the bind
# mount pins the file Caddy started with, and git replaces files on checkout.
caddy_sync() {
  hv_compose exec -T caddy caddy validate --adapter caddyfile --config /dev/stdin \
    < "$HV_COMPOSE_DIR/Caddyfile" >"$TMP/caddy.out" 2>&1 || return 1
  hv_compose exec -T caddy caddy reload --adapter caddyfile --config /dev/stdin \
    < "$HV_COMPOSE_DIR/Caddyfile" >>"$TMP/caddy.out" 2>&1
}

run_gate() {  # CONTAINER URL
  say "Checking the new copy before it takes a single request"
  local result rc
  result="$(bash "$REPO/scripts/box-checks.sh" "$1" "$2" hourly)"
  rc=$?
  sed 's/^/    /' <<< "$result"
  return "$rc"
}

public_verify() {
  local out line
  out="$(bash "$REPO/scripts/verify-live.sh" "$PUBLIC" 2>&1)"
  line="$(grep -E '[0-9]+ passed, [0-9]+ failed' <<< "$out" | tail -1 | tr -s ' ')"
  VERIFY_LINE="${line:-no summary}"
  grep -q ' 0 failed' <<< "$line" && return 0
  sed 's/\x1b\[[0-9;]*m//g' <<< "$out" | grep -E 'FAIL' | head -10 | sed 's/^/    /'
  return 1
}

# Stop OLD_CONTAINER once Caddy has nothing in flight to it (COLOR names its
# upstream; empty for the pre-blue/green container). It then finishes what
# it holds, handed-back jobs included, within its 300 s grace.
retire() {
  local container="$1" color="$2" waited=0
  if [ -n "$color" ]; then
    while [ "$(upstream_busy "$color")" != 0 ] && [ "$waited" -lt 120 ]; do
      sleep 2
      waited=$((waited + 2))
    done
  fi
  docker stop -t 300 "$container" >/dev/null 2>&1
}

lock() {
  exec 9>>"$HV_LOCK"
  if ! flock -n 9; then
    note "a deploy or health check is running; waiting for it (up to 30 min)"
    flock -w 1800 9 || die "the lock $HV_LOCK stayed taken for 30 minutes"
  fi
}

# A file listing two colors means an earlier run stopped mid-switch: keep
# whichever one buyers are actually reaching.
settle_active() {
  local active serving st bd
  active="$(hv_active_colors)"
  if [ "$(wc -w <<< "$active")" -gt 1 ]; then
    read -r st bd serving <<< "$(health_of "$PUBLIC")"
    case " $active " in
      *" ${serving:-none} "*)
        hv_running "$(hv_container_of "$serving")" || die "the live colors are '$active' and $serving is not running"
        hv_write_active "$serving"
        note "an earlier run stopped mid-switch; $serving serves buyers, so it is the live copy"
        ;;
      *) die "the live colors are '$active' but the public node answers as '${serving:-nothing}'" ;;
    esac
  fi
}

# Switch buyers from FROM (a color, or "" for the pre-blue/green container)
# to TO at BUILD, prove it from outside, and retire FROM. Undoes itself if
# the proof fails.
promote() {
  local from="$1" to="$2" build="$3" to_container
  to_container="$(hv_container_of "$to")"
  say "Switching buyers to $to (${build:0:7})"
  if [ -z "$from" ]; then
    # From the single container: Caddy still routes by name to it. Keep its
    # running config to undo with, then load the blue/green Caddyfile.
    hv_compose exec -T caddy wget -qO- http://127.0.0.1:2019/config/ > "$TMP/caddy-before.json" 2>/dev/null
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$TMP/caddy-before.json" 2>/dev/null \
      || { docker stop -t 30 "$to_container" >/dev/null 2>&1; die "cannot read Caddy's running config (needed to undo); nothing changed"; }
    hv_write_active "$to"
    if ! caddy_sync; then
      hv_write_active ""
      docker stop -t 30 "$to_container" >/dev/null 2>&1
      die "Caddy refused deploy/vps/Caddyfile: $(tail -2 "$TMP/caddy.out" | tr '\n' ' '); nothing changed"
    fi
  else
    if ! caddy_sync; then
      docker stop -t 30 "$to_container" >/dev/null 2>&1
      die "Caddy refused deploy/vps/Caddyfile: $(tail -2 "$TMP/caddy.out" | tr '\n' ' '); still on $from"
    fi
    hv_write_active "$from $to"
    sleep 4   # longer than health_passes x health_interval: Caddy has seen $to ready
    hv_write_active "$to"
  fi

  undo() {
    if [ -z "$from" ]; then
      hv_compose exec -T caddy caddy reload --config /dev/stdin < "$TMP/caddy-before.json" >/dev/null 2>&1
      hv_write_active ""
    else
      hv_write_active "$from"
    fi
    docker stop -t 300 "$to_container" >/dev/null 2>&1
  }

  if ! public_on "$to" "$build"; then
    undo
    die "the public node did not answer from $to within 90 s; buyers are back on ${from:-the previous container}"
  fi
  ok "buyers reach $to (${build:0:7}) through $PUBLIC"
  say "Checking the public node now that buyers are on the new copy"
  if ! public_verify; then
    undo
    die "verify-live on $PUBLIC after the switch: $VERIFY_LINE; buyers are back on ${from:-the previous container}"
  fi
  ok "verify-live on $PUBLIC: $VERIFY_LINE"

  say "Retiring the old copy (it finishes everything in flight first)"
  if [ -z "$from" ]; then
    retire "$HV_LEGACY_CONTAINER" ""
    ok "stopped $HV_LEGACY_CONTAINER (kept, not removed)"
  else
    retire "$(hv_container_of "$from")" "$from"
    ok "stopped hubvibe-$from; its build stays for rollback"
  fi
}

deploy() {
  local sha active cand cand_container url
  git -C "$REPO" diff --quiet HEAD -- 2>/dev/null \
    || die "tracked files differ from the commit; deploy a commit, not an edit (git -C $REPO status)"
  sha="$(git -C "$REPO" rev-parse HEAD)"
  settle_active
  active="$(hv_active_colors)"
  local mode=switch
  case "$active" in
    blue|green) cand="$(hv_other "$active")" ;;
    "")
      cand=blue
      if hv_running "$HV_LEGACY_CONTAINER"; then mode=migrate; else mode=first; fi
      ;;
    *) die "unreadable live color '$active' in $(hv_active_file)" ;;
  esac
  cand_container="$(hv_container_of "$cand")"
  say "Deploying ${sha:0:7} as hubvibe-$cand (live: ${active:-$([ "$mode" = migrate ] && echo "$HV_LEGACY_CONTAINER" || echo none)})"

  say "Building (the live copy keeps serving)"
  HUBVIBE_BUILD_SHA="$sha" hv_compose build "hubvibe-$cand" >"$TMP/build.out" 2>&1 \
    || { tail -20 "$TMP/build.out"; die "the build failed; nothing changed"; }
  ok "built"

  if [ "$mode" != first ]; then
    # The new copy must start NOT ready: list only what is live now.
    hv_write_active "$active" || die "cannot write $(hv_active_file); nothing changed"
  fi
  say "Starting it beside the live copy, standing by"
  HUBVIBE_BUILD_SHA="$sha" hv_compose up -d --no-deps --no-build --force-recreate "hubvibe-$cand" \
    >"$TMP/up.out" 2>&1 || { tail -20 "$TMP/up.out"; die "could not start hubvibe-$cand; nothing changed"; }
  if ! url="$(wait_up "$cand_container" "$sha")"; then
    docker logs --tail 30 "$cand_container" 2>&1 | sed 's/^/    /'
    docker stop -t 30 "$cand_container" >/dev/null 2>&1
    die "hubvibe-$cand did not come up healthy on ${sha:0:7} (it crashed, or 4 minutes passed); buyers never saw it"
  fi
  ok "up at $url on ${sha:0:7}"

  if [ "$mode" = first ]; then
    # Nothing was serving, so there is no one to protect yet: start serving.
    hv_write_active "$cand" || die "cannot write the active-colors file"
    hv_compose up -d caddy >/dev/null 2>&1 || die "could not start caddy"
    record "deploy ${sha:0:7} -> $cand (first start, no gate: nothing was serving) $((SECONDS - STARTED))s"
    ok "serving on $cand. Prove it from anywhere: bash scripts/verify-live.sh $PUBLIC"
    return 0
  fi

  if ! run_gate "$cand_container" "$url"; then
    docker stop -t 30 "$cand_container" >/dev/null 2>&1
    die "${sha:0:7} failed its checks (above); buyers never saw it and are still on ${active:-$HV_LEGACY_CONTAINER}"
  fi
  ok "gate passed"

  promote "$([ "$mode" = migrate ] && echo "" || echo "$active")" "$cand" "$sha"
  record "deploy ${sha:0:7} -> $cand (from ${active:-$HV_LEGACY_CONTAINER}) gate passed, public $VERIFY_LINE, $((SECONDS - STARTED))s"
  ok "done in $((SECONDS - STARTED))s: ${sha:0:7} serves on $cand"
}

rollback() {
  local active target target_container build url
  settle_active
  active="$(hv_active_colors)"
  case "$active" in
    blue|green) ;;
    *) die "rollback needs one live color; the file says '${active:-none}'" ;;
  esac
  target="$(hv_other "$active")"
  target_container="$(hv_container_of "$target")"
  hv_exists "$target_container" \
    || die "there is no previous build to go back to: hubvibe-$target has never run (the first gated deploy has no predecessor in blue/green)"
  build="$(hv_build_of "$target_container")"
  say "Rolling back: hubvibe-$target (${build:0:7}) in place of hubvibe-$active"
  hv_write_active "$active" || die "cannot write $(hv_active_file)"
  hv_compose up -d --no-deps --no-build "hubvibe-$target" >"$TMP/up.out" 2>&1 \
    || { tail -20 "$TMP/up.out"; die "could not start hubvibe-$target"; }
  url="$(wait_up "$target_container" "")" \
    || { docker stop -t 30 "$target_container" >/dev/null 2>&1; die "hubvibe-$target did not come up healthy"; }
  if [ "${2:-}" != "--now" ]; then
    run_gate "$target_container" "$url" \
      || { docker stop -t 30 "$target_container" >/dev/null 2>&1; die "the previous build failed its checks too; still on $active"; }
  else
    note "--now: the bee sweep is skipped; /health passed"
  fi
  promote "$active" "$target" "$build"
  record "rollback -> $target (${build:0:7}) from $active, public $VERIFY_LINE, $((SECONDS - STARTED))s"
  ok "done: ${build:0:7} serves on $target"
}

status() {
  local active color container st bd cl
  active="$(hv_active_colors)"
  printf 'live color(s): %s\n' "${active:-none recorded}"
  for color in blue green; do
    container="$(hv_container_of "$color")"
    if hv_exists "$container"; then
      printf '  hubvibe-%-5s %-8s build %s\n' "$color" \
        "$(docker inspect -f '{{.State.Status}}' "$container")" "$(hv_build_of "$container" | cut -c1-7)"
    else
      printf '  hubvibe-%-5s never started\n' "$color"
    fi
  done
  if hv_exists "$HV_LEGACY_CONTAINER"; then
    printf '  %-13s %s (pre-blue/green)\n' "$HV_LEGACY_CONTAINER" "$(docker inspect -f '{{.State.Status}}' "$HV_LEGACY_CONTAINER")"
  fi
  read -r st bd cl <<< "$(health_of "$PUBLIC")"
  printf 'public %s: status %s, build %s, color %s\n' "$PUBLIC" "${st:-?}" "$(cut -c1-7 <<< "${bd:-?}")" "${cl:-?}"
  printf 'checkout: %s\n' "$(git -C "$REPO" log --oneline -1 2>/dev/null)"
  if [ -f "$HV_DEPLOY_LOG" ]; then
    echo "last deploys:"
    tail -5 "$HV_DEPLOY_LOG" | sed 's/^/  /'
  fi
}

case "$ACTION" in
  deploy)   lock; deploy ;;
  rollback) lock; rollback "$@" ;;
  status)   status ;;
  *) printf 'usage: bash scripts/deploy-box.sh [deploy|rollback [--now]|status]\n'; exit 2 ;;
esac
