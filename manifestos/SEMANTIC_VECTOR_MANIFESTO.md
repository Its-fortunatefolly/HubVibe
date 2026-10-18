# HubVibe Semantic + Vector Discovery Manifesto

## Mission
HubVibe sells completed work to autonomous buyers, not memberships. Buyers should be able to describe an intended outcome in natural language and discover an **existing, available** route with its input schema, exact price contract and supported payment rails. Semantic/vector discovery is **parallel to**, never a replacement for, the existing buyer router, payment verification, audit routes, MCP, A2A, OpenAPI, and VPS deployment.

## Capability descriptions
Each capability must state (a) delivered outcome and accepted inputs, (b) long-tail buyer phrases and alternate intents, (c) output schema/provenance/freshness, (d) what the tool cannot verify or promise, (e) behavior when sources are missing or time out, and (f) where to retrieve the authoritative live price, supported rails and purchase contract. The curated intent map in `integrations/hubvibe_semantic_descriptions.json` only enriches capabilities actually found in live OpenAPI. A description alone cannot create or advertise an unavailable route.

Examples: "check my site's accessibility, SEO, security and performance together" should match `/audit/bundle`; "get the live price of BTC" should match `/work/market/quote`; "screen sanctions and contact risks" should match `/work/identity/check`, **not** imply verified legal identity; "estimate event odds" should match `/work/market/prediction`, **not** assert a calibrated statistical forecast. Missing fields, failed checks and unsupported coverage must not become success, `null` with `pass:true`, or claims stronger than the provider's evidence.

## Parallel discovery architecture
* BM25 term relevance and normalized TF-IDF sparse vectors give fast, no-extra-fee, local retrieval.
* Optional local dense embeddings provide actual semantic vector similarity when an explicitly installed local embedding model exists. Never describe lexical similarity alone as neural semantic search.
* Fuse available rankings with reciprocal rank fusion; return no result when there is no matching evidence. Ranking scores are **not** probabilities.
* Verify that an exact selected route exists in current `/openapi.json`. Do not invent route IDs or prices.
* Only the existing `HubVibeRouter` can execute a **user-selected exact paid route**, using existing contracts, schema validation, per-call caps, idempotency, and x402/Base/Solana/MPP/prepaid guards as implemented. Do not auto-spend based on fuzzy ranking.
* Never proxy private keys or signed payment material. HTTP discovery must default to loopback-only. No new public route until explicitly secured and deployed.

## Agent discovery and registries
Expose real, reachable capability descriptions, intent phrases, output guarantees, route IDs and verified schema references through existing OpenAPI, ARD, MCP, A2A agent cards, and compatible machine-readable manifests when deployed and validated. Existing registry IDs and addresses stay stable. Publishing a GitHub manifesto **does not** itself embed HubVibe in third-party registry vector stores; each registry must actually ingest the published metadata. Discovery → intent matching → real contract/price quote → supported wallet/rail → authorized paid request → verified completed delivery → receipt is the intended customer journey.

Keep discovery cheap and fast: short timeouts, bounded JSON, small cached metadata, clean 4xx/5xx on failure and no slow model calls while a crawler probes. Async work must expose job status/polling and final delivery; do not mark a 202 as delivered.

## Payments and honesty
An unauthenticated HTTP 402 is a normal price challenge, not proof of payment failure. Distinguish challenge, missing test credentials, failed signature, incompatible payment headers, verification/settlement rejection, provider failure, timeout, and buyer abandonment. Only measured accepted and settled paid requests count as sales. No silent paid failures; no charges for work not delivered. Protect logs from API keys, wallet secrets, and payment headers.

Pricing is authoritative only in **current contracts, checkout and live 402 metadata**. Do not hardcode disputed historic pricing. A price adjustment requires an exact, owner-approved matrix and parity checks across route, contract, registry metadata, checkout and receipts. Preserve working audit prices ($0.05 single/$0.15 bundle) and the separately specified market.signal $0.05 / market.forecast $1.00 pending any new explicit decision.

## Measure what matters
Compare semantic/vector discovery with a baseline using real intent searches, first legitimate buys, submitted payment credentials, authorization results, verified delivery, repeat buyers and latency percentiles. Exclude health monitors and crawlers from conversion calculations. Do not promise a conversion percentage without evidence.

## Implementation state
`integrations/hubvibe_semantic_vector_router.py` is a parallel offline-first discovery implementation. It must be tested with actual production OpenAPI and authorized purchase paths before exposing public registry endpoints. It does not modify the working production router or payment rails.
