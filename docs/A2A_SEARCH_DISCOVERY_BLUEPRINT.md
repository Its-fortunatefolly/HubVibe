# InfoLab: A2A Search & Discovery Layer — Technical & Economic Blueprint

Status: design specification
Scope: systems architecture and settlement economics only (no UI, no sales motion)

## 1. The failure this closes

Every autonomous agent that comes online today (a browser-automation
recovery agent, a pricing bot, a code-review agent, a data-enrichment
worker) publishes a capability card at `/.well-known/agent.json` — a
manifest declaring an endpoint, a price, and I/O schema (`POST /resolve`,
`payment_required: true`, `price_usd: 0.002`, in the shape already emerging
across the ecosystem). That pattern scales to zero agents finding each
other. There is no protocol-level answer to "which of the 4M agents on the
open internet can perform capability X for $0.002 with a 99.9th-percentile
latency under 400ms, and can I trust its manifest is real and its uptime is
real." Agents currently either hardcode counterparty URLs (no market) or
crawl blind (no verification, no ranking, no settlement). That gap is the
product: **InfoLab**, a search engine whose documents are agent capability
manifests, whose queries are other agents, and whose click-through is a
metered API call that settles automatically.

This is structurally different from web search: the corpus is small
per-document (a JSON capability card, not a webpage), the query volume is
driven by machines running in loops (orders of magnitude higher QPS per
"user" than any human search engine), the ranking signal is objective
(uptime, verified schema conformance, price, latency, settlement success
rate) instead of subjective relevance, and every successful match is already
a metered, billable transaction on both sides. The index *is* the
marketplace.

## 2. System overview

```
                         ┌─────────────────────────────┐
   Agents (millions)     │        INGESTION PLANE       │
   .well-known/agent.json│  crawl · push · verify · sign │
   webhooks, pings   ───▶│                               │
                         └───────────────┬───────────────┘
                                         │ canonicalized
                                         │ AgentCapabilityRecord (ACR)
                                         ▼
                         ┌─────────────────────────────┐
                         │        INDEX PLANE            │
                         │  sparse (BM25) + dense (ANN)  │
                         │  capability graph (edges)     │
                         │  trust/reputation store       │
                         └───────────────┬───────────────┘
                                         │
                         ┌───────────────┴───────────────┐
                         │        QUERY PLANE              │
   Agents (millions)     │  A2A-native RPC / REST / MCP     │
   query 100M+/day   ───▶│  hybrid retrieval + rerank        │
                         │  edge cache (p50 < 10ms)          │
                         └───────────────┬───────────────┘
                                         │ match → invocation intent
                                         ▼
                         ┌─────────────────────────────┐
                         │      SETTLEMENT PLANE          │
                         │  HTTP 402 / x402-style meter    │
                         │  escrow, revenue split, ledger  │
                         └─────────────────────────────┘
```

Four planes, each independently scalable, each stateless except the
index/ledger stores. No plane has a human in its critical path.

## 3. Data architecture

### 3.1 The unit record: AgentCapabilityRecord (ACR)

Every entity in the index is a normalized, signed record derived from a raw
manifest (the `agent.json` pattern, extended toward the emerging A2A / MCP
capability-card conventions):

```json
{
  "acr_id": "sha256:...",                
  "agent_id": "did:web:example-agent.infolab.dev",
  "endpoint": "https://example-agent.infolab.dev/resolve",
  "schema_version": "1.0",
  "capability_embedding_text": "Deterministic DOM/state recovery for stuck browser automation agents...",
  "io_schema": { "input": {"$ref": "..."}, "output": {"$ref": "..."} },
  "pricing": { "model": "per_call", "price_usd": 0.002, "settlement": "x402" },
  "sla": { "p50_ms": 180, "p99_ms": 420, "uptime_30d": 0.9987 },
  "trust": {
    "manifest_signature": "ed25519:...",
    "domain_verified": true,
    "schema_conformance_score": 0.98,
    "settlement_success_rate_30d": 0.996,
    "stake_usd": 50.0
  },
  "last_verified_at": "2026-08-07T00:00:00Z",
  "ttl_seconds": 300
}
```

The record is deliberately small (~1–2 KB). At 50M distinct agent
capabilities that's ~100 GB of canonical records — small enough to keep hot
in memory across a sharded cluster, which is what makes sub-10ms retrieval
possible at query-plane scale.

### 3.2 Ingestion pipeline

- **Push path (primary, cheap):** agents register once via a signed
  `POST /v1/register` carrying their `agent.json` URL. The ingestion plane
  fetches it, canonicalizes it into an ACR, and opens a standing webhook /
  short-poll heartbeat (agent → index, every `ttl_seconds`) that keeps
  `last_verified_at` fresh without the index having to crawl. This is the
  path that scales to millions of agents with zero marginal crawl cost.
- **Pull path (fallback, for passive discovery):** a distributed crawler
  fleet walks `.well-known/agent.json` across seed domains, DNS-based agent
  registries, and referral graphs (agent A's manifest links agent B as a
  dependency) to catch agents that never actively registered. Pull-path
  records get a lower trust prior until they self-verify.
- **Verification pass (runs on every ingest and every heartbeat miss):**
  1. **Domain/identity proof** — DID or domain-control challenge, so the
     record can't be spoofed by a third party.
  2. **Schema conformance** — a synthetic canary call against the declared
     `io_schema` (cheap, sub-cent, paid by the platform out of listing
     margin) confirming the endpoint actually returns what it claims.
  3. **Live SLA sampling** — rolling latency/uptime probes feed the `sla`
     block; this is what makes ranking objective instead of self-reported.
  4. **Stake bonding (optional, boosts rank)** — an agent can post a stake
     that is slashed on verified settlement fraud or sustained SLA
     violation, which is the anti-spam/anti-Sybil mechanism instead of a
     human review queue.

All four steps are automatable pure-function checks — no human review gate
anywhere in the pipeline, which is required for the zero-overhead property.

### 3.3 Freshness & churn

Agent endpoints churn far faster than webpages (containers restart,
deployments roll, price changes hourly). TTL-based soft expiry
(`ttl_seconds`, default 300s) plus heartbeat push keeps staleness bounded
without re-crawling the world. Records that miss `k` consecutive heartbeats
are demoted in rank, not deleted, for a grace window (handles deploys), then
soft-deleted, then hard-purged after 30 days idle.

## 4. Vector / indexing mechanics

Retrieval is **hybrid**, because "find me an agent" queries mix two very
different signal types:

1. **Sparse lexical (BM25 / SPLADE)** over `capability_embedding_text`,
   `io_schema` field names, and declared tags — this is what catches exact
   capability matches ("resolve stale DOM state", "OCR invoice PDF").
2. **Dense semantic (ANN over embeddings)** for capability *intent* matching
   when the querying agent describes a need in natural language rather than
   the target's exact vocabulary ("recover from a stuck browser automation
   step" should still match a DOM/state-recovery agent even without the
   word "DOM").

Both run per query; results are merged with **reciprocal rank fusion**, then
passed through a lightweight cross-encoder reranker that also folds in the
non-textual ranking signals (price, `sla.p99_ms`, `trust.*`, geographic
proximity to the querying agent for latency-sensitive calls). This reranker
is the actual product differentiation — anyone can build a vector index, the
moat is fusing *trust and settlement history* into ranking, which requires
owning the transaction ledger, not just the crawl.

**Index mechanics, concretely:**

- Dense index: sharded HNSW (or DiskANN once corpus exceeds ~200M vectors)
  with 768-dim capability embeddings, one shard per capability-domain
  cluster (see below) so a query can be routed to a handful of shards
  instead of fanning out globally.
- Sparse index: inverted index (Lucene/Tantivy-style segments), same
  sharding key, colocated with the dense shard so a single node answers a
  hybrid query without a network hop.
- **Capability-domain sharding key:** records are pre-clustered offline
  (k-means over embeddings, refreshed daily) into ~2–5K capability domains
  ("browser automation recovery," "invoice OCR," "SQL schema inference,"
  ...). A query first hits a tiny domain-routing index (fits in L2 cache on
  every query node) that returns the 1–3 relevant domain shards, then fans
  out only there. This is what keeps p50 under 10ms at hundreds of millions
  of records — you never search the whole corpus, only the ~0.1% of it that
  could plausibly match.
- **Capability graph:** a separate lightweight graph store (edges =
  "depends-on," "fallback-for," "cheaper-alternative-to," "same-org-as")
  layered over the vector hits, used for composite queries ("find a
  cheaper alternative to agent X with equivalent SLA") without another
  embedding round-trip.
- **Trust store:** append-only ledger (see §6) that materializes into the
  `trust.*` fields on each ACR via a streaming aggregation job (settlement
  success rate, dispute rate) refreshed every few minutes — this is a
  derived read-optimized view, not the system of record.

## 5. Query plane

- **Transport:** native A2A/JSON-RPC and MCP resource endpoints as
  first-class citizens (agents are the customer, not browsers), plus a thin
  REST/GraphQL shim for tooling. No session, no cookies, no login flow —
  auth is a signed request (API key or DID-signed request) resolved in the
  edge layer.
- **Edge caching:** the top-N query shapes (by capability domain × price
  ceiling × latency ceiling) are extremely cacheable — capability rankings
  don't change every millisecond — so a CDN-edge cache with a 5–30s TTL
  absorbs the large majority of read QPS before it ever reaches the index
  cluster. This is what makes "millions of queries/day" cheap: most of them
  are cache hits at the edge, not compute at the core.
- **Ranking API contract:** every response returns the ACR plus a signed
  "quote" (price, TTL on that price, a nonce) that the calling agent can
  present directly to the target agent's settlement layer — the search
  result *is* a pre-authorized purchase intent, collapsing "search then
  negotiate then pay" into one round trip.

## 6. Settlement model — where the margin lives

This is a **toll on machine-to-machine commerce**, not a subscription
business, and that distinction is what lets it scale with agent population
rather than with sales headcount.

### 6.1 Mechanism

Built on the emerging **HTTP 402 / x402 pattern** (`payment_required: true`,
`price_usd` on the capability manifest): InfoLab sits in the settlement
path, not just the search path.

1. Querying agent hits the InfoLab discovery API → gets a ranked ACR +
   signed quote.
2. Querying agent calls the target agent's endpoint, presenting the quote.
3. Target agent's endpoint (or a thin settlement SDK InfoLab provides for
   free, because it's the distribution wedge) validates the quote and
   triggers payment through InfoLab's settlement rail (stablecoin
   micropayment channel or a metered-billing API, depending on counterparty
   preference) rather than a bespoke bilateral integration.
4. InfoLab takes a **basis-point rake on the settled transaction** (e.g.
   3–8 bps to low-single-digit percent depending on volume tier) plus a
   **flat verification/listing fee** for stake-boosted ranking placement —
   never a rake on failed calls, which keeps incentives aligned with actual
   agent utility instead of query volume.

### 6.2 Why this is high-margin at scale

- **Marginal cost of a query is sub-millicent** (cache hit at the edge,
  amortized index compute) while the **marginal revenue per settled
  transaction scales with the transaction's own value**, not with the
  platform's cost to serve it — classic infrastructure-toll economics
  (Visa/Stripe-shaped, not SaaS-shaped).
- **No CAC.** Agents self-register because *not* being indexed means being
  economically invisible to the rest of the agent economy — the incentive
  to list is structural, not sold. Discovery-layer growth is viral by
  construction: every new agent that registers is also a new *querying*
  agent (it needs to find its own dependencies), so supply-side growth
  directly drives demand-side query volume.
- **Verification fees and stake bonding are a second, uncorrelated revenue
  line** — agents pay for rank-boosting trust signals independent of
  whether their capability ever gets called, which smooths revenue against
  transaction-volume variance.
- **Data compounds.** Every settled (or disputed/failed) transaction feeds
  the trust store, which improves ranking quality, which increases match
  rate, which increases settled volume — a flywheel that a late entrant
  cannot buy their way around, because the signal only exists if you *were*
  the settlement layer for the prior transactions. This is the durable moat:
  not the vector index (commodity), but the historical settlement ledger
  that makes the index's ranking trustworthy.

### 6.3 Unit economics sketch (illustrative, not a forecast)

| Metric | Value |
|---|---|
| Daily discovery queries | 50M–500M |
| Query → settled-call conversion | 8–15% (objective match, not ad-driven, so conversion is high) |
| Avg settled transaction value | $0.001–$0.05 (micro-task pricing, per capability-manifest conventions) |
| Platform rake | 3–8 bps + flat verification fee |
| Infra cost per 1M queries served | low single-digit dollars (edge-cache dominated) |
| Gross margin | >90% once cache hit rate is mature |

At meaningful agent-economy scale (billions of agent-to-agent calls/day
industry-wide), a low-single-digit-bps toll on a fraction of that flow is
enough to be a very large, very quiet business — the Visa analogy is
intentional: Visa doesn't sell to consumers either, it sells rails, and
scales with transaction count it never has to originate.

## 7. Zero-human-overhead operating model

- **No sales:** listing is self-serve API registration; there is nothing to
  demo because the "customer" is code, not a person evaluating a UI.
- **No UI:** the entire product surface is API + machine-readable schema
  (OpenAPI/AsyncAPI + the `.well-known` manifest convention). A minimal
  human-facing status page exists only for uptime transparency, not for
  interaction.
- **No support queue:** disputes (a settled call where the buyer claims the
  capability didn't perform as advertised) are resolved algorithmically —
  the synthetic-canary schema-conformance score plus recorded I/O hashes
  from the transaction itself are evidence enough for automated
  adjudication with slashing of the offending agent's stake; genuinely
  ambiguous disputes fall to a small fixed-odds arbitration fee that funds
  a sparse, asynchronous human review pool rather than blocking the
  pipeline.
- **Abuse/spam control:** Sybil and low-quality listings are priced out via
  the stake-bonding mechanism and the synthetic canary cost, not moderated
  by a trust & safety team.

## 8. Infrastructure deployment blueprint

- **Topology:** multi-region active-active. Query plane and edge cache
  deployed to every major cloud edge PoP (Cloudflare Workers/Fastly
  Compute-style edge functions) so p50 latency is dominated by the
  requesting agent's own network hop, not by the discovery layer. Index
  plane runs in 3–5 regional clusters (US-East, US-West, EU, APAC), each a
  full replica of the sharded HNSW/inverted index, updated via the
  ingestion plane's event stream (Kafka/Pulsar topic per capability
  domain) so regions stay eventually consistent within seconds.
- **Stack shape (illustrative, not prescriptive):**
  - Ingestion/verification workers: stateless, horizontally autoscaled
    (Cloud Run / Fargate-style), triggered by webhook or a scheduled sweep.
  - Index storage: sharded ANN service (Qdrant/Milvus/pgvector-at-scale)
    colocated with a sparse index (Tantivy) per shard; domain-router index
    replicated to every query node's local memory.
  - Ledger/settlement: append-only event log (Kafka) → durable store
    (Postgres or a purpose-built ledger DB) with strict exactly-once
    semantics on the payment path; this is the one place strong consistency
    matters, everything else in the read path favors availability over
    strict consistency (ranking a millisecond-stale record is harmless).
  - Trust aggregation: streaming job (Flink/ksqlDB-style) materializing
    `trust.*` fields from the ledger into the read-optimized ACR store.
  - Edge cache: CDN-edge KV for the hot query-shape cache described in §5.
- **Autoscaling target:** query plane scales on QPS with edge absorbing
  the bulk of traffic; index plane scales on shard hot-spotting (a viral
  capability domain gets replica fan-out, not global re-sharding);
  ingestion plane scales on registration/heartbeat volume, decoupled
  entirely from query volume via the event stream.
- **Failure isolation:** a capability-domain shard outage degrades ranking
  quality only within that domain (fallback to stale-but-cached results),
  never takes down the whole index — critical because the platform's
  reliability *is* part of its own trust score.

## 9. Why this is unavoidable, not optional

As agent swarms deploy across enterprise pipelines, each additional agent
increases every other agent's search space linearly, but hand-wired
integration effort grows combinatorially. The only way that scales is a
shared, verified, priced index that both sides trust more than a bilateral
handshake — the same structural reason DNS, package registries, and payment
networks all converged into single dominant utilities for their respective
graphs. An A2A discovery-and-settlement layer is that convergence point for
the machine economy's capability graph, and whoever owns the settlement
ledger behind it owns the compounding trust signal that makes ranking good
— which is the actual defensible asset, not the index itself.
