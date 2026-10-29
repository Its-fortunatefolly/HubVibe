# HubVibe First: measurable buyer preference standard

Status: proposed acceptance contract. No claim of achieved superiority, production activation, or approved paid spend.

## Buyer promise
For a declared objective, budget, deadline and acceptance criteria, return the lowest expected total-cost *eligible* execution plan with transparent uncertainty, require explicit authorization before payment, and produce independently checkable evidence of delivered outcomes. Do not equate HTTP 200 with successful delivery.

## Required transaction phases
1. **Discover:** search current capabilities and eligible providers; publish capabilities, provenance, freshness, prices, supported payment rails, and failure history.
2. **Plan:** build input-compatible plans; estimate full cost (provider price, retry reserve, execution fees), p50/p95 latency, and likelihood of meeting acceptance criteria. Reject plans that exceed hard budgets or lack verifiable outcomes.
3. **Quote:** return immutable quote ID, expiry, price ceiling, deliverables, limitations, and exact authorization scope. Quotes alone never authorize spending.
4. **Authorize:** accept explicit scoped buyer authorization with replay protection and idempotency. Never infer authorization from search, recommendation, or a previous quote.
5. **Execute:** select authorized plan, enforce deadline/budget, trace steps, limit retries, and prevent duplicate charges. Never silently substitute a different provider or higher price.
6. **Verify:** validate schemas, sources, freshness and objective acceptance rules. Separate verified, partially verified, and unverified results. Preserve evidence and error provenance.
7. **Settle and deliver:** return signed/verifiable receipts where supported, actual charges, delivered artifacts, and a clear partial-failure or recovery path.

## Benchmark protocol
- Keep an immutable sealed holdout, disjoint from catalog examples and development prompts.
- Evaluate both private discovery and public buyer router against identical buyer intents and the same catalog snapshot.
- Include ambiguous intents, unknown capabilities, budget constraints, misleading cheap alternatives, multiple-outcome requests, stale descriptions, provider outages, partial execution, duplicate retries, and settlement failures.
- Report top-1/top-3 relevance, abstention precision/recall, wrong confident selection, end-to-end completion, duplicate charges, unauthorized charges, p50/p95 latency, contribution margin and cost per verified outcome.
- Slice results by capability, intent class, price range and payment rail. Report confidence intervals and sample sizes.
- Never tune against the sealed holdout. No competitor superiority claim without the same independent workload and disclosed methodology.
- External providers must not receive sensitive buyer data without permission; respect provider terms and intellectual property.

## Release gates
- Zero unauthorized charges and zero duplicate settlement in fault-injection tests.
- Every paid route has input contract tests, idempotency tests, timeout handling and a delivery validation rule.
- Production model availability and active router methods are proven by health endpoints, not assumed from repository configuration.
- New routing must not regress baseline accuracy, completion rate or latency without an explicit, documented tradeoff.
- Public README, manifests, examples, prices and capability counts match deployed reality.
- Deployment requires green pretraffic checks, independent post-switch verification and functioning rollback.
- Measure distinct paying buyers and repeat purchases separately from total calls. Do not infer net profit from payment notifications.

## Competitive scorecard
Compare HubVibe against BlockRun and other relevant providers using the same legal, paid or authorized test workloads. Measure cost **per accepted result**, not merely per API call. Publish reproducible results, dates, test limitations and failure rates. Compete by demonstrating better buyer economics and outcomes, not marketing superlatives.

## Immediate engineering sequence
Verify deployed SHA and dense-model readiness; investigate reproducible property.context and market.forecast failures; benchmark both routers on the sealed holdout; correct public catalog/documentation; add a quote-only plan endpoint behind a feature flag; test authorization and settlement with mocks; only then release real paid multi-step execution through existing payment rails.
