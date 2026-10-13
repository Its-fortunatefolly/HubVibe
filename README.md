# HubVibe

[![smithery badge](https://smithery.ai/badge/hubvibe/hubvibe)](https://smithery.ai/servers/hubvibe/hubvibe)

**One node, 70 machine-payable jobs, one price per call, one receipt per job.**
HubVibe sells work to autonomous agents over HTTP 402: 65 workers under
`/work/*` — LLM inference, live web search and page extraction, Base chain
reads, spot and prediction-market data, BigQuery analysis and forecasting,
deterministic regression and probability statistics,
image/speech/video generation, sandboxed Python, maps, and cited research,
verification and company briefs that compose several of them in one call —
plus 5 deterministic site audits (WCAG 2.1 A/AA, SEO, security headers,
performance, bundle). Pay with x402 (USDC on Base) or an MPP transfer-hash
credential. No account, no human in the loop, and every delivered job has a
machine-verifiable receipt at `/work/receipts/{id}`.

Live: **https://hubvibe-io.com**

Every check is a deterministic rule run against the live page. Nothing here is
a language model judging whether a site looks compliant, and a check that could
not run is returned as an error, never as a passing result.

There are two ways in. Both take under a minute.

---

## 1 — Gate your CI on it: one step, nothing to install

```yaml
- uses: Its-fortunatefolly/HubVibe@v1
  with:
    url: https://staging.example.com
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}
```

That is the entire integration. Every pull request now runs the full
compliance bundle against your deployed preview and **fails the build on the
regression that caused it** — not in an audit six months later.

- Findings render in the job summary: rule, impact, nodes hit, link to the fix.
- A check that failed to *execute* is an error, never a silent pass — a green
  build means the checks actually ran.
- `fail-on-error: false` keeps our outage from ever blocking your deploy;
  your real regressions still gate it.
- **$0.15 per PR** for all four checks as one bundle, $0.05 for a single
  check. A repo merging 100 PRs a month spends $10. No subscription, no seat
  licence, no minimum.

Gate a promotion on it:

```yaml
- name: Audit staging
  id: audit
  uses: Its-fortunatefolly/HubVibe@v1
  with:
    url: https://staging.example.com
    wallet-key: ${{ secrets.HUBVIBE_WALLET_KEY }}

- name: Promote to production
  if: steps.audit.outputs.passed == 'true'
  run: ./deploy-production.sh
```

`wallet-key` is an EVM private key funded with USDC on Base. The step reads the
402, signs, and pays for its own run — no account, no checkout, nothing to
provision first. `max-price-usd` (default `0.15`) is a hard ceiling the client
refuses to sign above, so fund the address like petty cash rather than a
treasury. It needs no ETH: x402 signs the transfer off-chain and the
facilitator pays the gas.

If you already hold a prepaid API key, pass `api-key:` instead of `wallet-key:`
and the step spends that.

## 2 — Point your agent at it: no key, no signup, pay per call

An unauthenticated call is not an error here. It is the price sheet:

```bash
curl -i -X POST https://hubvibe-io.com/audit/wcag \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

```
HTTP/1.1 402 Payment Required
PAYMENT-REQUIRED: <base64 x402 v2 PaymentRequired: the same offer, Base and Solana>

{
  "x402Version": 1,
  "error": "payment_required",
  "price_usd": 0.05,
  "accepts": [
    {
      "scheme": "exact",
      "network": "base",
      "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      "payTo": "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd",
      "maxAmountRequired": "50000",
      "resource": "https://hubvibe-io.com/audit/wcag",
      ...
    }
  ],
  "extensions": { "bazaar": { ... } },
  "docs": "https://hubvibe-io.com/.well-known/agent.json"
}
```

An agent reads the 402, signs an x402 payment (USDC on Base, or on Solana
from the v2 header), retries with `X-PAYMENT` (v1) or `PAYMENT-SIGNATURE`
(v2), and gets the audit. Payment is **verified before the audit runs
and settled only after it produces a result** — a failed audit is never
charged: x402 is settled only once the audit has run (and a settlement the
facilitator refuses withholds the result and charges nothing), a prepaid key
is refunded, and an MPP credential a failed audit consumed is accepted again
on the retry.

For Python agents and swarms, the bundled tollbooth client does the whole
loop — challenge, budget check, signing, retry — with two hard spending
limits enforced *before* anything is signed:

```python
from integrations.hubvibe_tollbooth import HubVibeTollbooth

booth = HubVibeTollbooth.from_env()          # HUBVIBE_WALLET_KEY or HUBVIBE_API_KEY
result = booth.audit("https://example.com")  # full bundle, $0.15
result = booth.audit("https://example.com", endpoint="wcag")  # $0.05
```

`accepts` lists only the payment rails that can genuinely settle on this
deployment. A rail that is not configured is omitted rather than advertised
with a null recipient, so a paying agent never builds a payment that cannot
land.

**How machines find this node without being told the URL:** every 402
carries x402 Bazaar discovery data, so the facilitator catalogs this node by
capability and price on the payment that settles through it — the spec has no
other ingestion path; the [`/mcp`](https://hubvibe-io.com/mcp) endpoint is
published in the official MCP registry as
`io.github.Its-fortunatefolly/hubvibe`; A2A agents read
[`/.well-known/agent-card.json`](https://hubvibe-io.com/.well-known/agent-card.json);
and [`/.well-known/agent.json`](https://hubvibe-io.com/.well-known/agent.json)
is generated from the same catalog the routes charge from, so the advertised
price is the charged price by construction.

## Endpoints

| Route | Price | Checks |
|---|---|---|
| `POST /audit/wcag` | $0.05 | WCAG 2.1 A/AA via axe-core, against the rendered page |
| `POST /audit/seo` | $0.05 | Title, meta description, H1s, canonical, OpenGraph, structured data, lang |
| `POST /audit/security` | $0.05 | HTTPS, HSTS, CSP, X-Content-Type-Options, clickjacking, Referrer-Policy, CORS |
| `POST /audit/performance` | $0.05 | DOM nodes, transferred bytes, request count from one real page load |
| `POST /audit/bundle` | $0.15 | All four against one URL, billed once |

Body is `{"url": "..."}`; `wcag` and `seo` also accept raw `{"html": "..."}`.

### Worker network (`/work/*`)

The same payment gate sells a wider catalog beside the audits — each worker
validated for free before any payment is read, never billed for a call that
produced no result, with per-provider retries, exponential backoff, failover
and a circuit breaker behind it. All 65 are in the catalog; every one the node can serve is live on the public node at hubvibe-io.com (`GET /work` lists
them, free). On any other deployment a worker whose provider is not
configured is absent, with a specific reason, until it is.

| Worker | Price | Capability |
|---|---|---|
| `chain.network` / `chain.address` / `chain.transaction` | $0.02–0.05 | Base mainnet reads: block/gas, address report, transaction + receipt |
| `chain.rpc` | $0.05 | Generic allowlisted JSON-RPC passthrough on Base |
| `market.quote` / `market.rates` / `market.ticker` | $0.02 | Coinbase spot price, exchange rates, bid/ask/volume |
| `market.prediction` / `prediction.market` / `prediction.events` | $0.05 | Polymarket odds — top-volume, by slug, or by event |
| `market.stock` / `market.fundamentals` | $0.05 | Live equity quote + daily history (Nasdaq, Yahoo fallback); SEC EDGAR XBRL fundamentals by period |
| `finance.analytics` | $0.50 | Deterministic trading math: returns, volatility, Sharpe/Sortino, drawdown, VaR/CVaR, beta, SMA/EMA/RSI/Bollinger, Black-Scholes Greeks, Kelly |
| `extract.page` / `fetch.raw` | $0.10 | Rendered-page extraction, or raw status/headers/body |
| `search.web` | $0.10 | Live web search, grounded via Gemini's Google Search tool |
| `opendata.search` | $0.10 | Asia-Pacific open-data portals (AU, JP national + Tokyo, HK) searched in one call, any language, with download URLs |
| `traffic.route` | $0.10 | Traffic-aware travel time and distance between two places (Google Routes API): live and static duration, delay, distance, warnings; DRIVE/TWO_WHEELER/WALK/BICYCLE/TRANSIT, optional future `departure_time`, `as_of` + `checked_at` |
| `video.youtube` | $0.05 | YouTube search or lookup by id, every item with live views, likes, comments, duration and thumbnail (YouTube Data API v3) |
| `social.bluesky` / `social.mastodon` | $0.05 | Keyless social reads: Bluesky profile/posts/actor search/thread; Mastodon hashtag timeline/account/search/trends on any public instance |
| `social.x_pulse` | $0.50 | X (Twitter) topic pulse as aggregates only: volume per hour/day, engagement totals and rate, language mix, top hashtags (no posts or profiles returned) |
| `llm.analyze` / `llm.extract` | $0.25 | Gemini: answer-from-material / structured-field extraction |
| `llm.generate` | $0.25 | Raw completion from your prompt — Gemini by default, provider and model selectable |
| `code.execute` | $0.25 | Python, run in Google's own hosted sandbox |
| `image.generate` | $0.50 | Imagen 4 |
| `speech.synthesize` / `speech.transcribe` | $0.25 | Cloud Text-to-Speech / Speech-to-Text v2 (sync, ≤60s) |
| `data.query` | $0.50 | Read-only BigQuery SQL, dry-run cost-gated |
| `data.question` / `data.forecast` / `data.anomalies` | $5–10 | BigQuery: NL→SQL→answer, `AI.FORECAST`, `AI.DETECT_ANOMALIES` |
| `market.intel` | $5.00 | Spot + prediction odds, reconciled by Gemini |
| `research.brief` / `research.page_facts` | $5.00 | One URL → cited brief / caller-named fields |
| `research.web` / `verify.claims` / `security.mcp_inspect` | $5.00 | Web-search brief with citations; claims-vs-sources fact check; MCP endpoint audit |
| `research.company` | $10.00 | Company research brief from live web sources, cited |
| `monitor.snapshot` / `monitor.check` | $0.50 | Baseline a page, then get a diff summary later |
| `commerce.availability` | $0.50 | Live buy/book check on any product or booking page: availability, price, options, quantity and ship-to eligibility, with evidence and `checked_at` |
| `maps.places` / `maps.route` / `maps.weather` | $0.10 | Google's managed Maps Grounding Lite MCP server |
| `video.generate` | $10.00 | Veo, 4-second clip |

**A worker whose provider is not configured is absent** — no route, no
price, no tool, no manifest entry — the same rule the payment rails follow.
`GET /work` (free)
lists what is live on this deployment and why anything else is not.

Prices are flat per call and published where the audits' are: the 402
challenge, `/.well-known/agent.json`, `/openapi.json`, and the MCP tool
list. Callers can send `Idempotency-Key` to make retries of one request
return the first delivery instead of buying the work twice.

## Paying

Three rails, all fail-closed — no valid credential means no job runs:

- **`X-PAYMENT` / `PAYMENT-SIGNATURE`** — x402 v2, `exact` scheme, USDC on
  Base, settled through the Coinbase facilitator. This is the primary rail.
- **`Authorization: Payment ...`** — MPP `evm` method, `hash` credential: the
  payer sends the exact USDC amount to the recipient itself and presents the
  transaction hash. No signature is verified, so smart-wallet payers (Coinbase
  Base Account and other EIP-7702 / ERC-4337 wallets that x402 facilitators
  reject) can buy through this rail. It is advertised in every 402 under
  `other_rails` and in the `WWW-Authenticate: Payment` challenge.
- **`X-API-Key`** — prepaid key, live on the public node. Buy credit by card
  at https://hubvibe-io.com/start (credit packs, no subscription) or with a
  USDC transfer on Solana (`solana_topup` in the agent manifest), then send
  the key as `X-API-Key` on any paid route, including tool calls to `/mcp`.
  Each call is debited at its listed price; a call that fails is refunded.

Read `accepts` and `other_rails` in any 402, or `payment` in the agent
manifest — both list only what actually settles on that deployment. Workers
priced above $1 carry a `buyer_note`: x402 client libraries cap a payment at
$1 by default and the buyer must raise the cap to purchase them.

### What you are charged for

Only a job that produced a result.

- An audit that could not run returns **502** with `billed: false` and is
  never settled. x402 payments are *verified* to grant access but only
  *settled* after the audit has delivered; a prepaid key debited for the
  call is refunded, and a prepaid key bought by an MPP top-up is still
  returned on the 502, holding everything it bought.
- A rate-limited request returns **429** with `Retry-After`, checked before any
  payment is touched, so it costs nothing.
- A settled x402 payment gets a receipt: the facilitator's settle response
  (transaction hash, network, payer) comes back on the 200 in the
  `PAYMENT-RESPONSE` header (`X-PAYMENT-RESPONSE` for v1 clients), exactly
  as the x402 spec describes. The x402 client libraries decode it; the
  bundled `hubvibe_tollbooth.py` keeps it as `last_settlement`.
- One signed payment buys one job. A replayed x402 authorization is
  refused with a 402 before it reaches the facilitator.
- Before paying, `GET /contracts/{capability}` (free) is the Purchase
  Verification Contract: the exact capability, its input and output
  schemas, price, and every rail's network, asset and pay-to, from the same
  catalog the 402 charges from, under one `contract_hash`. The 402 names
  it. `POST /contracts/{capability}/verify` checks your expected terms and
  your request body. A paid request carrying `X-HubVibe-Contract:
  <contract_hash>` is refused before any payment is touched if the route
  no longer sells that contract. The bundled router does all of this
  before it signs anything.
- Every result is checked against the route's published output schema
  (`openapi.json`, the MCP `outputSchema`, the 402's Bazaar record, the A2A
  skill) before it is billed. A result that does not match is not delivered
  and not charged: HTTP 502, reason `contract_mismatch`.
- Every `/work` call, paid or refused, has a receipt at
  `GET /work/receipts/{receipt_id}` (the id is returned on the response):
  outcome (`paid_delivered`, `paid_failed`, `unpaid_refused`, ...), rail,
  network, asset, amount, payer, transaction hash, sha256 hashes of the
  canonical request and result, provider provenance and node version. A
  buyer can verify what it paid for without trusting this node's counters.

### What this service will not fetch

Every audit loads the URL you send from inside the deployment, so the node
refuses, with a **400** and before any payment is read: addresses that are
not globally routable (loopback, private ranges, link-local, the cloud
metadata endpoint), internal hostnames, schemes other than `http`/`https`,
and names that do not resolve. Raw `html` is capped at 2 MiB. None of that
costs the caller anything.

## Discovery

Agents shouldn't have to read documentation to use this:

| | |
|---|---|
| [`/.well-known/agent.json`](https://hubvibe-io.com/.well-known/agent.json) | Full manifest — pricing, live rails, limits, per-endpoint examples |
| [`/openapi.json`](https://hubvibe-io.com/openapi.json) | OpenAPI 3.1, response schema and example on every `/work` route |
| [`/.well-known/ard.json`](https://hubvibe-io.com/.well-known/ard.json) | Agentic Resource Discovery manifest: one entry per worker and audit, with representative queries |
| [`/mcp.json`](https://hubvibe-io.com/mcp.json) | MCP tool definitions: every audit and every worker is a tool on [`/mcp`](https://hubvibe-io.com/mcp) |
| [`/.well-known/agent-card.json`](https://hubvibe-io.com/.well-known/agent-card.json) | A2A Agent Card: the same tools as skills, JSON-RPC at `/a2a` (A2A 1.0 and 0.3), paid with the a2a-x402 extension |
| [`/llms.txt`](https://hubvibe-io.com/llms.txt) | Plain-text summary |
| [`/docs`](https://hubvibe-io.com/docs) | Interactive reference |

## Integrations

- **[`integrations/hubvibe_router.py`](integrations/hubvibe_router.py)** — the
  buyer-side gateway: an agent runs it on its own machine, points HTTP calls at
  `http://127.0.0.1:8402/work/...`, and the router clears each 402 by signing
  with the agent's own wallet (USDC on Base or on Solana, key never leaves the
  machine), retries, and returns the result. Analytical routes (`/work/data/*`,
  `/work/stats/*`) are cached locally for 24 hours so a loop pays once per
  distinct query. Per-call, per-process and per-day caps refuse before any
  signature exists. `python hubvibe_router.py quote|call|serve|status|ledger`.
  As a library (the wallet comes from `HUBVIBE_WALLET_KEY` /
  `HUBVIBE_SOLANA_KEY` or the key files, never from code):

  ```python
  from hubvibe_router import HubVibeRouter
  client = HubVibeRouter(endpoint="https://hubvibe-io.com", wallet_type="base")
  response = client.execute_task(tool="stats.probability", payload={...})
  ```
- **[`action.yml`](action.yml)** — the GitHub Action (`uses: Its-fortunatefolly/HubVibe@v1`).
  It retries transient failures but never a 4xx (repeating a 402 on a metered
  endpoint risks paying twice for one answer), renders findings into the job
  summary via `scripts/render_audit_summary.py`, and can be adopted with
  `fail-on-error: false` so an outage in this service cannot block someone
  else's deploys.
- **Claude Code plugin** — `/plugin marketplace add Its-fortunatefolly/HubVibe`,
  then install `hubvibe` ([`plugins/hubvibe`](plugins/hubvibe)).
- **Gemini CLI extension** — `gemini extensions install https://github.com/Its-fortunatefolly/HubVibe`
  ([`gemini-extension.json`](gemini-extension.json)).
- **MCP registry** — [`server.json`](server.json), listed as `io.github.Its-fortunatefolly/hubvibe`;
  the server itself is `https://hubvibe-io.com/mcp`.
- **[`integrations/hubvibe_tollbooth.py`](integrations/hubvibe_tollbooth.py)** — the client for agents
  running unattended: it settles the 402 itself from an EVM wallet via x402, with a per-call cap
  **and** a process-lifetime budget enforced before anything is signed. Exposes LangChain/CrewAI
  tools via `hubvibe_tools()`.
- **[`glama.json`](glama.json)** — listing metadata for the Glama MCP directory.

## For people, not pipelines

The machine API is the product, and per call is the only price: there are no
subscriptions or human plans (retired 2026-09-06). A person can pay the same
per-call rates through a $0.50 prepaid block where the MPP top-up rail is live.
There is deliberately **no free scan**: an audit costs a real browser page
load, so giving them away funds strangers' compute and invites abuse.

## Honest limits

These are narrow, disclosed, automated signals. They are not a substitute for
a manual accessibility audit, a penetration test, or a Lighthouse run, and
automated scanning is not a compliance certification. Each function's
docstring states exactly what it does and does not check.
