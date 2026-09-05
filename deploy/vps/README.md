# Run HubVibe on any flat-rate box

The whole tollbooth — every paid route, the MCP endpoint, HTTPS — on one
machine with a fixed monthly price. Nothing metered, nothing that can
surprise-bill, and no Google dependency: the per-call rails (x402, MPP)
never needed one, and `KEY_STORE=sqlite` gives the API-key store a local
file instead of Firestore.

A 4 GB / 2 vCPU box (≈ $4–5/month) runs 2 concurrent audits — roughly
30–50k audits/day of capacity, which at $0.03/call is far more than the
box costs. Raise `MAX_CONCURRENT_AUDITS` and `mem_limit` together when
revenue outgrows it, or add a second box behind the same domain.

## The two manual steps

1. Buy a domain (any registrar, ≈ $10/yr). **The domain is the point**: every
   manifest, registry entry and agent cache holds the URL, so owning the
   domain means the host underneath can change with a DNS record.
2. Create a DNS **A record** for it pointing at the box's IP.

## Then, on the box

```bash
git clone https://github.com/Its-fortunatefolly/HubVibe
cd HubVibe
bash scripts/vps-install.sh yourdomain.com
```

That validates the payment recipient before touching anything, installs
Docker if missing, writes `deploy/vps/.env`, builds, starts, and waits for
health. Caddy fetches and renews the HTTPS certificate itself once DNS
resolves.

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

```bash
cd HubVibe/deploy/vps
docker compose logs -f hubvibe          # the node's log (x402 SETTLED lines = revenue)
docker compose up -d --build            # deploy a new version after git pull
docker compose restart hubvibe          # bounce the service
```

Both containers restart on failure and on reboot (`restart: unless-stopped`).
The key store lives in the `hubvibe_data` volume; back it up with
`docker run --rm -v vps_hubvibe_data:/data alpine tar czf - /data > keys-backup.tgz`
if the Stripe/prepaid rails carry balances you care about. x402 revenue
never touches the box — it lands on-chain in the pay-to wallet.

## Going back (or up) later

Cloud Run remains the right shape once traffic is real and spiky —
scale-to-zero and burst elasticity. Because the identity is the domain,
returning is: deploy there, flip the A record. Nothing else changes.
