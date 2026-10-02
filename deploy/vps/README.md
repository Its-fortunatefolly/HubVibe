# Run HubVibe on any flat-rate box

The whole tollbooth — every paid route, the MCP endpoint, HTTPS — on one
machine with a fixed monthly price. Nothing metered, nothing that can
surprise-bill, and no Google dependency: the per-call rails (x402, MPP)
never needed one, and `KEY_STORE=sqlite` gives the API-key store a local
file instead of Firestore.

A 4 GB / 2 vCPU box (≈ $4–5/month) runs 2 concurrent audits — roughly
30–50k audits/day of capacity, which at $0.05/call is far more than the
box costs. Raise `MAX_CONCURRENT_AUDITS` and `mem_limit` together when
revenue outgrows it, or add a second box behind the same domain.

## The manual steps

1. Buy a domain (any registrar, ≈ $10/yr). **The domain is the point**: every
   manifest, registry entry and agent cache holds the URL, so owning the
   domain means the host underneath can change with a DNS record.
2. Buy the box and note its public IP (Hostinger: the VPS overview page).
   Make sure its firewall allows **80 and 443** inbound (Hostinger's VPS
   firewall is off by default; if you enabled one, add both ports).
3. Create two DNS **A records** pointing at that IP: `@` (the bare domain)
   and `www`. Caddy serves the bare domain and redirects `www` to it.

## Then, on the box -- and only on the box

Open the box's terminal (Hostinger: VPS → **Browser terminal**; or
`ssh root@YOUR_VPS_IP`). **Not Google Cloud Shell** -- that is a temporary
terminal, not a server, and the installer refuses to run there.

```bash
git clone https://github.com/Its-fortunatefolly/HubVibe
cd HubVibe
bash scripts/vps-install.sh yourdomain.com
```

That validates the payment recipient before touching anything, installs
Docker if missing, writes `deploy/vps/.env`, and builds and starts the node
through `scripts/deploy-box.sh`, which waits for health. Caddy fetches and
renews the HTTPS certificate itself once DNS resolves. Re-running the same
command later is safe: `.env` is kept, and a box already serving is
redeployed with no downtime.

To also enable the Stripe rails, export their variables in the same shell
before running the installer (see `.env.example`); absent, those rails stay
off and are never advertised — the same fail-closed rule as everywhere else.

## Prove it, then get paid

```bash
curl https://yourdomain.com/health
BASE=https://yourdomain.com bash scripts/first-paid-call.sh
```

The first paid call proves settlement end to end and is what seeds the
Bazaar discovery index — a facilitator catalogs this service when a real
payment carries its discovery record.

## Day-2 operations

Every change to what runs -- a new commit, an edited `.env`, a new
Caddyfile -- goes through one command, from the repo root on the box:

```bash
git pull
bash scripts/deploy-box.sh              # deploy the checkout, no downtime
bash scripts/deploy-box.sh status       # which build serves, which stands by
bash scripts/deploy-box.sh rollback     # previous build back in front
```

The node runs as two identical copies, `hubvibe-blue` and `hubvibe-green`.
One serves; a deploy builds the commit as the other, starts it beside the
live one, and checks it with the same tests the hourly monitor runs
(`scripts/box-checks.sh`: verify-live, a real browser audit, the bee sweep).
Only a build that passes takes traffic: Caddy moves to it at its next
health probe, requests already running finish on the old copy, and the old
copy then stops after finishing everything it holds. A build that fails
never sees a request. If the public node does not answer correctly after
the switch, traffic goes straight back. Each run leaves a line in
`/root/hubvibe-deploy.log`.

Do not use `docker compose up -d --build` or `restart` on the node: they
would bypass the gate (a bare `up -d` now starts only Caddy, by design).

```bash
bash scripts/box-exec.sh <command>      # run a command in the live copy
cd deploy/vps
docker compose logs -f hubvibe-blue hubvibe-green     # the node's log (x402 SETTLED lines = revenue)
docker compose logs hubvibe-blue hubvibe-green | grep -c "x402 SETTLED"   # paid calls since each copy started
```

Durable records never depend on these logs: every sale is in the purchase
book and the worker ledger on the `hubvibe_data` volume.

From any machine, `BASE=https://yourdomain.com bash scripts/payment-status.sh`
reads the wallet balances, the live 402 and whether the recipient is yours.

The live copy and Caddy restart on failure and on reboot (`restart:
unless-stopped`); the copy standing by stays stopped until the next deploy.
The key store lives in the `hubvibe_data` volume; back it up with
`docker run --rm -v vps_hubvibe_data:/data alpine tar czf - /data > keys-backup.tgz`
if the Stripe/prepaid rails carry balances you care about. x402 revenue
never touches the box — it lands on-chain in the pay-to wallet.

## Going back (or up) later

Cloud Run remains the right shape once traffic is real and spiky —
scale-to-zero and burst elasticity. Because the identity is the domain,
returning is: deploy there, flip the A record. Nothing else changes.
