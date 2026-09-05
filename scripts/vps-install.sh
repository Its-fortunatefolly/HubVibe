#!/usr/bin/env bash
# Put the whole tollbooth on THIS machine, behind HTTPS, in one command.
#
#     git clone https://github.com/Its-fortunatefolly/HubVibe
#     cd HubVibe
#     bash scripts/vps-install.sh yourdomain.com
#
# Point the domain's DNS A record at this box first (an apex or a subdomain,
# either works). The script validates the payment configuration BEFORE it
# touches Docker -- a refused recipient must cost zero setup -- then installs
# Docker if missing, writes deploy/vps/.env, builds, starts the stack, and
# waits for the service to answer.
#
# Why a flat-rate box at all: the per-call rails (x402, MPP) never needed
# Google -- only the API-key store did, and KEY_STORE=sqlite (set by the
# compose file) replaces it with one local file. A machine with a fixed
# monthly price is the one host that cannot surprise-bill.
#
# Overrides, all optional:
#     X402_PAY_TO_ADDRESS=0x...   a different receiving wallet
#     X402_FACILITATOR_URL=...    a different facilitator
#     STRIPE_SECRET_KEY=...       plus the other Stripe vars: their rails
#                                 go live too; unset, they stay off.

set -uo pipefail

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mNOTE\033[0m  %s\n' "$1"; }
die()  { printf '  \033[31mSTOP\033[0m  %s\n' "$1"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VPS_DIR="$REPO_ROOT/deploy/vps"

# ---------------------------------------------------------------------------
# 1. Validate everything that can be validated for free, before any install.
# ---------------------------------------------------------------------------

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  printf '\nUsage:  bash scripts/vps-install.sh yourdomain.com\n\n'
  printf 'Point the domain'"'"'s DNS A record at this machine first.\n'
  exit 1
fi
case "$DOMAIN" in
  http://*|https://*) die "pass the bare domain (audits.example.com), not a URL" ;;
  *.*) ;;
  *) die "'$DOMAIN' does not look like a domain (no dot)" ;;
esac

# The recipient gate, same discipline as go-live.sh: shape is checked, the
# zero address is refused (well-formed, unownable, USDC reverts transfers to
# it), and the two addresses this repo knows nobody holds the key to are
# refused by name. Advertising a rail that cannot receive is the one thing
# this codebase must never do, on any host.
DEFAULT_X402_PAY_TO="0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
UNAFFIRMED_ADDRESSES="
0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256
0x32b08c5e927c69877d0fcab35618c265674922bc
"
ZERO_ADDRESS="0x0000000000000000000000000000000000000000"

PAY_TO="${X402_PAY_TO_ADDRESS:-$DEFAULT_X402_PAY_TO}"
FACILITATOR="${X402_FACILITATOR_URL:-https://facilitator.xpay.sh}"

lower() { printf '%s' "$1" | tr 'A-Z' 'a-z'; }

step "Checking the x402 recipient can actually receive money"
if ! printf '%s' "$PAY_TO" | grep -qiE '^0x[0-9a-f]{40}$'; then
  die "X402_PAY_TO_ADDRESS is not 0x + 40 hex (this one has $(( ${#PAY_TO} - 2 )) characters). Nothing was installed."
fi
if [ "$(lower "$PAY_TO")" = "$ZERO_ADDRESS" ]; then
  die "X402_PAY_TO_ADDRESS is the zero address: well-formed and unownable -- USDC reverts transfers to it. Nothing was installed."
fi
for candidate in $UNAFFIRMED_ADDRESSES; do
  if [ "$(lower "$PAY_TO")" = "$(lower "$candidate")" ]; then
    die "X402_PAY_TO_ADDRESS is an address nobody here holds the key to (see UNAFFIRMED_ADDRESSES). Nothing was installed."
  fi
done
ok "recipient ${PAY_TO:0:6}...${PAY_TO: -4} passes every gate this repo has"

# ---------------------------------------------------------------------------
# 2. Docker.
# ---------------------------------------------------------------------------

step "Checking Docker"
if ! command -v docker >/dev/null 2>&1; then
  warn "Docker is not installed; installing it via get.docker.com"
  curl -fsSL https://get.docker.com | sh || die "Docker install failed. Install it manually and re-run."
fi
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (docker compose). Install docker-compose-plugin and re-run."
ok "docker + compose present"

# ---------------------------------------------------------------------------
# 3. Write .env. Never overwrite one that exists -- it may hold live Stripe
#    keys; a re-run must be an update, not a reset.
# ---------------------------------------------------------------------------

step "Writing deploy/vps/.env"
ENV_FILE="$VPS_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  warn ".env already exists -- keeping it, updating only DOMAIN"
  if grep -q '^DOMAIN=' "$ENV_FILE"; then
    sed -i.bak "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    printf 'DOMAIN=%s\n' "$DOMAIN" >> "$ENV_FILE"
  fi
else
  {
    printf 'DOMAIN=%s\n' "$DOMAIN"
    printf 'X402_FACILITATOR_URL=%s\n' "$FACILITATOR"
    printf 'X402_PAY_TO_ADDRESS=%s\n' "$PAY_TO"
    printf 'MAX_CONCURRENT_AUDITS=%s\n' "${MAX_CONCURRENT_AUDITS:-2}"
    # Stripe rails ride along only when their variables are present in the
    # installing shell; absent, the rails stay off and are not advertised.
    for var in STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET STRIPE_PRICE_PRO \
               STRIPE_PRICE_AGENCY STRIPE_PRICE_ONEOFF_REPORT \
               MPP_STRIPE_NETWORK_PROFILE_ID MPP_TEMPO_RECIPIENT_ADDRESS \
               GEMINI_API_KEY; do
      value="$(eval "printf '%s' \"\${$var:-}\"")"
      [ -n "$value" ] && printf '%s=%s\n' "$var" "$value"
    done
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "written (mode 600)"
fi

# ---------------------------------------------------------------------------
# 4. Build and start.
# ---------------------------------------------------------------------------

step "Building and starting the stack (first build downloads Chromium; minutes, not seconds)"
docker compose -f "$VPS_DIR/docker-compose.yml" --project-directory "$VPS_DIR" up -d --build \
  || die "compose failed -- the output above says why. Nothing is half-configured; re-run after fixing it."

step "Waiting for the service to answer"
for attempt in $(seq 1 30); do
  if docker compose -f "$VPS_DIR/docker-compose.yml" --project-directory "$VPS_DIR" \
       exec -T hubvibe python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5)' 2>/dev/null; then
    ok "the node is up inside the box"
    break
  fi
  [ "$attempt" -eq 30 ] && die "the service did not come up in 5 minutes. Read: docker compose -f deploy/vps/docker-compose.yml logs hubvibe"
  sleep 10
done

step "What happens next"
printf '  1. DNS: an A record for %s must point at this machine.\n' "$DOMAIN"
printf '     Caddy obtains the HTTPS certificate automatically once it does.\n'
printf '  2. Prove it from ANY machine:\n'
printf '         curl https://%s/health\n' "$DOMAIN"
printf '  3. The first paid call -- the one that proves settlement and seeds\n'
printf '     Bazaar discovery -- from any machine with the repo:\n'
printf '         BASE=https://%s bash scripts/first-paid-call.sh\n' "$DOMAIN"
printf '\n  x402 revenue lands on-chain at %s...%s -- the wallet is the counter.\n' "${PAY_TO:0:6}" "${PAY_TO: -4}"
printf '  This box has a fixed monthly price. Nothing here can surprise-bill.\n\n'
