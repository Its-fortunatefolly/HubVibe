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
- **Facilitator: `https://facilitator.payai.network`** — keyless, lists
  both rails the node advertises (v1 `base`, v2 `eip155:8453`), declares
  the `bazaar` extension, keeps a `/discovery/resources` index that
  x402scan aggregates, and its settlement signers held 0.014–0.027 ETH when
  read on 2026-09-12. The repo defaults (`vps-install.sh`, `.env.example`,
  `first-paid-call.sh`, `go-live.sh`) are PayAI as of this change. The box
  was read on 2026-09-12 as still on `https://facilitator.xpay.sh` — the
  facilitator that took the first real paid call on 2026-09-11 (`x402
  SETTLED` in the box log, tx `0xc03e7d…634a58`, pay-to 4.25 → 4.28 USDC)
  but keeps no index — and is moved with the runbook line below; confirm
  with `grep X402_FACILITATOR_URL /root/HubVibe/deploy/vps/.env` on the box.
  `https://x402.dexter.cash` indexes but **its settlement signer
  `0x402Feee072D655B85e08f1751AF9ddbCd249521f` is out of gas** (0.00000013
  ETH on Base, read 2026-09-12): every settle through it fails, and until
  this change a failed settle delivered the audit free. Before trusting any
  facilitator, `probe-facilitators.sh` says who settles and indexes, and one
  read of its signer says whether it can:
  `curl -s -X POST https://mainnet.base.org -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"eth_getBalance","params":["<signer from /supported>","latest"]}'`
  `x402.org/facilitator` is Sepolia-only (the probe script's substring
  match used to call it a mainnet settler; fixed).
- **Pay-to:** `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd`
  (`hubvibe.base.eth`). x402 revenue lands on-chain there and never appears
  in Stripe. The wallet is the counter; `x402 SETTLED` log lines are the
  per-call ledger. It holds 2 USDC the owner sent on 2026-09-06.
- **Rails live on the node:** x402 on Base, plus x402 USDC on Solana once
  `X402_SOLANA_PAY_TO_ADDRESS` (the Base app's Solana address for
  `hubvibe.base.eth`) is in the box's `.env` and the stack is rebuilt --
  the v2 header then carries both rails, gated on the facilitator listing
  Solana with a fee payer. Stripe/MPP are unset on the box, so
  `other_rails` is `[]` and no key can be bought; the code keeps those rails
  fail-closed until their variables are exported.
- **First paid call: ATTEMPTED 2026-09-08, and it did NOT settle.** Revenue
  is still zero. The owner funded the payer and ran the script. Verify
  passed, the audit ran and was delivered — and the node's own response body
  carried
  `billing_warning: payment settlement failed after the audit ran; this call
  was not charged`. So everything up to settle is proven for the first time
  (signature, facilitator verify, route, audit) and **settle is the one step
  still unexercised.** The node delivers an unpaid audit on purpose (the
  lesser evil versus charging for undelivered work) and flags it; that flag
  is the authority on whether money moved.
  **Next, on the box — which of three ways it failed is in the node's log:**
  `cd /root/HubVibe/deploy/vps && docker compose logs --since 3h 2>&1 | grep -i "x402 settle" | tail -20`
  `settle_sync` fails three ways and logs three different sentences —
  `REFUSED after delivery` (the facilitator declined), `TIMED OUT after
  delivery` (no answer; status unknown, do NOT re-run), and `FAILED before
  the facilitator could answer` (an exception on our side, logged through
  `_log_rejection`). `x402 settle` is the only token common to all three.
  If compose prints nothing, its project name may not match the running
  stack — ask the daemon directly:
  `docker logs --since 3h $(docker ps -qf name=hubvibe | head -1) 2>&1 | grep -i x402 | tail -30`
  Three of our own tools misreported this run and are fixed: the script
  announced `settled $0.03` while printing that body (it read the HTTP
  status, never the body — it now exits 1 and says the node was not paid);
  it blamed the missing PAYMENT-RESPONSE receipt on a stale deployment when
  a settle that never happened simply has no hash (that would have sent the
  owner rebuilding a healthy node); and it handed over a grep for one of the
  three failure sentences, so the owner got silence and the box looked
  broken (2026-09-08). `test_the_settle_diagnostic_matches_every_failure_log`
  now pins the pattern against `x402_payments.py`.
  **And a real money bug was found while diagnosing it.** `settle_sync`'s
  `except TimeoutError` — the branch that exists to say "unknown, the money
  may still have moved" — caught only the node's own 45s guard. httpx's
  timeouts are **not** builtin `TimeoutError` (`ReadTimeout` →
  `TimeoutException` → `TransportError` → … → `Exception`), and the client's
  default read timeout is 30s against that 45s guard, so httpx always fired
  first. Every mid-flight settle timeout was recorded `refused` and told the
  payer **"this call was not charged"** about a transfer that may have
  completed — an invitation to pay twice for one audit. Fixed by classifying
  on whether the request could have reached the facilitator: read/write
  timeouts and a connection dropped mid-exchange are `unknown`; connect and
  pool failures never sent it and stay `refused`. This is a live candidate
  for what the owner actually hit.
- **Bazaar indexing is downstream of that**, so its zero is expected for
  now. The index CHECK was independently wrong and is fixed: it matched the
  pay-to address case-sensitively and looked for nothing else, so a
  facilitator storing the address lowercased, or keying its record by
  resource URL, reads as "not indexed" indistinguishably from never being
  listed. It now matches case-insensitively, accepts the host, and reads the
  index belonging to the node's own `X402_FACILITATOR_URL` instead of a
  compiled-in default.
- **The payer wallet.** The throwaway `0x5bce…5d06` is retired (owner's
  instruction). The payer
  is now `0x104feA79F30b4fB4Da86B6D65951217F914bdd35`, created on the box by
  `first-paid-call.sh --new-wallet` (`Account.create()`, key written mode
  600). **The owner does not recognise this address and has said so
  (2026-09-08): it is not a wallet they hold anywhere.** That is accurate —
  it is a machine-held throwaway whose only key sits in a file on the
  server, existing solely because a self-test needs a payer distinct from
  the pay-to (you cannot pay yourself). Do not describe it as "the owner's
  wallet"; do not put more in it than one call costs. Its key is at
  `~/.hubvibe-wallet-key` (`HUBVIBE_WALLET_FILE` overrides). To prove the
  box controls it:
  `~/.hubvibe-venv/bin/python -c "from eth_account import Account; print(Account.from_key(open('/root/.hubvibe-wallet-key').read().strip()).address)"`.
  It is funded and its money is still there — the 2026-09-08 attempt was
  refused at settle, so nothing left the wallet. Keep ~$0.25 USDC **on
  Base** in it per attempt. The payer needs no ETH — it
  signs off-chain and the facilitator pays the gas — but the transfer that
  funds it is an ordinary send out of the owner's own wallet, which covers
  its own gas like any other transfer; that distinction is the one the
  "NO ETH NEEDED" lines used to blur. There is no
  recovery phrase for the owner's own wallet — do not build for one.
- **That payer address is a WALLET, not an account id.** `eth_account`
  generated it on the box and `~/.hubvibe-wallet-key` (mode 600) is its
  private key. Whoever holds that file controls anything sent to it — which
  today is the box, and nobody else. The address exists identically on every
  EVM chain, so USDC sent on Ethereum, Arbitrum, Optimism or Polygon by
  mistake is sitting at the same address on that chain: not lost, just not
  on Base, and spendable with that key.
- **The balance check reads ONE token on ONE chain:** native USDC
  (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) on Base mainnet. Bridged
  USDbC, ETH, or USDC on any other chain all read as `$0.00`, which looks
  exactly like "never received". `bash scripts/find-my-money.sh` reads the
  address across chains; `bash scripts/go.sh --check` reads the Base USDC
  balance and prints the file that controls the address. Neither spends
  anything. An explorer's default Transactions tab is ALSO empty for this
  wallet — it holds no ETH, so it has no normal transactions and the USDC
  sits under the ERC-20 tab.
- **Base app registration:** the homepage serves
  `<meta name="base:app_id" content="6a83832901463168d7e651ca" />`, the id
  the owner's Add Domain dialog asked for on 2026-09-07. It replaced
  `6a8383066ea1f57fed333625` (#84): the two are 35 seconds apart as
  ObjectIDs, so the app was created twice and setup is open on the second.
  **The box is already serving it** — the owner's
  `curl -s https://hubvibe-io.com/ | grep base:app_id` returned the new id on
  2026-09-08, which also proves the box carries the current build. The
  deploy no longer gates this: pressing Register is the whole remaining step.
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
  deploy that touches payments; re-run green 2026-09-08 with the settle
  fixes in. The shipped container image was rebuilt and paid the same three
  ways (24/24) on 2026-09-06.
  **It needs a Chromium the installed Playwright can drive, and version
  skew is the usual reason it "fails".** The repo pins `playwright==1.48.0`,
  which wants chromium **1140**; sandboxes and CI images commonly ship a
  newer build (1194 here) with browser downloads disabled. Symlinking one
  revision to the other gets past the *missing executable* error and then
  dies with `Target page, context or browser has been closed` — a launch
  the client cannot drive, which reads like a broken audit engine. Install
  the client that matches the browser instead (1194 ⇒ `playwright==1.56.0`)
  in the throwaway venv; leave the pin alone, since the box installs its own
  matching pair. Cost an hour on 2026-09-08.
- A failed audit costs nothing on every rail, and a refused settlement
  delivers nothing: x402 is settled only once the audit has run, and a
  settle the facilitator refuses answers with the 402 (reason on the body)
  instead of the audit; a prepaid debit is refunded; the key an MPP top-up bought is
  returned on the 502 holding everything it bought; an MPP credential a
  failed audit consumed is accepted again on the retry
  (`tests/test_failed_audit_is_not_charged.py`).
- `bash scripts/verify-live.sh` checks a deployed node from outside
  (positional URL, then `$BASE`, then the domain); a run whose check count
  is below the current checker's is a stale checkout.
- **The repo is public and is now guarded.** A scan of the working tree and
  of all git history found no credential was ever committed — every history
  match is a redaction fixture (`sk_live_ABC123XYZ`) that exists to prove
  the redactor. The Stripe account id and three `buy.stripe.com` links for
  the retired plans were removed from the tree anyway.
  `tests/test_no_secrets_in_repo.py` scans every tracked file for ten
  credential shapes plus those two private identifiers, allows only the
  ERC-20 Transfer topic0 and the fixtures under `tests/`, and asserts no
  `.env`, wallet key, `.pem` or `id_rsa` is tracked and that `.gitignore`
  covers them. Wallet addresses stay: an EVM address is public by
  construction and the code needs the pay-to. History still holds the
  account id and the links — not credentials, and not worth rewriting a
  public repo's history over; deactivate the links in Stripe if they should
  be dead.

## Settled decisions — do not re-litigate

- **Both receiving wallets are the owner's, affirmed 2026-09-05:**
  `0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd` (default pay-to) and
  `0x37555E884c5EbA10f6E816DbecEA30965B9b38C0` (alternate; one override,
  `X402_PAY_TO_ADDRESS=...`, on `vps-install.sh` or `go-live.sh`). Only an
  EVM address on Base mainnet can receive here; funds sent to any other
  chain are unrecoverable.
- **The first paid call is funded by the owner, from the box's own payer
  wallet** (owner's instruction, 2026-09-07): no throwaway buyer, and **no
  recovery phrase — the owner does not have twelve words.** The phrase path
  in `first-paid-call.sh` stays for anyone who does, but the supported route
  is the box wallet `0x104feA…dd35` funded with USDC on Base. The
  self-payment guard stays and is overridden on purpose with
  `HUBVIBE_ALLOW_SELF_PAYMENT=1` only when paying from `0x837C…77dd` itself.
- **`0x2b3bb4feb0c8af003da4a46e8c65e25bd6f10256` and the zero address are
  never recipients.** The first is an unidentified address that was once
  deployed for weeks; the second satisfies every shape gate and can never
  receive. Both are refused by name in every gate. Shape is not payability.
- **Coinbase CDP is back (2026-09-12).** The first account was blocked on a
  DBA review; the owner opened a new one. CDP's facilitator
  (`https://api.cdp.coinbase.com/platform/v2/x402`) is the one behind the
  x402 Bazaar -- the index the official x402 SDKs' discovery reads by
  default -- and it needs a CDP API key: `CDP_API_KEY_ID` and
  `CDP_API_KEY_SECRET` in the box's `deploy/vps/.env`, then a rebuild, then
  `switch-facilitator.sh` to the CDP URL. The node signs one JWT per
  endpoint with `cdp-sdk` and sends the pair only to a Coinbase host.
- **PayAI is the facilitator, because it settles AND indexes.** A
  facilitator's index is the only path into a Bazaar, and a facilitator
  that cannot settle sells nothing: Dexter's signer ran out of gas (read
  2026-09-12), xpay.sh keeps no index, PayAI does both keylessly. The
  default was Dexter from #66/#101 until this change. Change it on a running box
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
  record reaches it. The first paid call through PayAI is what indexes
  this node.
- **Payable is not discoverable, and demand is not plumbing.** Until #61
  no conforming client could pay the 402 at all; that was fixed. Do not
  restate "the constraint is demand" as established.
- **The host was never the constraint.** The identity is the domain; a
  host move is a DNS record. Cloud Run is the right shape once traffic is
  real and spiky.

## Owner runbook (one line each)

**Which terminal.** Anything that changes the node runs **on the box** —
Hostinger's Browser terminal, or `ssh root@2.25.172.160`. Google Cloud Shell
is a temporary terminal with no stack in it: `vps-install.sh`,
`switch-facilitator.sh` and `first-paid-call.sh` all refuse to run there by
name. Read-only checks
(`payment-status.sh`, `verify-live.sh`, `curl`) run from anywhere, Cloud
Shell included. Each line below says which it is.

- [anywhere] What the money is doing:
  `curl -fsSL https://raw.githubusercontent.com/Its-fortunatefolly/HubVibe/main/scripts/payment-status.sh | BASE=https://hubvibe-io.com bash`
- [ON THE BOX] Redeploy the node after a merge — this is what puts merged
  code and a changed homepage in front of the world; `git pull` alone does
  nothing, the image has to be rebuilt:
  `cd /root/HubVibe && git pull -q origin main && cd deploy/vps && docker compose up -d --build`
- [ON THE BOX] Point the node at the facilitator that indexes (verifies
  itself against the live 402, rolls back on a dead rail):
  `cd /root/HubVibe && bash scripts/switch-facilitator.sh https://facilitator.payai.network`
- [ON THE BOX] Move to Coinbase's facilitator for the x402 Bazaar, once
  `CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` are in `deploy/vps/.env` and
  the stack has been rebuilt:
  `cd /root/HubVibe && bash scripts/switch-facilitator.sh https://api.cdp.coinbase.com/platform/v2/x402`
- **Prove the node can take a customer's money — costs nothing, from any
  machine:** `bash scripts/verify-live.sh`
  This is the readiness check, and it needs no wallet, no funds, and no
  address to trust. It calls the live endpoint unpaid, reads the 402, and
  validates every `accepts[]` entry against the fields
  `PaymentRequirementsV1` requires — the exact validation a customer's x402
  client runs before it will sign anything. If this passes, a funded
  customer can pay. That is the fact that matters; **it does not depend on
  the owner holding money anywhere.**

  Why it does not: `x402_payments.is_configured()` gates the rail on a
  facilitator URL and a well-formed pay-to address, and nothing else
  (`wcag-audit-engine/app/x402_payments.py:152`). The pay-to address only
  ever *receives*. An empty wallet does not close the till.

- **Optional — the owner's own self-test.** `scripts/go.sh` buys one audit
  from the node so there is a receipt on Basescan with the owner's name on
  it. This is a demonstration, not a prerequisite: skip it and customers can
  still pay.

  Read this before funding anything. `go.sh` calls
  `first-paid-call.sh --new-wallet`, which **generates a fresh random
  private key** into a file on the box and derives an address from it. That
  address (`0x104feA…dd35` on the current box) is not anyone's known wallet
  and there is no reason to recognise it — it did not exist until a script
  minted it. Do not send funds to an address on anyone's say-so, this
  document included. Print it from the key file on the box first:
  `bash scripts/go.sh` shows it on the `payer` line before it asks for
  anything. Fund only an address the box shows you.

  Two other routes exist and cost the owner no new trust: pay from
  `0x837C…77dd` itself with `HUBVIBE_ALLOW_SELF_PAYMENT=1` (round-trips the
  money back to the same wallet, but puts that wallet's key on the server —
  do not do this with a wallet holding real balance), or import the box
  key into a wallet app so the address stops being a stranger. Either way,
  `verify-live.sh` above already proves the rail; this only adds a receipt.
- Finish the Base app registration. The live page already carries the id
  (confirmed 2026-09-08), so nothing needs deploying first: enter
  `hubvibe-io.com` in the dashboard's Add Domain box and press Register. If
  the page is ever changed, re-confirm with
  `curl -s https://hubvibe-io.com/ | grep base:app_id` before pressing it —
  Base fetches the live page, so pressing it ahead of a deploy verifies
  nothing.
- Move this repo's action tag to current main so `@v1` uses the domain:
  `git tag -f v1 origin/main && git push -f origin v1` (from a clone of
  HubVibe; the push output must say `HubVibe.git`).
- Regenerate and retag the standalone action repo:
  `bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action`, then in
  `hubvibe-audit-action`: `git tag -f v1 origin/main && git tag v1.0.1 origin/main && git push -f origin v1 v1.0.1`.
  A Marketplace Release is UI-only.
- Republish the MCP registry entry (1.2.0, domain URL): GitHub → Actions →
  "Publish MCP registry entry" → Run workflow. It logs in with the
  workflow's own OIDC token (no browser, no local install) and fails if the
  registry does not serve `server.json`'s version afterwards. It also runs
  by itself whenever `server.json` changes on main. Bump `version` before
  any later republish; the registry rejects a version it already serves.
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
- Every script that touches the box refuses Cloud Shell by name. A temporary
  terminal with its own home directory turns "wrong machine" into a wallet
  error, a missing `.env`, or a stale five-word file — the symptom, never
  the cause. `vps-install.sh` and `first-paid-call.sh` both refuse; any new
  box script must too. And a file that is not what it claims to be is litter
  to step over with a named warning, not a reason to abort a run that has a
  working key beside it.
- Say which repo a git command runs in; the action repo and this one have
  both received each other's tags. Read the push output.
- The owner is often on a phone: one short command per line.

## Tooling map

| Script | Purpose | Where |
|---|---|---|
| `scripts/vps-install.sh` | the whole service on a flat-rate box, one command; gates the recipient and the facilitator before writing anything | box |
| `scripts/switch-facilitator.sh` | change the facilitator on a running box: edit `.env`, restart, read the live 402, roll back if the rail vanished | box |
| `scripts/payment-status.sh` | wallets, live 402, recipient match, payer readiness, verdict | anywhere |
| `scripts/go.sh` | **the one command.** Finds or makes the payer wallet, prints the address to fund, waits for the money on-chain, then makes the paid call. Unattended: the owner's part is one transfer, whenever | box |
| `scripts/find-my-money.sh` | "the box says it never arrived" — reads one address across Base, Ethereum, Arbitrum, Optimism and Polygon, native coin and USDC/USDbC/USDT, and says which chain holds it. Never reports an unreachable chain as an empty one | anywhere |
| `scripts/first-paid-call.sh` | one real $0.03 x402 payment from the box's payer wallet, with preflight, receipt and index check; an empty wallet is reported as an empty wallet, not a broken rail | box |
| `scripts/simulate-paid-call.py` | the whole paid path locally, for free | dev |
| `scripts/verify-live.sh` | end-to-end checks of a deployed node | anywhere |
| `scripts/probe-facilitators.sh` | which facilitators this library can use, and which keep a Bazaar index | anywhere |
| `scripts/publish-action-repo.sh` | generates the standalone Marketplace action repo verbatim | dev |
| `scripts/repair-and-deploy.sh`, `go-live.sh`, `launch.sh` | Cloud Run source deploy with preflight; `go-live` resolves recipients first; `launch` sweeps costs then deploys | Cloud Shell |
| `scripts/repair-secrets.sh`, `cost-sweep.sh`, `snapshot-state.sh`, `measure-call-cost.sh`, `x402-log.sh`, `lib-api-key.sh` | Cloud Run operations: secrets, idle billing, live config snapshot (reads the box's facilitator), per-call cost, payment log, key resolution | Cloud Shell |
| `scripts/prospect_scan.py`, `draft_outreach.py` | find sites with failing audits and draft outreach that only claims what a run backs | dev |
