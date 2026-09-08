# HubVibe — session brief

Paste this at the start of a session. It is version-controlled on purpose: the
brief that lived only in a chat paste went stale and cost several sessions,
because a pasted copy cannot be corrected by the work it describes.

**Rule for this file: it holds only facts that do not change week to week.**
Anything that moves — the current `main`, the test count, the live-check count,
what is verified — lives in `docs/HANDOFF.md` and is read from there, never
frozen here. A number written in a brief is wrong one merge later.

---

## What this is

A machine-payable site auditing API, live at https://hubvibe-io.com on the
owner's own box (an old Cloud Run revision still serves as a stopgap).
Software agents POST a URL, get HTTP 402 carrying the price and the rails
that can settle it, pay, and receive an audit. No account, no signup, no
human.

Paid routes: `/audit/wcag`, `/audit/seo`, `/audit/security`,
`/audit/performance`, `/audit/bundle`, plus `/audit` (alias of wcag).

**Tollbooth on a highway, not SaaS.** A machine drives up, pays, drives on.

## Money model

$0.03 per single audit, $0.10 per bundle, ~98% gross margin. Revenue at that
rate needs enormous call volume, so the whole game is machine traffic.

The human plan tiers are retired (2026-09-06): per call is the only price.
Do not build SaaS features, checkout UIs, dashboards, or logins.

**The website is not a product surface.** Owner, 2026-09-08: "This was never a
SaaS... it truly isnt for anything but an about HubVibe homebase. HubVibe is
software to software. A tollbooth." The landing page exists only to say what
HubVibe is, kept for continuity; the product is the 402 path and the discovery
surfaces that lead agents to it. An early build drifted into a website and human
tiers and had to be unwound, so this is restated rather than assumed -- and
plan/subscription language surviving on any public surface is a truth bug, not
cosmetics: the landing page's meta description, the one string every crawler and
link preview shows, went on offering the retired per-site plan until 2026-09-08.
(The sweep that guards this reads every shipped .md, so describe the retired
wording here -- never quote it, or this file becomes the offender.)

## Where the truth lives

| Question | Read this, do not assume |
|---|---|
| What is on main, what is verified | `docs/HANDOFF.md` — current and maintained |
| Does the deployed node work | `bash scripts/verify-live.sh` — it prints its own commit and warns if the checkout is stale |
| How many tests | `python -m pytest -q` — read it off the run |
| Who receives x402 payments | `BASE=https://hubvibe-io.com bash scripts/payment-status.sh` |

Fixed facts: the node runs from `deploy/vps` on the owner's box; the Cloud
Run stopgap is project `resolver-time`, service `hubvibe`, region
`us-south1`. Facilitator: `x402.dexter.cash` is the default everywhere
(keyless, Base mainnet, indexes on the first payment); the box was installed
on `facilitator.xpay.sh`, which settles but indexes nothing, and is moved
only with `scripts/switch-facilitator.sh`. Coinbase CDP is **abandoned, not
pending** — its review wants proof of a DBA that does not exist, and its
code is gone. Do not suggest Coinbase.

## Things already settled — do not re-litigate

- **The pay-to address exists and is well-formed.** Two sessions burned days on
  a "missing 40-hex address." It was minted for the deployment, not held in a
  wallet app. Both halves of that contradiction were true.
- **Coinbase is out of the path entirely.**
- **Bazaar indexing needs a payment, not a better facilitator.** The spec has
  one ingestion path: a facilitator catalogs a resource when a `PaymentPayload`
  carrying the discovery extension reaches it. No registration endpoint, no
  crawler. So a resource nobody has paid is uncatalogued on *every* facilitator.
  Hunting for "a keyless facilitator that also serves `/discovery/resources`"
  cannot fix that, and was the wrong question for a while.
- **Revenue being zero was NOT purely a demand problem.** Until #61 the 402 was
  unpayable by any conforming client: `accepts[]` used field names of our own
  invention, so the x402 library raised before producing a signature. The
  failure happened inside the caller's process — nothing reached the
  facilitator, nothing was logged, and from this side it looked exactly like
  nobody arriving. Fixed and verified live. Do not restate "the constraint is
  demand, not plumbing" as established; it was wrong once and cost the most.

## Hard rules

- **The sandbox cannot reach** `*.run.app`, `api.stripe.com`, or facilitator
  hosts, and has no `gcloud`. It cannot deploy or verify live. The owner runs
  those in Cloud Shell. **Never claim a live fact without seeing its output.**
- **Setting env vars is not a deploy.** On the box a deploy is `git pull`
  then `docker compose up -d --build` in `deploy/vps`. On Cloud Run,
  `gcloud run services update --update-env-vars` keeps the same container
  image; use `scripts/repair-and-deploy.sh` (or `go-live.sh`, which hands
  off to it), which deploy source. This exact mistake hid every merged fix
  from production for days.
- **A stale checkout is not a pass.** `verify-live.sh` prints its own commit and
  the check count; a run whose count is lower than the current checker's is an
  old script that never asked the new questions. This has cost three cycles.
- **Never advertise a rail that cannot settle.** Everything fails closed. This
  is the core discipline of the codebase.
- **Never print the per-call cent price on a human-facing page.**
- **Prove every new test by reintroducing the bug and watching it go red.**
- **When a surface is consumed by someone else's parser, test it with their
  parser.** Three separate bugs shipped green because a check asked whether a
  field was *present* rather than whether the consumer *accepts* it: the Bazaar
  record, the payable 402, the zero address.
- Be concise. No status essays, no repeated options. Find a problem, fix it.
  Cannot do something? One line, plus the exact command for the owner to run.

## The open work

Read `## Live state` and `## Owner runbook` in `docs/HANDOFF.md`. They are
maintained; this list is not.

The standing goal is the first paid call — one $0.03 payment that proves the
settle side (never once exercised) and registers the node in whatever Bazaar
index processes it:

```bash
bash scripts/first-paid-call.sh --new-wallet   # only if no wallet yet
# fund the printed address with USDC on Base -- $1 is plenty
BASE=https://hubvibe-io.com bash scripts/first-paid-call.sh
```

That payer wallet needs no ETH: x402's exact-EVM scheme signs an EIP-3009
authorization off-chain, so it never broadcasts, and the facilitator submits
the transfer and pays the gas. The send that FUNDS it is an ordinary transfer
out of whichever wallet holds the dollar, and covers its own gas like any
other -- the one place on this path where gas is the owner's to pay. Saying
"no ETH needed" without naming which wallet is what makes a routine gas
prompt look like something broken.
