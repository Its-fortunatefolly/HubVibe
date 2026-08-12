# HubVibe

**Machine-payable site compliance audits.** An agent calls an endpoint, gets an
HTTP 402 carrying the price and how to pay, settles it, and receives a result —
no account, no signup, no human in the loop.

Live: **https://hubvibe-831480473793.us-south1.run.app**

Every check is a deterministic rule run against the live page. Nothing here is
a language model judging whether a site looks compliant, and a check that could
not run is returned as an error, never as a passing result.

---

## Try it

An unauthenticated call tells you exactly what it costs and how to pay:

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
  "accepts": [ { "protocol": "mpp", "method": "tempo", ... } ],
  "docs": "/.well-known/agent.json"
}
```

`accepts` lists only the payment rails that can genuinely settle on this
deployment. A rail that is not configured is omitted rather than advertised
with a null recipient, so a paying agent never builds a payment that cannot
land.

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
# HubVibe Site Compliance Audit (GitHub Action)

Runs a real, deterministic site audit (WCAG accessibility, SEO, security
headers, performance, or all four as a bundle) against a live URL and fails
the build if it doesn't pass. Every check runs against the actual page --
axe-core for accessibility, a real HTTP response for security headers, a
real Playwright page load for performance -- never an LLM guessing at
quality.

This directory is meant to become the **root** of its own dedicated public
repo before publishing to the GitHub Actions Marketplace: Marketplace
requires `action.yml` at repo root and requires the repo contain no other
workflow files, which rules out publishing directly from the HubVibe
monorepo (it has its own CI workflow). Copy this directory's contents to
the root of a new repo (e.g. `hubvibe-audit-action`), tag a release (e.g.
`v1.0.0`), and check "Publish this release to the GitHub Marketplace" when
creating the release.

## Usage

```yaml
- name: HubVibe compliance audit
  uses: its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://your-site.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
    endpoint: bundle   # or: wcag, seo, security, performance
```

## Inputs

| Input               | Required | Default  | Description                                      |
|---------------------|----------|----------|---------------------------------------------------|
| `url`                | yes      | --       | The live URL to audit.                            |
| `api-key`            | yes      | --       | Your HubVibe API key (`X-API-Key`).                |
| `endpoint`           | no       | `bundle` | `wcag`, `seo`, `security`, `performance`, or `bundle`. |
| `fail-on-violation`  | no       | `true`   | Set `false` to report without failing the build.  |

## Outputs

| Output    | Description                                  |
|-----------|-----------------------------------------------|
| `passed`   | `"true"` or `"false"` -- the audit's overall result. |
| `response` | Raw JSON response body from the audit call.  |

For CI, paying per call is usually the right choice: $0.03 per check or
$0.10 for the bundle over x402/MPP, straight against the REST endpoints,
with no subscription key to store as a repository secret.

If you'd rather hold a key, the human plans at
https://hubvibe-831480473793.us-south1.run.app are priced per site watched
($79/month for 5, $249/month for 50) rather than per scan — see
`/.well-known/agent.json` for the live prices, which is the only place
guaranteed to match what checkout actually charges.
