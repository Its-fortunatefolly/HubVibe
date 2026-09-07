# HubVibe — session handoff

Read this first. It is the current state, the decisions already made, and
the owner's runbook. It replaces a 2,700-line chronicle (2026-08-11 to
2026-09-06) that is still in git history — `git log -p -- docs/HANDOFF.md`
— and is not to be re-grown: this file holds what a new session needs to
act, not a diary. `docs/SESSION_BRIEF.md` is the companion of standing rules.

## What this is

A machine-payable site-audit API: a tollbooth. Software agents POST a URL,
get HTTP 402 carrying the price and the rails that can settle it, pay, and
receive the audit. $0.03 per single audit, $0.10 per bundle, ~98% gross
margin. Per call is the only price. Revenue is machine traffic; nothing else.

## Live state (2026-09-07)

- **Node:** `https://hubvibe-io.com`, on the owner's Hostinger KVM
  (`2.25.172.160`, repo at `/root/HubVibe`, `deploy/vps` compose stack:
  Caddy TLS in front of uvicorn, SQLite key store). `@` and `www` A records
  point at it. 25/25 live probes from Cloud Shell on go-live day.
- **Facilitator on the box:** `https://facilitator.xpay.sh` — what
  `vps-install.sh` wrote into the box's `.env` on 2026-09-06. It settles on
  Base mainnet (v2 `eip155:8453`, v1 `base`) but keeps **no** Bazaar index
  (`/discovery/resources` answers `{"message":"Not Found"}`), so a payment
  through it registers the node nowhere.
- **Facilitator in the repo:** `https://x402.dexter.cash` — the default in
  `vps-install.sh`, `.env.example`, `first-paid-call.sh` and `go-live.sh`
  since #66/#101 (keyless, Base mainnet, indexes a resource in its
  marketplace on the first settled payment). Its `/supported` is
  UNVERIFIED from any sandbox; `scripts/switch-facilitator.sh` verifies the
  switch against the live 402 and rolls back if the rail vanishes. The box
  has NOT been switched yet.
- **Pay-to:** `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd`
  (`hubvibe.base.eth`). x402 revenue lands on-chain there and never appears
  in Stripe. The wallet is the counter; `x402 SETTLED` log lines are the
  per-call ledger. It holds 2 USDC the owner sent on 2026-09-06.
- **Rails live on the node:** x402 only. Stripe/MPP are unset on the box, so
  `other_rails` is `[]` and no key can be bought; the code keeps those rails
  fail-closed until their variables are exported.
- **First paid call:** NOT yet made. The throwaway payer `0x5bce…5d06` is
  retired (owner's instruction); the buyer is the owner's own wallet, paid
  from its recovery phrase, and a payment from `0x837C…77dd` to itself needs
  `HUBVIBE_ALLOW_SELF_PAYMENT=1`.
- **Old Cloud Run node:** still serves at `https://hubvibe-831480473793.us-south1.run.app`
  on a 2026-09-03 revision (v1.1.2), pays the same wallet, min-instances 0.
  The MCP registry still points at it (1.1.0 entry).
- **Action:** `uses: Its-fortunatefolly/HubVibe@v1` resolves this repo's `v1`
  tag at `30fce35`, whose `action.yml` defaults `base-url` to the run.app
  URL. The standalone `hubvibe-audit-action` repo's `v1`/`v1.0.0` also
  predate the domain.

## Proven, and how

- `python -m pytest tests/ -q` — read the count off the run; lint gate
  `flake8 --select=E9,F63,F7,F82` is 0.
- `python3 scripts/simulate-paid-call.py` — 48 checks, 0 failed: boots the
  real service against a stub facilitator that recovers EIP-712 signers,
  and pays it with the real x402 2.22 client three ways (v2 header, v1
  body, MCP `_meta`), proves verify-before-audit and settle-after, the
  receipt headers, replay refusal, 64/64 concurrent payers on keep-alive,
  fail-closed outage handling, and the target gate. Run it before any
  deploy that touches payments; needs a Chromium Playwright can launch.
  The shipped container image was rebuilt and paid the same three ways
  (24/24) on 2026-09-06.
- A failed audit costs nothing on every rail: x402 is settled only after
  delivery; a prepaid debit is refunded; the key an MPP top-up bought is
  returned on the 502 holding everything it bought; an MPP credential a
  failed audit consumed is accepted again on the retry
  (`tests/test_failed_audit_is_not_charged.py`).
- `bash scripts/verify-live.sh` checks a deployed node from outside
  (positional URL, then `$BASE`, then the domain); a run whose check count
  is below the current checker's is a stale checkout.

## Settled decisions — do not re-litigate

- **Both receiving wallets are the owner's, affirmed 2026-09-05:**
  `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd` (default pay-to) and
  `0x37555E884c5EbA10f6E816DbecEA30965B9b38C0` (alternate; one override,
  `X402_PAY_TO_ADDRESS=...`, on `vps-install.sh` or `go-live.sh`). Only an
  EVM address on Base mainnet can receive here; funds sent to any other
  chain are unrecoverable.
- **The first paid call is a self-payment from the owner's wallet**
  (owner's instruction, 2026-09-06): no throwaway buyer. The self-payment
  guard in `first-paid-call.sh` stays and is overridden on purpose with
  `HUBVIBE_ALLOW_SELF_PAYMENT=1`; the recovery phrase is read from
  `HUBVIBE_WALLET_MNEMONIC` or `~/.hubvibe-wallet-phrase`, the key is
  derived in memory and never written to disk, and
  `HUBVIBE_EXPECT_ADDRESS` finds the funded account among the phrase's
  first ten.
- **`0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256` and the zero address are
  never recipients.** The first is an unidentified address that was once
  deployed for weeks; the second satisfies every shape gate and can never
  receive. Both are refused by name in every gate. Shape is not payability.
- **Coinbase CDP is abandoned, not pending** (its review wants a business
  entity that does not exist). Its code is gone. Do not suggest it.
- **Dexter is the facilitator, because it indexes.** xpay.sh settles and
  indexes nothing; a facilitator's index is the only path into the Bazaar,
  so the default moved to Dexter (#66, #101). Change it on a running box
  only with `scripts/switch-facilitator.sh`: the node refuses to advertise
  a rail its facilitator cannot verify, so a bad URL leaves `/health` at
  200 while nothing can be sold.
- **Stripe does MPP, not x402.** One rail per network: Base is x402, fiat
  and Tempo are MPP. x402 revenue is on-chain and never in Stripe.
- **Human tiers are retired (2026-09-06).** No subscriptions, no reports,
  no checkout doorway on any surface. `billing.HUMAN_PLANS` is empty on
  purpose; the quota plumbing only serves keys issued before that date.
  What remains for humans is the $0.50 MPP prepaid block, where Stripe is
  configured.
- **No free scan.** An audit costs a real browser page load.
- **Never print the per-call cent price on a human-facing page.** Agents
  read exact rates from `/.well-known/agent.json` and the 402.
- **Never advertise a rail that cannot settle.** Everything fails closed
  and omits what is not configured. This is the core discipline.
- **Bazaar indexing needs a payment, not a registration.** A facilitator
  catalogs a resource when a paid `PaymentPayload` carrying the discovery
  record reaches it. The first paid call through Dexter is what indexes
  this node.
- **Payable is not discoverable, and demand is not plumbing.** Until #61
  no conforming client could pay the 402 at all; that was fixed. Do not
  restate "the constraint is demand" as established.
- **The host was never the constraint.** The identity is the domain; a
  host move is a DNS record. Cloud Run is the right shape once traffic is
  real and spiky.

## Owner runbook (one line each)

- What the money is doing, from any machine:
  `curl -fsSL https://raw.githubusercontent.com/Its-fortunatefolly/HubVibe/main/scripts/payment-status.sh | BASE=https://hubvibe-io.com bash`
- Redeploy the node after a merge, on the box:
  `cd /root/HubVibe && git pull -q origin main && cd deploy/vps && docker compose up -d --build`
- Point the box at the facilitator that indexes (verifies itself, rolls
  back on a dead rail), on the box:
  `cd /root/HubVibe && bash scripts/switch-facilitator.sh https://x402.dexter.cash`
- First paid call, from any machine with the repo (the script builds its
  own Python environment in `~/.hubvibe-venv`), with the recovery phrase in
  `~/.hubvibe-wallet-phrase`:
  `BASE=https://hubvibe-io.com HUBVIBE_EXPECT_ADDRESS=0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd HUBVIBE_ALLOW_SELF_PAYMENT=1 bash scripts/first-paid-call.sh`
  The receipt line and the Basescan link are the proof; the script then
  reads Dexter's index.
- Move this repo's action tag to current main so `@v1` uses the domain:
  `git tag -f v1 origin/main && git push -f origin v1` (from a clone of
  HubVibe; the push output must say `HubVibe.git`).
- Regenerate and retag the standalone action repo:
  `bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action`, then in
  `hubvibe-audit-action`: `git tag -f v1 origin/main && git tag v1.0.1 origin/main && git push -f origin v1 v1.0.1`.
  A Marketplace Release is UI-only.
- Republish the MCP registry entry (1.2.0, domain URL), from a clone:
  `./mcp-publisher login github` then `./mcp-publisher publish`. Bump
  `version` in `server.json` before any later republish; the registry
  rejects a version it already serves.
- Optional Cloud Run stopgap until the registry is republished:
  `bash scripts/repair-and-deploy.sh` (pins `PUBLIC_BASE_URL` to the
  service URL, min-instances 0). `DELETE_IDLE=1 bash scripts/cost-sweep.sh`
  first if anything idle could bill.
- Read a refused payment's reason on Cloud Run: `bash scripts/x402-log.sh`;
  on the box: `docker compose logs hubvibe | grep x402`.

## Hard-won rules

- Never claim a live fact without seeing its output. The sandbox cannot
  reach the node, run.app, Stripe, either facilitator or Base RPC; it can
  reach PyPI, the GitHub API and the MCP registry.
- Setting env vars is not a deploy. `gcloud run services update` keeps the
  old image; deploy source (`repair-and-deploy.sh`), or `docker compose up
  -d --build` on the box. Never edit the facilitator in `.env` by hand.
- When a surface is consumed by someone else's parser, test it with their
  parser (the x402 client, the Bazaar validator, `mppx`, the MCP SDK).
  Presence is not acceptance — this shipped four bugs green.
- Prove every new test by reintroducing the bug and watching it go red.
- A check that silently skips is worse than none; `importorskip` on a
  module that guards a shipped artifact converts a missing pin into a
  green run. Pin, import hard, assert the pin.
- Green tests do not prove a deploy; `verify-live.sh` does. A stale
  checkout is not a pass. A tool must check the thing it was asked to
  check: two scripts silently read production instead of `$BASE`.
- A substring match on a network id is a bug: `eip155:84532` (Base
  Sepolia) contains `eip155:8453`. Match with delimiters.
- `gcloud --format=flattened` pads names; always `--format=json`. Secret
  Manager `latest` is the highest version number regardless of state;
  repair additively, never by disabling. Write secrets with `printf '%s'`.
- Say which repo a git command runs in; the action repo and this one have
  both received each other's tags. Read the push output.
- The owner is often on a phone: one short command per line.

## Tooling map

| Script | Purpose | Where |
|---|---|---|
| `scripts/vps-install.sh` | the whole service on a flat-rate box, one command; gates the recipient and the facilitator before writing anything | box |
| `scripts/switch-facilitator.sh` | change the facilitator on a running box: edit `.env`, restart, read the live 402, roll back if the rail vanished | box |
| `scripts/payment-status.sh` | wallets, live 402, recipient match, payer readiness, verdict | anywhere |
| `scripts/first-paid-call.sh` | one real $0.03 x402 payment from the owner's wallet, with preflight, receipt and index check | anywhere with the phrase |
| `scripts/simulate-paid-call.py` | the whole paid path locally, for free | dev |
| `scripts/verify-live.sh` | end-to-end checks of a deployed node | anywhere |
| `scripts/probe-facilitators.sh` | which facilitators this library can use, and which keep a Bazaar index | anywhere |
| `scripts/publish-action-repo.sh` | generates the standalone Marketplace action repo verbatim | dev |
| `scripts/repair-and-deploy.sh`, `go-live.sh`, `launch.sh` | Cloud Run source deploy with preflight; `go-live` resolves recipients first; `launch` sweeps costs then deploys | Cloud Shell |
| `scripts/repair-secrets.sh`, `cost-sweep.sh`, `snapshot-state.sh`, `measure-call-cost.sh`, `x402-log.sh`, `lib-api-key.sh` | Cloud Run operations: secrets, idle billing, live config snapshot (reads the box's facilitator), per-call cost, payment log, key resolution | Cloud Shell |
| `scripts/prospect_scan.py`, `draft_outreach.py` | find sites with failing audits and draft outreach that only claims what a run backs | dev |
