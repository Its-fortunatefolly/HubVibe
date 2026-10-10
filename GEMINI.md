# HubVibe tools

HubVibe (https://hubvibe-io.com) sells completed work to agents, one price per call. The `hubvibe` MCP server lists every tool for free; each `tools/call` is paid.

## Paying for a call

- With a prepaid key: set it once with `gemini extensions config hubvibe`. It is sent as `X-API-Key` and spent per call.
- Without a key: a paid call answers with its price and the accepted payment methods instead of a result. Nothing is charged for that answer.
- A call that fails is not billed. Every `hubvibe_*` tool returns a `receipt_url` with its result.

## Choosing a tool

- Current news on any topic, in any language: `hubvibe_news_search`.
- Web and news results to read next: `hubvibe_search_results`.
- Official economic series (GDP, inflation, unemployment...): `hubvibe_data_macro`.
- Government datasets (Korea, Japan, EU, UK, Canada, Australia...): `hubvibe_opendata_search`, then `hubvibe_opendata_table` to get the rows.
- Can this product be bought, and can it ship here: `hubvibe_commerce_availability`, then `hubvibe_commerce_shipping`.
- Stocks and company filings: `hubvibe_market_stock`, `hubvibe_market_fundamentals`; portfolio and options math: `hubvibe_finance_analytics`.
- Website accessibility, SEO, security and performance checks: `audit_wcag`, `audit_seo`, `audit_security`, `audit_performance`, `audit_bundle`.

Every tool accepts an optional `language` (BCP-47, such as `ja`, `ko`, `pt-BR`); human-readable text in the result comes back in that language.
