---
name: hubvibe
description: "HubVibe pay-per-call tools from this plugin's MCP server. Use to qualify a business website as a sales lead, audit a website (accessibility, SEO, security headers, page speed), verify an email address, get a company profile, screen a name against sanctions lists, run an identity check, search the web, extract a web page, or get stock quotes, SEC filings, insider trades, crypto prices and current news."
---

# HubVibe

HubVibe (https://hubvibe-io.com) sells finished work per call. This plugin connects its MCP server, `https://hubvibe-io.com/mcp`. In Claude Code the tools are named `mcp__plugin_hubvibe_hubvibe__<tool>`, for example `mcp__plugin_hubvibe_hubvibe__hubvibe_lead_qualify`.

## Paying

- Each call is charged from the user's prepaid HubVibe credit at the tool's listed price. A call that fails is not charged. Listing the tools is not charged.
- The key comes from a credit pack bought by card at https://hubvibe-io.com/start. No subscription, no account.
- The user sets the key as the environment variable `HUBVIBE_API_KEY` in the shell that starts Claude Code. The plugin sends it as the `X-API-Key` header.
- A call that answers with a price and payment options instead of a result means no usable key reached HubVibe: the variable is not set, the key is wrong, or the credit left is less than that call's price. Nothing was charged. Tell the user to check the key, or to buy credit at https://hubvibe-io.com/start, set `HUBVIBE_API_KEY`, and restart Claude Code.

## Pick the tool

| Job | Tool (main input) | USD per call |
| --- | --- | --- |
| Qualify a business website as a sales lead: 0-100 score, outreach hooks, SEO and security findings, site tags, mail setup, company profile | `hubvibe_lead_qualify` (url, optional business_name) | 0.25 |
| Full website audit: accessibility, SEO, security headers and page speed from one page load | `audit_bundle` (url) | 0.15 |
| Accessibility audit only (WCAG 2.1 A/AA, axe-core) | `audit_wcag` (url or html) | 0.05 |
| SEO audit only | `audit_seo` (url or html) | 0.05 |
| Security header audit only | `audit_security` (url) | 0.05 |
| Page speed and page weight audit only | `audit_performance` (url) | 0.05 |
| Verify an email address before sending | `hubvibe_email_verify` (email) | 0.02 |
| Company profile from a domain or a name | `hubvibe_company_enrich` (domain or name) | 0.05 |
| Sanctions screening against US, UK and EU lists | `hubvibe_sanctions_screen` (name) | 0.05 |
| Identity check: sanctions, email, phone and IP in one call | `hubvibe_identity_check` (any of name, email, phone, ip) | 0.10 |
| Web search answered from live results, with sources | `hubvibe_search_web` (query) | 0.10 |
| Raw web and news results (SERP) | `hubvibe_search_results` (query) | 0.25 |
| Read a web page as clean text | `hubvibe_extract_page` (url) | 0.10 |
| Stock quote and daily history | `hubvibe_market_stock` (symbol) | 0.05 |
| SEC-filed fundamentals (10-K, 10-Q) | `hubvibe_market_fundamentals` (symbol or cik) | 0.05 |
| Insider trades from SEC Form 4 filings | `hubvibe_market_insiders` (symbol or cik) | 0.10 |
| Crypto spot price | `hubvibe_market_quote` (product_id, such as BTC-USD) | 0.02 |
| Crypto best bid and ask | `hubvibe_market_ticker` (product_id) | 0.02 |
| Current news on any topic, in any language | `hubvibe_news_search` (query or symbol) | 0.25 |

## Habits

- One URL, several audit checks: call `audit_bundle` once instead of the single audits. One page load, lower total.
- Most tools take an optional `language` (BCP-47, such as `es` or `pt-BR`) for the readable text in their result. On `hubvibe_search_results` and `hubvibe_news_search` it narrows the search to results in that language instead.
- The server has more tools (research, agent tasks, data, generation) priced up to USD 20 per call. Each tool's description states its price. Before any call priced at USD 1 or more, tell the user the price and wait for a yes.
- A long job may answer `"status": "processing"` with a `collect_url` such as `/work/jobs/<id>`. Wait `retry_after_seconds`, then GET `https://hubvibe-io.com/work/jobs/<id>` until the result is there. Collecting costs nothing, and a job that fails is not charged.
- Each delivered `hubvibe_*` result includes a `receipt_url`.
