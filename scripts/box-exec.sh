#!/usr/bin/env bash
# Run a command inside the copy of the node that is serving buyers right now
# (blue or green; scripts/box-lib.sh). For the owner's commands, e.g.:
#
#   bash scripts/box-exec.sh python -m app.purchase_reconcile
#
# Use this, never a hard-coded container name: which copy is live changes
# with every deploy.
set -u
# shellcheck source=scripts/box-lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/box-lib.sh"
container="$(hv_live_container)" || { echo "no copy of the node is running" >&2; exit 1; }
exec docker exec -i "$container" "$@"
