# HubVibe — session handoff

Read this first. It is the state of the business and the decisions already
made, so a new session does not re-litigate them or rebuild traps that were
already removed.

## What this is

A machine-payable site auditing service. Software agents call an HTTP
endpoint, get a 402 carrying the price and how to pay, settle it, and receive
a result — no account, no signup, no human in the loop. There is also a human
plan tier, but **the A2A machine API is the product**, not a SaaS with an API
bolted on.

The goal is revenue. Treat "does this make money / can a buyer actually pay"
as the test that outranks everything else.

- **Live service:** https://hubvibe-831480473793.us-south1.run.app
- **Public site:** https://its-fortunatefolly.github.io/HubVibe/
- **Repo:** https://github.com/Its-fortunatefolly/HubVibe (public)
- **Working branch:** `claude/a2a-search-discovery-wbtk1g`

## Current state — all verified live, not assumed

| | |
|---|---|
| Cloud Run | project `resolver-time`, service `hubvibe`, region `us-south1` |
| Tests | 166 passed, 1 skipped; flake8 clean |
| Live checks | `bash scripts/verify-live.sh` → 27 passed, 0 failed |
| Stripe account | `acct_1U28tvDA21T9EAQB`, **zero outstanding requirements** |
| Payouts | daily → SUTTON BANK ····1444 |
| Webhook | `/billing/webhook`, enabled, `checkout.session.completed` |
| MCP registry | `io.github.Its-fortunatefolly/hubvibe` 1.1.0, **active** |
| Payment rails live | `mpp-stripe`, `mpp-tempo`, `stripe_api_key` |
| x402 | code complete, **dormant** — needs a facilitator credential |

### Stripe price IDs (verified against the live account)

| Plan | Price ID | Amount |
|---|---|---|
| Single report | `price_1U34JXDA21T9EAQB8IfiGxII` | $29.99 one-time |
| Pro | `price_1U34LiDA21T9EAQB3LK5dS0I` | $79.00/mo |
| Agency | `price_1U34PXDA21T9EAQB7aMyADgE` | $249.00/mo |

Machine rates: **$0.03** per single audit, **$0.10** per bundle.

## Decisions already made — do not reverse these

1. **Never print the per-call cent price on a human-facing page.** Not on the
   landing page, not in meta descriptions, not in JSON-LD `lowPrice`. A
   `$0.03` sitting above a `$79` plan invites one subtraction and makes every
   plan look absurd. This exact mistake has been made and fixed twice. Agents
   read exact rates from `/.well-known/agent.json` and the 402 challenge,
   which are generated from the same catalog the routes charge from. Three
   tests enforce this.
2. **Human plans are priced per site watched, never per scan.** Denominating
   in scans makes them directly comparable to the machine rate and strictly
   worse than paying per call.
3. **No free scan.** An audit costs a real browser page load.
4. **Never advertise a rail or plan that cannot settle.** Everything is
   fail-closed and omits what is not configured, rather than advertising it
   with a null recipient. This is the core discipline of the codebase.
5. **A2A leads.** The machine API section precedes the plans on the landing
   page. The plans are the secondary path for humans who don't want to build
   an integration.

## How to deploy

One command. It reads the live config, repairs only what is wrong, deploys,
and verifies:

```bash
cd ~/HubVibe-deploy4 && git fetch origin main && git reset --hard origin/main
bash scripts/repair-and-deploy.sh
```

It is safe to re-run. It refuses to point at a Stripe secret that does not
exist, and it strips placeholder x402 values before they can reach a live
revision.

## What is left

**1. x402 + Bazaar discovery (the remaining traffic lever).**
Everything is built and tested; it needs one credential. x402 is not only a
payment rail — facilitators catalog x402 resources by reading a Bazaar
discovery extension off their 402s, and agents shop that index *by
capability*. Without it, agents can only find HubVibe by name via the MCP
registry.

Coinbase CDP is the facilitator to use: mainnet, first 1,000 settlements a
month free then ~$0.001, and it is what feeds the Bazaar. Get a free API key
at https://portal.cdp.coinbase.com then set:

```
X402_FACILITATOR_URL = https://api.cdp.coinbase.com/platform/v2/x402
X402_PAY_TO_ADDRESS  = <Stripe-custodied crypto deposit address>
CDP_API_KEY_ID       = <key id>
CDP_API_KEY_SECRET   = <Secret Manager — it is a private signing key>
```

`_CdpAuthProvider` in `app/x402_payments.py` already signs the per-request
JWTs correctly, verified against the real SDK.

**2. Marketplace listing for the GitHub Action.** `action.yml` is now at the
repo root, so `uses: Its-fortunatefolly/HubVibe@v1` already works. The
*listing* still needs a separate repo — see the lesson below — via
`bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action`.

**3. Register with Glama.** `glama.json` is in place; the directory still has
to be pointed at the repo once.

**4. Cosmetic:** the card statement descriptor reads `HUBEVIBE` (extra E).
Dashboard → Settings → Payments.

## The growth math, stated plainly

The build side of "more machine traffic" is now essentially done. What is not
done, and what no amount of code closes, is demand.

At $0.03–$0.10 a call, **$1M of revenue is 10–33M paid calls; a multi-million
run rate is 100M+.** Spread over a year, 100M calls is ~3 paid calls every
second, continuously, from a base that is currently approximately zero paying
machine callers.

**Per-call compute is not the risk.** An earlier session flagged it as the
biggest unknown; that was wrong, and worth correcting so nobody re-opens it.
Run `bash scripts/measure-call-cost.sh` for the real figure, but the ceiling
is easy to bound: even at a pessimistic 4 vCPU / 4 GiB and 12 seconds per
audit, one call costs roughly **$0.0016** — about 1.6% of the $0.10 bundle.
Compute would have to be ~60x worse than that before the margin is in danger.
Gross margin per call is ~98%.

Two things actually threaten the economics, and neither is CPU time:

1. **Idle billing at low volume.** `min-instances=1` on a browser-sized
   container is on the order of **$275/month before a single call arrives**.
   Against near-zero paid traffic that is the entire cost structure, and it is
   pure loss. Check it first; the measure script warns when min-instances > 0.
2. **Demand.** ~98% margin on zero calls is zero. The constraint was never the
   cost side.

The honest constraint: adoption is not something the code can force. A CI
gate that costs money on every push is a line item someone has to approve,
and the ones that get adopted are the ones that are trivially removable and
never block a deploy on a third-party outage. That is why the action defaults
to retrying transient failures and supports `fail-on-error: false`. Making it
harder to remove would lower adoption, not raise it.

## Sandbox limits — know these before promising to check something

The build sandbox **cannot reach**: `*.run.app`, `*.github.io`,
`api.stripe.com`, `api.cdp.coinbase.com`, `docs.cdp.coinbase.com`,
`x402.org`. It **can** reach PyPI, the GitHub API, and the MCP registry API,
and it has a Stripe MCP connector with live read + write access.

So: live-service verification must be run by the user with
`verify-live.sh`. Do not claim a live URL is fine without evidence.

## Hard-won lessons — these cost real time

- **A filtered view is not a record.** Reading `gcloud ... | grep price`
  output and reconstructing name/value pairs produced a confident, wrong
  conclusion that a Stripe key was broken, and a "fix" that overwrote a
  working secret reference and blocked every deploy. Dump the whole record.
- **Green tests do not prove a deploy.** A route that passed every local test
  still 500'd in the container because its data file was never COPYed into
  the image. `verify-live.sh` exists for exactly this.
- **A dev machine's transitive dependencies mask missing pins.** Bazaar
  discovery silently returned `{}` because `jsonschema` was installed locally
  but not pinned. Only clean CI caught it. There is now a static test on the
  requirements text.
- **Prove a test fails.** Every guard in this repo was verified by
  reintroducing the bug and watching the test go red, then restoring.
- **A Marketplace action repo must contain NO workflow files.** Not "one
  action at the root" — no workflows, at all. HubVibe has CI, so it can never
  be listed itself, and moving `action.yml` around does not fix it. This was
  correctly worked out in an earlier session, written down in a README, and
  then that README was deleted along with the directory it lived in. It cost
  a re-derivation. `scripts/publish-action-repo.sh` now encodes it, and
  `tests/test_marketplace_action.py` fails if the assumption changes.
- **Direct `uses:` and a Marketplace listing are different things.**
  `uses: owner/repo@ref` resolves a root `action.yml` with no listing
  involved. Marketplace is discoverability only. Conflating them makes the
  listing look like a blocker for adoption when it is not.
- **The user is often on mobile.** Long multi-line pasted commands land on a
  non-empty input line and run together into garbage (`1gcloud`, `%bash`).
  Keep commands to one short line; that is why `repair-and-deploy.sh` exists.
