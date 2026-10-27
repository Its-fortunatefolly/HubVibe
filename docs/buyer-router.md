# HubVibe buyer router v1.3.0

`integrations/hubvibe_router.py` is a single-file buyer client for autonomous
agents. It discovers and verifies a HubVibe capability, enforces the buyer's
local authorization, signs at most one payment identity, verifies settlement
and delivery, and returns the result. The wallet stays on the buyer's machine.

Version 1.3.0 keeps the v1.2 constructors, `quote`, `verify`, `call`, `a2a`,
`execute_task`, CLI commands, environment variables, and result bodies. Its new
arguments are optional keyword arguments.

## Install

Use Python 3.10 or later on Linux or macOS. Clone the existing public repository
or download the one client file:

```bash
git clone --depth 1 https://github.com/Its-fortunatefolly/HubVibe.git
cd HubVibe

# Prepaid API-key and walletless discovery use only httpx.
python -m pip install httpx

# Install the official x402 signer only for the wallet rail the agent uses.
python -m pip install "x402[evm]"  # USDC on Base
# python -m pip install "x402[svm]"  # USDC on Solana
```

The router reads secrets from environment variables or local files. Never put a
wallet key or prepaid key in source control:

```bash
export HUBVIBE_WALLET_FILE="$HOME/.hubvibe-wallet-key"
export HUBVIBE_MAX_PRICE_USD="1.00"
export HUBVIBE_BUDGET_USD="10.00"
export HUBVIBE_DAILY_CAP_USD="25.00"
export HUBVIBE_HOME="$HOME/.hubvibe-router"
```

For a prepaid key, set `HUBVIBE_API_KEY` instead. For Solana, use
`HUBVIBE_SOLANA_FILE` and `HUBVIBE_RAIL=solana`.

Confirm the local configuration without purchasing anything:

```bash
python integrations/hubvibe_router.py status
python integrations/hubvibe_router.py quote \
  /work/market/quote '{"product_id":"BTC-USD"}'
python integrations/hubvibe_router.py verify \
  /work/market/quote '{"product_id":"BTC-USD"}'
```

`quote`, `verify`, discovery endpoints, and `status` do not authorize a
purchase. Without a wallet or prepaid key, a paid call returns the structured
reason `payment_not_configured` before signing or spending anything.

## Call from an agent

```python
from integrations.hubvibe_router import Router

buyer = Router.from_env()

# Reuse this key only for retries of this exact logical request and payload.
result = buyer.execute_task(
    tool="market.quote",
    payload={"product_id": "BTC-USD"},
    idempotency_key="portfolio-agent:market.quote:2026-10-27T16:00Z:BTC-USD",
)
```

An idempotency key is durable in `HUBVIBE_HOME`. A retry with the same key and
payload reuses the verified delivery. Reusing the key with a different route or
payload fails before another signature is created. If settlement is unknown,
the router keeps the reservation and refuses to sign that identity again.

For agents that speak HTTP, run the local proxy:

```bash
python integrations/hubvibe_router.py serve --host 127.0.0.1 --port 8402
```

Then send the same HubVibe route and JSON body to `http://127.0.0.1:8402`.
Keep the proxy bound to loopback unless the surrounding runtime provides its
own authentication and network isolation.

## Multi-worker authorization

Discovery may suggest capabilities, but it does not authorize spending. Build
and inspect a plan, give the exact total to the buyer's policy or operator, and
authorize only that frozen plan:

```python
plan = buyer.prepare_plan([
    {"tool": "market.quote", "payload": {"product_id": "BTC-USD"}},
    {"tool": "market.prediction", "payload": {"query": "bitcoin"}},
])

authorization = buyer.authorize_plan(
    plan,
    max_total_usd=0.15,
    expires_in=300,
)
results = buyer.execute_plan(plan, authorization)
```

The router deduplicates identical tasks, rejects a composite alongside a
component already included by that composite, reserves the whole workflow
budget before the first signature, and refuses changed or expired plans.

## Delivery and spending guarantees

- A file lock makes the daily budget decision atomic across threads and local
  processes sharing `HUBVIBE_HOME`.
- A reservation exists before payment signing. Explicit unbilled failures
  release it; unknown settlement outcomes do not.
- A paid worker result is accepted and cached only after the settlement and
  `/work/receipts/{id}` delivery receipt match the route, amount, transaction,
  request, and result.
- Deferred HTTP 202 jobs are collected from their free `collect_url` without a
  second payment.
- Local ledgers and idempotency records contain purchase metadata, never wallet
  private keys.

The process budget is local to one `Router` instance. The daily cap is shared
by every instance and process using the same `HUBVIBE_HOME`. Machines with
separate homes do not share a cap; give each machine its own explicit budget.

## Test without funds

The public tests use only a local fake HTTP node. Signing is stubbed at the x402
boundary, so they require no wallet, chain access, credentials, or paid calls:

```bash
python -m pip install pytest httpx
python -m pytest tests/test_hubvibe_router.py -q
```

The suite covers concurrent threads and processes, shared budgets, durable and
conflicting idempotency keys, process restart and expiration, unknown
settlement, receipt mismatches, deferred delivery, composite authorization,
and walletless fail-closed behavior.
