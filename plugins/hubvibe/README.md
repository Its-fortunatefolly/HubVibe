# HubVibe plugin for Claude Code

HubVibe (https://hubvibe-io.com) sells finished work to AI agents and businesses, one price per call. This plugin connects Claude Code to HubVibe's MCP server (`https://hubvibe-io.com/mcp`) and adds a skill that tells Claude which HubVibe tool fits which job. Calls are paid from a prepaid HubVibe credit key.

## Install

1. Add the HubVibe marketplace and install the plugin. In your shell:

   ```bash
   claude plugin marketplace add Its-fortunatefolly/HubVibe
   claude plugin install hubvibe@hubvibe
   ```

   Or inside a Claude Code session, run `/plugin marketplace add Its-fortunatefolly/HubVibe`, then `/plugin install hubvibe@hubvibe` and choose an install scope in the panel that opens. On Claude Code v2.1.275 or later, `/plugin install hubvibe --marketplace Its-fortunatefolly/HubVibe` does both in one step.

2. Get a key. Buy a prepaid credit pack by card at https://hubvibe-io.com/start (packs from $25; no subscription, no account). The key is shown as soon as the card payment clears. Save it.

3. Set the key in the shell you start Claude Code from, then start Claude Code:

   ```bash
   export HUBVIBE_API_KEY="paste-your-key-here"
   claude
   ```

   In PowerShell: `$env:HUBVIBE_API_KEY = "paste-your-key-here"`. The plugin sends the key as the `X-API-Key` header. Run `/mcp` in the session to see the server, listed as `plugin:hubvibe:hubvibe`.

## Paying

Each tool call is charged from your credit at the tool's listed price. A call that fails is not charged. If no usable key reaches HubVibe (the variable is not set, the key is wrong, or the credit left is less than the call's price), a call answers with its price and payment options instead of a result, and nothing is charged.

## Tools

| Job | Tool | Price per call |
| --- | --- | --- |
| Qualify a business website as a sales lead | `hubvibe_lead_qualify` | $0.25 |
| Full website audit (accessibility, SEO, security headers, page speed) | `audit_bundle` | $0.15 |
| Accessibility audit (WCAG 2.1 A/AA) | `audit_wcag` | $0.05 |
| SEO audit | `audit_seo` | $0.05 |
| Security header audit | `audit_security` | $0.05 |
| Page speed and page weight audit | `audit_performance` | $0.05 |
| Email verification | `hubvibe_email_verify` | $0.02 |
| Company profile | `hubvibe_company_enrich` | $0.05 |
| Sanctions screening (US, UK and EU lists) | `hubvibe_sanctions_screen` | $0.05 |
| Identity check (sanctions, email, phone, IP) | `hubvibe_identity_check` | $0.10 |
| Web search with sources | `hubvibe_search_web` | $0.10 |
| Web and news results (SERP) | `hubvibe_search_results` | $0.25 |
| Web page extraction | `hubvibe_extract_page` | $0.10 |
| Stock quote and daily history | `hubvibe_market_stock` | $0.05 |
| SEC-filed fundamentals | `hubvibe_market_fundamentals` | $0.05 |
| Insider trades (SEC Form 4) | `hubvibe_market_insiders` | $0.10 |
| Crypto spot price | `hubvibe_market_quote` | $0.02 |
| Crypto bid and ask | `hubvibe_market_ticker` | $0.02 |
| Current news, any language | `hubvibe_news_search` | $0.25 |

The server offers more tools than these. Each tool's description in the server's tool list states its price per call, and the `hubvibe_*` tools are also listed with their prices at https://hubvibe-io.com/work (`price_usd`; `hubvibe_lead_qualify` appears there as `lead.qualify`). If a price here ever differs from the server's, the server's price applies.

## Update or remove

```bash
claude plugin update hubvibe@hubvibe
claude plugin uninstall hubvibe@hubvibe
```

Questions: hubvibe@hubvibe-io.com
