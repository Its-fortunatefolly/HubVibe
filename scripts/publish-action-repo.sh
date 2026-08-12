#!/usr/bin/env bash
#
# Materialize the standalone repo that GitHub Marketplace requires, from the
# action.yml in this monorepo.
#
# Why this exists, so nobody rediscovers it the hard way:
#
#   GitHub's Marketplace publishing rules require that the repo contain a
#   single action.yml AT ITS ROOT, and that the repo contain NO WORKFLOW
#   FILES AT ALL. HubVibe has .github/workflows/python-app.yml and
#   google-cloudrun-docker.yml, so this repo can never itself be published to
#   Marketplace, no matter where action.yml sits.
#
#   Note the two are separate things:
#     * Direct use  -- `uses: Its-fortunatefolly/HubVibe@v1` already works
#                      today off the root action.yml. No Marketplace needed.
#     * Marketplace -- listing/discoverability only, and it needs the clean
#                      standalone repo this script builds.
#
# The monorepo's action.yml stays the single source of truth. This script
# copies it out rather than letting a second hand-maintained copy exist,
# because the copy that drifts is always the one people are running.
#
# Usage:
#   bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action
#
# Then follow the printed steps to push and tag.

set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage: bash scripts/publish-action-repo.sh <target-directory>" >&2
  exit 64
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "error: $TARGET exists and is not empty. Refusing to overwrite." >&2
  exit 1
fi

mkdir -p "$TARGET/scripts"
cp "$REPO_ROOT/action.yml" "$TARGET/action.yml"
cp "$REPO_ROOT/scripts/render_audit_summary.py" "$TARGET/scripts/render_audit_summary.py"

cat > "$TARGET/README.md" <<'MARKDOWN'
# HubVibe Site Compliance Audit

Runs a real, deterministic site audit against a live URL and fails the build
if it doesn't pass: WCAG 2.1 A/AA accessibility (axe-core), SEO, security
headers, and performance.

Every check runs against the actual page — axe-core for accessibility, a real
HTTP response for security headers, a real browser page load for performance.
Nothing here is an LLM guessing at quality, and a check that couldn't run is
never reported as a pass.

## Usage

```yaml
- name: HubVibe compliance audit
  uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://your-site.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
```

Adopting it without letting a third-party outage block your deploys:

```yaml
- uses: Its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://staging.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
    fail-on-error: false      # network/402/5xx warns instead of failing
    fail-on-violation: true   # real findings still gate the build
```

Findings are written to the job summary, so reviewers see the table in the
Checks tab rather than digging through raw logs.

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `url` | yes | — | The live URL to audit. |
| `api-key` | no | — | HubVibe API key (`X-API-Key`). Without it the run reports the service's 402 challenge and how to pay. |
| `endpoint` | no | `bundle` | `wcag`, `seo`, `security`, `performance`, or `bundle`. |
| `fail-on-violation` | no | `true` | Set `false` to report findings without failing the build. |
| `fail-on-error` | no | `true` | Set `false` so infrastructure failures warn instead of failing. |
| `timeout-seconds` | no | `90` | Per-attempt HTTP timeout. |
| `retries` | no | `2` | Retries on network error or 5xx. 4xx is never retried. |
| `base-url` | no | the hosted service | Override for self-hosted deployments. |

## Outputs

| Output | Description |
|---|---|
| `passed` | `"true"` or `"false"` — the audit's overall result. |
| `response` | Raw JSON response body. |
| `http-status` | HTTP status of the final attempt (`000` if it never completed). |

## Pricing

$0.03 per single audit, $0.10 for the bundle. For CI, paying per call over
x402/MPP directly against the REST endpoints is usually the better fit — no
subscription key to store as a repository secret. See
[`/.well-known/agent.json`](https://hubvibe-831480473793.us-south1.run.app/.well-known/agent.json),
which is the only place guaranteed to match what checkout actually charges.

## Source

Developed in the [HubVibe monorepo](https://github.com/Its-fortunatefolly/HubVibe).
This repository is generated from it, because Marketplace requires an action
repository to contain no workflow files.
MARKDOWN

# Guard the one rule that silently disqualifies the listing.
if find "$TARGET" -path '*/.github/workflows/*' -print -quit | grep -q .; then
  echo "error: generated repo contains workflow files; Marketplace will reject it." >&2
  exit 1
fi

cat <<EOF

Generated the Marketplace repo at: $TARGET

Contents (no workflow files, single action.yml at root):
$(cd "$TARGET" && find . -type f | sort | sed 's/^/  /')

Next steps:

  cd $TARGET
  git init -b main
  git add .
  git commit -m "HubVibe Site Compliance Audit action"
  git remote add origin https://github.com/Its-fortunatefolly/hubvibe-audit-action.git
  git push -u origin main

  # Tag the release. v1 is the moving major-version tag consumers pin to.
  git tag -a v1.0.0 -m "v1.0.0"
  git tag -f -a v1 -m "v1"
  git push origin v1.0.0
  git push -f origin v1

Then, on github.com: Releases -> Draft a new release -> choose tag v1.0.0 ->
tick "Publish this Action to the GitHub Marketplace" -> accept the agreement
-> Publish release.

The Marketplace name must be globally unique. "HubVibe Site Compliance Audit"
is set in action.yml; if the publish flow reports it as taken, change the
name there and re-run this script.
EOF
