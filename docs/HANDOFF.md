# HubVibe — operator notes

Short, current facts an operator needs and the code does not state. The
product itself is documented in the [README](../README.md); how to run a
node is in [`deploy/vps/README.md`](../deploy/vps/README.md).

## Where things run

- The public node is `https://hubvibe-io.com`: the `deploy/vps` compose
  stack (Caddy TLS in front of uvicorn, SQLite key store) on a flat-rate
  VPS. `@` and `www` A records point at it.
- The facilitator is whatever `X402_FACILITATOR_URL` names in the box's
  `deploy/vps/.env`. Change it only with `scripts/switch-facilitator.sh`,
  which reads the live 402 afterwards and rolls back if the rail vanished.
  Both `https://facilitator.payai.network` (keyless) and
  `https://api.cdp.coinbase.com/platform/v2/x402` (needs `CDP_API_KEY_ID`
  and `CDP_API_KEY_SECRET` in `.env`) settle on Base mainnet and keep a
  Bazaar index; PayAI's index also ingests MCP records, Coinbase's does not.

## Wallets

- **Pay-to:** `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd`
  (`hubvibe.base.eth`). x402 revenue lands on-chain there; `x402 SETTLED`
  lines in the node log are the per-call ledger. Alternate owner wallet:
  `0x37555E884c5EbA10f6E816DbecEA30965B9b38C0`.
- **Payer (self-tests and listing refreshes only):** a machine-generated
  key on the box at `~/.hubvibe-wallet-key` (`HUBVIBE_WALLET_FILE`
  overrides), minted by `scripts/first-paid-call.sh --new-wallet`. It only
  ever needs USDC on Base — the payer signs off-chain and the facilitator
  pays the gas. Keep no more in it than a refresh cycle costs.
- Nothing here is ever a recipient except the pay-to. Paying from the
  pay-to to itself needs `HUBVIBE_ALLOW_SELF_PAYMENT=1`.

## Runbook (on the box, in the checkout)

- Redeploy after a merge — `git pull` alone changes nothing, the image is
  rebuilt: `git pull -q origin main && cd deploy/vps && docker compose up -d --build`
  (a `Caddyfile` change also needs `docker compose restart caddy`).
- Switch facilitator: `bash scripts/switch-facilitator.sh <url>`
- Re-seed both Bazaar indexes after a price or description change
  (~$1.05, payer → pay-to): `bash scripts/refresh-listings.sh`
- Read the money: `BASE=https://hubvibe-io.com bash scripts/payment-status.sh` (from anywhere)
- Verify a deployment from outside: `bash scripts/verify-live.sh https://hubvibe-io.com`
- Who is arriving and who pays (last ~2 days): `bash scripts/traffic-ledger.sh`
- Republish the MCP registry entry: bump `version` in `server.json` and merge;
  the "Publish MCP registry entry" workflow does the rest with OIDC.

## Machine services (/svc/*)

- The service catalog lives in `wcag-audit-engine/app/services.py`; the
  providers in `service_providers.py`. Same payment gate as the audits,
  no second payment path.
- With `.env` untouched, the keyless services are live (fetch, extract,
  rpc, market, prediction). Keys switch on the rest: `ANTHROPIC_API_KEY` /
  `GEMINI_API_KEY` / `OPENAI_API_KEY` (llm; the latter two also image+tts),
  `BRAVE_SEARCH_API_KEY` / `SERPER_API_KEY` (search; search+llm together
  enable research), `SVC_CODE_EXEC=1` (sandboxed code). `SVC_DISABLED=all`
  restores the audit-only surface exactly; restart the container after any
  of these change.
- The usage/margin ledger is SQLite beside the key store
  (`/data/hubvibe-services.db` on the box). Read it from anywhere:
  `curl -H "X-API-Key: $AUDIT_API_KEY" https://hubvibe-io.com/svc/metrics`
  — per service and provider: calls, success rate, latency, revenue,
  estimated provider cost, margin. `GET /svc/health` is the free
  availability/circuit view.
- After enabling a new capability, re-seed the Bazaar indexes
  (`refresh-listings.sh`) so agents can find it by capability.

## Settled decisions

- Software-to-software only. No signup, no plans, no checkout, no free scan.
  The website is an about-page, not a product surface.
- The price lives in one place per catalog: `_CATALOG` in
  `wcag-audit-engine/app/main.py` for the audits, `CATALOG` in
  `wcag-audit-engine/app/services.py` for the machine services; the 402,
  the agent card, the MCP tools and the Bazaar records derive from them.
  After a price or description change, every index must be re-paid to show it.
- Never advertise a rail that cannot settle: everything fails closed and
  omits what is not configured.
- A failed audit is never charged; a settlement the facilitator refuses
  withholds the audit.
- A Bazaar lists a resource only when a payment carrying its discovery
  record reaches that facilitator; there is no registration endpoint.
- A stale checkout is not a pass: `verify-live.sh` prints its own commit.
- Scripts that spend or deploy refuse to run in a temporary terminal (Cloud
  Shell) by name; run them on the box.
