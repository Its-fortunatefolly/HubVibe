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
- **`langchain_tool.py`** — LangChain tool wrapper
- **`github_action.yml`** — audit-on-push CI gate
- **`marketplace-action/`** — the same as a publishable composite Action

## For people, not pipelines

The machine API is the product. There is also a website for humans who want a
report rather than an integration — priced per site watched, not per scan.
There is deliberately **no free scan**: an audit costs a real browser page
load, so giving them away funds strangers' compute and invites abuse.

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
