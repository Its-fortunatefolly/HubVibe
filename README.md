# HubVibe

**Machine-payable site compliance audits.** WCAG 2.1 A/AA, SEO, security
headers, and performance — deterministic rules against the real rendered page,
priced per call, payable by software with no account and no human in the loop.

Live: **https://hubvibe-831480473793.us-south1.run.app**

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
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
```

That is the entire integration. Every pull request now runs the full
compliance bundle against your deployed preview and **fails the build on the
regression that caused it** — not in an audit six months later.

- Findings render in the job summary: rule, impact, nodes hit, link to the fix.
- A check that failed to *execute* is an error, never a silent pass — a green
  build means the checks actually ran.
- `fail-on-error: false` keeps our outage from ever blocking your deploy;
  your real regressions still gate it.
- **$0.10 per PR** for all four checks as one bundle, $0.03 for a single
  check. A repo merging 100 PRs a month spends $10. No subscription, no seat
  licence, no minimum.

Gate a promotion on it:

```yaml
- name: Audit staging
  id: audit
  uses: Its-fortunatefolly/HubVibe@v1
  with:
    url: https://staging.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}

- name: Promote to production
  if: steps.audit.outputs.passed == 'true'
  run: ./deploy-production.sh
```

Keys come from [`/billing/checkout`](https://hubvibe-831480473793.us-south1.run.app/billing/checkout).
Or skip the key entirely — see the second way in.

## 2 — Point your agent at it: no key, no signup, pay per call

An unauthenticated call is not an error here. It is the price sheet:

```bash
curl -i -X POST https://hubvibe-831480473793.us-south1.run.app/audit/wcag \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

```
HTTP/1.1 402 Payment Required
WWW-Authenticate: Payment ...

{
  "error": "payment_required",
  "price_usd": 0.03,
  "accepts": [ { "protocol": "x402", ... }, { "protocol": "mpp", ... } ],
  "docs": "/.well-known/agent.json"
}
```

An agent reads the 402, signs an x402 payment (USDC on Base), retries with
`X-PAYMENT`, and gets the audit. Payment is **verified before the audit runs
and settled only after it produces a result** — a failed audit is never
charged, on any rail.

For Python agents and swarms, the bundled tollbooth client does the whole
loop — challenge, budget check, signing, retry — with two hard spending
limits enforced *before* anything is signed:

```python
from integrations.hubvibe_tollbooth import HubVibeTollbooth

booth = HubVibeTollbooth.from_env()          # HUBVIBE_WALLET_KEY or HUBVIBE_API_KEY
result = booth.audit("https://example.com")  # full bundle, $0.10
result = booth.audit("https://example.com", endpoint="wcag")  # $0.03
```

`accepts` lists only the payment rails that can genuinely settle on this
deployment. A rail that is not configured is omitted rather than advertised
with a null recipient, so a paying agent never builds a payment that cannot
land.

**How machines find this node without being told the URL:** every 402
carries x402 Bazaar discovery data, so facilitators index it by capability
and price; the MCP endpoint at [`/mcp`](https://hubvibe-831480473793.us-south1.run.app/mcp)
is listed in the official registry as `io.github.Its-fortunatefolly/hubvibe`;
and [`/.well-known/agent.json`](https://hubvibe-831480473793.us-south1.run.app/.well-known/agent.json)
is generated from the same catalog the routes charge from, so the advertised
price is the charged price by construction.

## Endpoints

| Route | Price | Checks |
|---|---|---|
| `POST /audit/wcag` | $0.03 | WCAG 2.1 A/AA via axe-core, against the rendered page |
| `POST /audit/seo` | $0.03 | Title, meta description, H1s, canonical, OpenGraph, structured data, lang |
| `POST /audit/security` | $0.03 | HTTPS, HSTS, CSP, X-Content-Type-Options, clickjacking, Referrer-Policy, CORS |
| `POST /audit/performance` | $0.03 | DOM nodes, transferred bytes, request count from one real page load |
| `POST /audit/bundle` | $0.10 | All four against one URL, billed once |

Body is `{"url": "..."}`; `wcag` and `seo` also accept raw `{"html": "..."}`.

## Paying

Three rails, all fail-closed — no valid credential means no audit runs:

- **`X-API-Key`** — subscription key from `/billing/checkout`
- **`X-PAYMENT`** — x402
- **`Authorization: Payment ...`** — MPP (Stripe Shared Payment Tokens for
  fiat, or Tempo for crypto)

Which are live is deployment-specific. Read `accepts` in any 402, or
`payment.methods` in the agent manifest — both list only what actually works.

### What you are charged for

Only an audit that produced a result.

- An audit that could not run returns **502** and is never settled. x402
  payments are *verified* to grant access but only *settled* after the audit
  has delivered.
- A rate-limited request returns **429** with `Retry-After`, checked before any
  payment is touched, so it costs nothing.

## Discovery

Agents shouldn't have to read documentation to use this:

| | |
|---|---|
| [`/.well-known/agent.json`](https://hubvibe-831480473793.us-south1.run.app/.well-known/agent.json) | Full manifest — pricing, live rails, limits, per-endpoint examples |
| [`/openapi.json`](https://hubvibe-831480473793.us-south1.run.app/openapi.json) | OpenAPI 3.1 |
| [`/mcp.json`](https://hubvibe-831480473793.us-south1.run.app/mcp.json) | MCP tool definitions |
| [`/llms.txt`](https://hubvibe-831480473793.us-south1.run.app/llms.txt) | Plain-text summary |
| [`/docs`](https://hubvibe-831480473793.us-south1.run.app/docs) | Interactive reference |

## Integrations

In [`wcag-audit-engine/integrations/`](wcag-audit-engine/integrations/):

- **`mcp_server.py`** — MCP server exposing all five audits as tools, built on
  the official SDK. Standalone, with its own `mcp_requirements.txt`: the `mcp`
  package needs a newer Starlette than the deployed service pins for FastAPI,
  so it is deliberately kept out of the service's dependency tree.
- **`langchain_tool.py`** — LangChain tool wrapper. Subscription key only; it
  raises on a 402 rather than paying.
- **`hubvibe_tollbooth.py`** — the client for agents running unattended. Same
  audits, but it settles the 402 itself from an EVM wallet via x402, so no
  human has to go get a key. Enforces a per-call cap **and** a
  process-lifetime budget, both before anything is signed — an autonomous
  loop with an unbounded wallet is a drained wallet. Exposes LangChain/CrewAI
  tools via `hubvibe_tools()`.
- **`github_action.yml`** — a copyable workflow, for repos that would rather
  paste a job than depend on a published action.

At the repo root:

- **`action.yml`** — the composite GitHub Action, and the single copy of it.
  It retries transient failures but never a 4xx (repeating a 402 on a metered
  endpoint risks paying twice for one answer), renders findings into the job
  summary via `scripts/render_audit_summary.py`, and can be adopted with
  `fail-on-error: false` so an outage in this service cannot block someone
  else's deploys.
- **`scripts/publish-action-repo.sh`** — generates the standalone repo the
  Marketplace listing needs (see below).
- **`glama.json`** — listing metadata for the Glama MCP directory.

## For people, not pipelines

The machine API is the product. There is also a website for humans who want a
report rather than an integration — priced per site watched, not per scan.
There is deliberately **no free scan**: an audit costs a real browser page
load, so giving them away funds strangers' compute and invites abuse.

## Publishing the GitHub Action

Two separate things, with different rules:

**Direct use works today.** An `action.yml` at a public repo's root is usable
as-is, no Marketplace involved:

```yaml
- uses: Its-fortunatefolly/HubVibe@v1
  with:
    url: https://your-site.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
```

**A Marketplace listing needs a different repo.** GitHub requires an action
repository to contain a single root `action.yml` *and no workflow files at
all*. This repo has `.github/workflows/`, so it can never be listed itself —
moving `action.yml` around does not help. Generate the clean standalone repo
instead:

```bash
bash scripts/publish-action-repo.sh /tmp/hubvibe-audit-action
```

It copies `action.yml` and the summary renderer verbatim (so the published
action cannot drift from the one in this repo), writes a listing README, and
prints the exact push/tag/publish steps.

## Publishing to the MCP registry

`server.json` in this repo root registers the live `/mcp` endpoint as a
**remote** server, so no package needs publishing anywhere.

`mcp-publisher` is a Go binary from the registry's GitHub releases — it is
**not** on npm (the `mcp-publisher` package on npm is an unrelated
browser-automation tool):

```bash
curl -L "https://github.com/modelcontextprotocol/registry/releases/download/v1.8.1/mcp-publisher_linux_amd64.tar.gz" \
  | tar xz mcp-publisher
./mcp-publisher login github     # opens a browser; proves you own the namespace
./mcp-publisher publish
```

The `name` field is `io.github.its-fortunatefolly/hubvibe`, which the GitHub
login authenticates against. `server.json` is validated against the official
schema (note: `description` is capped at 100 characters).

**Bump `version` before every publish.** The registry treats a version as
immutable and rejects a re-publish of one it already serves, so a corrected
`server.json` left at its old version fails at the last step — after the
browser login, which is the most annoying place to find out. Check what is
actually live first; the registry is public and needs no auth:

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=hubvibe"
```

Correcting text without bumping the version is the trap: the file reads
right in the repo, the registry keeps serving the old copy, and nothing
reports a problem. On 2026-08-11 the registry took 1.1.0 whose header still
read "pay per call with x402/MPP"; the repo dropped that rail assertion the
same week, and the two stayed out of sync because a republish of 1.1.0 could
never have landed.

## Repository layout

```
wcag-audit-engine/        the audit service (this is the product)
  app/                    FastAPI app, audit engines, payment rails
  integrations/           MCP server, LangChain tool, GitHub Action
privacy-compliance-scanner/
dead-end-resolver/
scripts/verify-live.sh    verifies a deployed node from outside
tests/
```

## Running the tests

```bash
pip install -r requirements.txt
pytest tests/ -q
```

`scripts/verify-live.sh` checks a *deployed* node end to end — service up, the
whole discovery surface, every paid route answering 402 rather than 404, and
that the 402 is actually machine-actionable. Green unit tests do not prove a
deploy; this does.

## Honest limits

These are narrow, disclosed, automated signals. They are not a substitute for
a manual accessibility audit, a penetration test, or a Lighthouse run, and
automated scanning is not a compliance certification. Each function's
docstring states exactly what it does and does not check.
