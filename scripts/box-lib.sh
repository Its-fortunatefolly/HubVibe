# shellcheck shell=bash
# Shared by the scripts that run ON the box: where the stack lives, which
# copy of the node is live, and the lock that keeps deploys and health
# checks from overlapping. Source it; it defines functions and variables
# only and runs nothing.
#
# The node runs as two identical compose services, hubvibe-blue and
# hubvibe-green (deploy/vps/docker-compose.yml). The live color(s) are
# written in one file on the node's volume, /data/hubvibe-active, which the
# app reads for /ready (Caddy routes to whichever copy answers 200) and
# scripts/deploy-box.sh writes. Nothing else decides which copy serves.

HV_REPO="${HV_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HV_COMPOSE_DIR="${HV_COMPOSE_DIR:-$HV_REPO/deploy/vps}"
# Compose names everything after the project, which is the directory name.
HV_PROJECT="${HV_PROJECT:-vps}"
HV_VOLUME="${HV_PROJECT}_hubvibe_data"
# The single container the stack ran as before blue/green (one deploy
# migrates from it; see deploy-box.sh).
HV_LEGACY_CONTAINER="${HV_PROJECT}-hubvibe-1"
# Taken by every deploy and every monitor run (the cron line locks the same
# file), so a deploy never switches copies under a running health check.
# shellcheck disable=SC2034  # read by the scripts that source this file
HV_LOCK="${HV_LOCK:-/tmp/hv-monitor.lock}"
# shellcheck disable=SC2034
HV_DEPLOY_LOG="${HV_DEPLOY_LOG:-/root/hubvibe-deploy.log}"

hv_compose() {
  docker compose -f "$HV_COMPOSE_DIR/docker-compose.yml" --project-directory "$HV_COMPOSE_DIR" "$@"
}

hv_other() { if [ "$1" = blue ]; then echo green; else echo blue; fi; }

hv_container_of() { echo "${HV_PROJECT}-hubvibe-$1-1"; }

hv_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }

hv_exists() { docker inspect "$1" >/dev/null 2>&1; }

hv_ip_of() {
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1" 2>/dev/null
}

# The build a container was made from (HUBVIBE_BUILD_SHA, stamped at build).
hv_build_of() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | sed -n 's/^HUBVIBE_BUILD_SHA=//p' | head -1
}

# Path of the active-colors file on the host (inside the volume).
hv_active_file() {
  local dir
  dir="$(docker volume inspect -f '{{.Mountpoint}}' "$HV_VOLUME" 2>/dev/null)" || return 1
  [ -n "$dir" ] || return 1
  echo "$dir/hubvibe-active"
}

# The live color(s), space separated; empty when none is recorded.
hv_active_colors() {
  local file
  file="$(hv_active_file)" || return 0
  [ -f "$file" ] || return 0
  tr -s ' \t\n' ' ' < "$file" | sed 's/^ //; s/ $//'
}

# Atomic: the app reads this file once a second for every probe.
hv_write_active() {
  local file tmp
  file="$(hv_active_file)" || return 1
  tmp="$file.tmp.$$"
  printf '%s\n' "$*" > "$tmp" && mv -f "$tmp" "$file"
}

# The container buyers are reaching: the running copy of a live color, else
# the pre-blue/green container. Prints nothing and fails when none runs.
hv_live_container() {
  local color container
  for color in $(hv_active_colors); do
    container="$(hv_container_of "$color")"
    if hv_running "$container"; then
      echo "$container"
      return 0
    fi
  done
  if hv_running "$HV_LEGACY_CONTAINER"; then
    echo "$HV_LEGACY_CONTAINER"
    return 0
  fi
  return 1
}

# The domain the node answers on, from deploy/vps/.env.
hv_domain() { sed -n 's/^DOMAIN=//p' "$HV_COMPOSE_DIR/.env" 2>/dev/null | tail -1; }
