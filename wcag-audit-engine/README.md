# HubVibe Site Compliance Auditing Suite

A metered A2A service with five real, rule-based audit endpoints:
**accessibility** (WCAG 2.1 A/AA via
[axe-core](https://github.com/dequelabs/axe-core)/
[axe-playwright-python](https://pypi.org/project/axe-playwright-python/)),
**SEO** (title/meta/OpenGraph/structured-data), **security** (HTTPS/HSTS/
CSP/CORS response headers), **performance** (DOM size/payload weight/request
count from a real page load), and a **bundle** that runs all four in one
call. An optional AI layer (Gemini) can generate plain-language remediation
notes on the accessibility results, but it never decides pass/fail — that's
always the deterministic rule engine for each check (see `app/audits.py`
for SEO/security/performance).

## Why this exists

An earlier draft of this service used an LLM as the sole auditor and
silently returned `{"pass": true}` whenever anything errored, on the theory
that a paying customer's CI/CD pipeline should never see a failure. That
design was rejected: a compliance-shaped API that fabricates a "pass" on
error isn't fail-safe, it's a false-positive generator — anyone relying on
it (including for legal accessibility obligations) would have no way to
tell "verified compliant" from "the check never ran." This version instead:

- **Runs a real, deterministic rule engine** (axe-core) instead of asking an
  LLM to guess at conformance.
- **Never coerces an error into a pass.** If the audit can't run, `/audit`
  returns HTTP 502 with `"pass": null`, not `"pass": true`.
- **Requires an API key, a verified x402 payment, or a verified MPP
  payment** (`X-API-Key`, checked with constant-time comparison; `X-PAYMENT`,
  verified and settled against an x402 facilitator; or `Authorization:
  Payment ...`, verified directly against Stripe or the Tempo network) and
  rate-limits per key, since this is a metered endpoint sitting in front of
  a paid LLM call — an open, unauthenticated endpoint is a wallet-drain
  vector, not just a security gap. None present, or all invalid, always
  gets HTTP 402 with everything needed to pay — never a fallback pass.
- **Keeps secrets out of the deploy command.** `GEMINI_API_KEY` and
  `AUDIT_API_KEY` are meant to be provisioned via Secret Manager, not
  `--set-env-vars` (which lands in shell history and Cloud Build logs).

## API

Five paid audit routes, all with identical auth (see below) and identical
honest-failure behavior (502 + `"pass": null` if the check itself couldn't
run, never billed):

| Route | Input | Price | Checks |
|---|---|---|---|
| `POST /audit` | `html` or `url` | $0.03 | Alias of `/audit/wcag`, kept for compatibility |
| `POST /audit/wcag` | `html` or `url` | $0.03 | WCAG 2.1 A/AA via axe-core |
| `POST /audit/seo` | `html` or `url` | $0.03 | Title, meta description, H1s, canonical, OpenGraph, structured data, lang |
| `POST /audit/security` | `url` (required) | $0.03 | HTTPS, HSTS, CSP, X-Content-Type-Options, frame protection, Referrer-Policy, CORS |
| `POST /audit/performance` | `url` (required) | $0.03 | DOM node count, transferred bytes, request count from one real page load |
| `POST /audit/bundle` | `url` (required) | $0.10 | All four above, atomically -- if any fails, the whole call fails and nothing is billed |

`security`/`performance`/`bundle` need a live, fetchable URL (they inspect a
real HTTP response / real browser load) -- raw HTML alone isn't enough for
those three, unlike `wcag`/`seo`.

Headers on every route: `X-API-Key: <key>` **or**
`X-PAYMENT: <x402 signed payment>` **or**
`Authorization: Payment <base64url MPP credential>`

If none of those is present or valid, the response is HTTP 402 with both:
- the x402-style JSON body (for callers reading price/payTo out of the body)
- one `WWW-Authenticate: Payment ...` header per configured MPP method (the
  spec-conformant challenge -- see mppx validate below), each carrying its
  own base64url-encoded `request` with method-specific price/recipient/etc.

```json
{
  "x402Version": 1,
  "scheme": "exact",
  "network": "eip155:8453",
  "price": "$0.03",
  "payTo": "0x...",
  "accepted_payment_header": "X-PAYMENT",
  "alternative": "X-API-Key header (Stripe-based billing) is also accepted"
}
```

```json
{
  "status": "ok",
  "pass": false,
  "engine": "axe-core",
  "ruleset": "wcag2a, wcag2aa, wcag21a, wcag21aa",
  "violations": [
    {"id": "image-alt", "impact": "critical", "help": "Images must have alternate text", "help_url": "...", "nodes_affected": 2}
  ],
  "remediation": {"ai_generated": true, "notes": "..."}
}
```

On audit failure (browser crash, invalid input, timeout): HTTP 502,
`{"status": "error", "pass": null, "detail": "..."}`. Failed audits are
never billed.

## Growth surface

`/` serves a landing page (`app/static/index.html`), not the health check
(that lives at `/health`; Cloud Run's frontend reserves `/healthz`).

The page exists for humans evaluating the service — buyers, partners, and
anyone verifying the business is real — but the product itself is the
machine API, and the page is written that way: it leads with per-call
pricing and the 402 payment handshake, not with a subscription pitch.

There is deliberately **no free scan**. An audit costs a real browser page
load, so giving them away funds strangers' compute at our expense and
invites abuse. The only human path is `/billing/checkout`, which issues an
API key; machines pay per call over x402/MPP.

## Getting paid

Two human/machine tiers, both real:

- **Agency / Developer Plan** ($49/month, Stripe subscription Checkout):
  1,500 scans/month included across all five routes; once a subscriber
  exceeds that in a calendar month, their `X-API-Key` alone is no longer
  sufficient (`billing.check_and_increment_quota`, Firestore-backed, reset
  monthly) and the call falls through to x402/MPP per-call payment instead
  -- same three-way auth check every route already does, so "overage"
  isn't a special code path, just the existing fallback kicking in.
- **Programmatic machine payments** ($0.03/call individual, $0.10/call
  bundle): x402 or MPP, no subscription, no signup -- see the two sections
  below.

Every real call also still reports a Stripe Meter Event
(`billing.record_usage`) regardless of which auth path was used to bill it
via Stripe, so usage history stays centralized in Stripe either way. Stripe
owns the balance and the invoicing — this service only stores a thin
`api_key -> Stripe customer_id` mapping in Firestore, so there's no custom
balance-tracking code that can drift from what Stripe actually charges.
(The $0.10 bundle price is approximated as 3 Meter Events, ~$0.09, against
the existing flat per-event meter rather than requiring a second Stripe
Price/meter just for this -- see `record_usage`'s docstring.)

Customer-facing flow (all handled by this service, no external pages
required to make it work):

1. `POST /billing/checkout {"email": "..."}` -> `{"checkout_url": "..."}`;
   the landing page redirects the customer there.
2. Stripe redirects back to `/billing/success?session_id=...` (or your own
   `CHECKOUT_SUCCESS_URL`, if you set one to override the default). That
   page polls `GET /billing/api-key` until the webhook lands and displays
   the customer's key with a ready-to-run `curl` example.
3. The customer uses that key as `X-API-Key` on any `/audit*` route. Every
   successful audit reports usage to Stripe; Stripe bills them on its
   normal cycle.

One-time setup in the Stripe Dashboard (not something this code can do for
you — it needs your Stripe account):

1. **Product catalog**: create a flat recurring Price (e.g. $49/month,
   *not* `usage_type: metered`) for the Agency/Developer plan. Note the
   Price ID -> `STRIPE_FLAT_SUBSCRIPTION_PRICE_ID`. If you skip this,
   checkout falls back to the pure-metered price below so nothing breaks
   -- but the "$49/month, 1,500 included" landing-page copy is only
   actually true once this exists.
2. **Billing > Meters**: create a meter (e.g. event name `wcag_audit_call`,
   aggregation = count).
3. **Product catalog**: create a recurring Price with `usage_type: metered`
   attached to that meter (e.g. $0.03 per unit). Note the Price ID ->
   `STRIPE_METERED_PRICE_ID`.
4. **Developers > Webhooks**: add an endpoint at
   `https://<your-service>/billing/webhook` subscribed to
   `checkout.session.completed`. Note the signing secret.

`SAAS_MONTHLY_QUOTA` (default `1500`) controls the included-scans cap.

Then provision the secrets and deploy (see below). `AUDIT_API_KEY` remains
available as an internal/testing key that bypasses Stripe entirely — leave
it unset once real customers exist, or keep it only for your own smoke
tests.

### Getting paid without Stripe: x402

`/audit` also accepts a per-request x402 payment (`X-PAYMENT` header) as an
alternative to a Stripe-issued API key — for AI agents that can pay
on-the-fly without a human first setting up billing. Verification and
settlement are delegated entirely to a facilitator via the official
[`x402`](https://pypi.org/project/x402/) package
(`wcag-audit-engine/app/x402_payments.py`); this service never hand-rolls
signature checking, and fails closed (rejects with 402) on any missing
config, malformed header, or facilitator error.

Until these are set, `x402_payments.is_configured()` is `False` and every
`X-PAYMENT` header is rejected — the Stripe `X-API-Key` path is unaffected
either way:

- `X402_FACILITATOR_URL` — your facilitator's base URL (Stripe now offers a
  native x402 facilitator on the same Stripe account used above; any
  x402-compatible facilitator works).
- `X402_PAY_TO_ADDRESS` — the wallet address that receives payment.
- `X402_NETWORK` — CAIP-2 network id (default `eip155:8453`, Base mainnet).
- `X402_PRICE` — default `$0.03`.

### Getting paid without Stripe subscriptions: MPP

`/audit` also accepts [MPP](https://docs.stripe.com/payments/machine/mpp)
(Machine Payments Protocol -- an open standard co-authored by Stripe and
Tempo) via `Authorization: Payment <credential>`. Unlike x402 (crypto-only,
Coinbase-authored) or the Stripe subscription flow above (human sets up
billing once, in advance), MPP covers **both** fiat and crypto per-request,
with no advance setup on the payer's side:

- **stripe** method: a single-use Stripe Shared Payment Token (`spt_...`).
  The server creates and confirms a PaymentIntent with
  `shared_payment_granted_token=<spt>` -- Stripe enforces single-use on the
  token itself.
- **tempo** method: USDC on the Tempo network, "push" mode only -- the
  caller broadcasts their own signed transfer and hands us the tx hash; the
  server fetches the receipt and checks the `Transfer` event log matches the
  challenge's amount/recipient/token. (Pull mode and the zero-amount
  EIP-712 "proof" credential type aren't implemented -- both fail closed.)

There's no official Python SDK for MPP (only the Node `mppx` package), so
`wcag-audit-engine/app/mpp_payments.py` hand-implements the wire protocol
directly against the published spec
([tempoxyz/mpp-specs](https://github.com/tempoxyz/mpp-specs)): the
challenge/response headers, the HMAC-based stateless challenge binding
(derived from `STRIPE_SECRET_KEY`, so no separate signing key to manage),
and the per-method request/payload shapes. Validate any running instance
against the reference implementation:

```bash
npx mppx@latest validate http://localhost:8000 \
  --endpoint "POST:/audit" \
  --header "Content-Type:application/json" \
  --body '{"url":"https://example.com"}'
```

Until each method's vars are set, it isn't offered (no `WWW-Authenticate`
header on a 402 for that method) and stays inert:

- **stripe** method needs `MPP_STRIPE_NETWORK_PROFILE_ID` (your Stripe
  Business Network Profile ID -- Dashboard → "Stripe profile" → Get
  started, in **live** mode; no Product/Price needed, unlike the
  subscription flow above). `MPP_STRIPE_PRICE_CENTS` (default `3`, i.e.
  $0.03), `MPP_STRIPE_CURRENCY` (default `usd`), and
  `MPP_STRIPE_API_VERSION` (default `2026-05-27.preview`) are optional.
- **tempo** method needs only `MPP_TEMPO_RECIPIENT_ADDRESS` -- everything
  else defaults to Tempo mainnet's real values (sourced from Tempo's own
  SDK, not guessed): `MPP_TEMPO_RPC_URL` defaults to
  `https://rpc.tempo.xyz`, `MPP_TEMPO_TOKEN_ADDRESS` defaults to the actual
  mainnet USDC.e contract `0x20C000000000000000000000b9537d11c60E8b50`, and
  `MPP_TEMPO_CHAIN_ID` defaults to `4217`. `MPP_TEMPO_PRICE_BASE_UNITS`
  defaults to `30000` ($0.03 at USDC's 6 decimals).

  The simplest way to get `MPP_TEMPO_RECIPIENT_ADDRESS`: let Stripe custody
  and auto-convert the funds instead of running your own wallet, via
  Stripe's crypto deposit-address API (needs your live `STRIPE_SECRET_KEY`,
  pulled from Secret Manager rather than typed/pasted anywhere):

  ```bash
  STRIPE_SECRET_KEY=$(gcloud secrets versions access latest --secret=stripe-secret-key)
  curl https://api.stripe.com/v1/crypto/deposit_addresses \
    -u "$STRIPE_SECRET_KEY:" \
    -H "Stripe-Version: 2026-05-27.preview" \
    -d network=tempo
  ```

  The response's `address` field is the value to use.
- `MPP_REALM` — optional; the challenge realm defaults to the request's own
  `Host` header (minus port), which is what the spec calls for and is what
  `mppx validate` checks for. Only set this to override that.

## Pipeline integrations

- `integrations/langchain_tool.py` — a LangChain `@tool`-decorated function
  calling `/audit/bundle`; recent CrewAI versions accept LangChain tools
  directly, so this works for both without a separate wrapper. Auth is
  `HUBVIBE_API_KEY` only (x402/MPP payment construction is out of scope for
  a thin tool wrapper -- use the `x402`/`mppx` client libraries directly if
  an agent should pay per-call instead of holding a subscription key).
- `integrations/github_action.yml` — a copy-paste GitHub Actions workflow
  that runs `/audit/bundle` as a CI/CD gate and fails the build on either a
  failed audit or a failed/unauthenticated request.
- `app/static/mcp.json`, served live at `/mcp.json` — tool definitions for
  all five routes in MCP's `{name, description, inputSchema}` shape, with a
  non-standard `httpEndpoint` extension mapping each onto its actual route
  and price, since this is a plain REST API, not a live MCP stdio/SSE
  server. The same schema is also served live at `/.well-known/agent.json`.

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The `mcr.microsoft.com/playwright/python` base image ships Chromium
pre-installed for the container build; for local runs outside that image,
install it once with `python -m playwright install --with-deps chromium`.

## Deployment (Cloud Run)

Create the secrets once:

```bash
echo -n "your-gemini-key"       | gcloud secrets create gemini-api-key        --data-file=-
echo -n "your-audit-key"        | gcloud secrets create audit-api-key         --data-file=-
echo -n "sk_live_..."           | gcloud secrets create stripe-secret-key     --data-file=-
echo -n "whsec_..."             | gcloud secrets create stripe-webhook-secret --data-file=-
```

Grant the Cloud Run service account Firestore access (Meter Events and
Checkout only need the Stripe secret key, not a separate GCP grant):

```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Build and deploy. The service name and region below are the ones the live
node actually runs under — `gcloud run deploy` **creates** a service when the
name doesn't match an existing one, so deploying as `wcag-audit-engine` in
`us-central1` would quietly stand up a second, unreferenced copy rather than
update production:

```bash
gcloud run deploy hubvibe \
  --source=wcag-audit-engine \
  --region=us-south1 \
  --memory=2Gi \
  --cpu=2 \
  --concurrency=4 \
  --min-instances=1 \
  --set-env-vars=STRIPE_METERED_PRICE_ID=price_...,STRIPE_METER_EVENT_NAME=wcag_audit_call,STRIPE_PRICE_ONEOFF_REPORT=price_...,STRIPE_PRICE_PRO=price_...,STRIPE_PRICE_AGENCY=price_...,X402_FACILITATOR_URL=https://...,X402_PAY_TO_ADDRESS=0x...,X402_NETWORK=eip155:8453,X402_PRICE=\$0.03,MPP_STRIPE_NETWORK_PROFILE_ID=profile_...,MPP_TEMPO_RPC_URL=https://...,MPP_TEMPO_TOKEN_ADDRESS=0x...,MPP_TEMPO_RECIPIENT_ADDRESS=0x... \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,AUDIT_API_KEY=audit-api-key:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest
```

`STRIPE_PRICE_ONEOFF_REPORT`, `STRIPE_PRICE_PRO` and `STRIPE_PRICE_AGENCY`
are the three human plans. A tier with no Price ID set is not offered — it
is omitted from `/.well-known/agent.json` and its checkout refuses, rather
than half-working.

**Redeploying code only:** leave every flag off.

```bash
gcloud run deploy hubvibe --source=wcag-audit-engine --region=us-south1
```

`--set-env-vars` and `--set-secrets` *replace* the service's configuration
rather than adding to it, so passing a partial list on a routine code deploy
silently unsets everything you left out — which, for the payment variables,
takes the paid rails offline. Omitting them keeps the existing config.

### Why those sizing flags

They are not arbitrary, and they have to agree with `MAX_CONCURRENT_AUDITS`
(default 4) or the container will either waste money or die under load:

- `--memory=2Gi` — each concurrent audit pins a Chromium instance from the
  pool in `app/browser_pool.py`. Chromium wants roughly 300–500 MB under a
  real page load, so four concurrent audits plus the Python process does not
  fit in 1 Gi. Under-provisioning here shows up as OOM-killed requests, which
  Cloud Run reports as a 5xx with no useful traceback.
- `--concurrency=4` — matches `MAX_CONCURRENT_AUDITS`. Letting Cloud Run send
  more simultaneous requests than the app will run in parallel just queues
  them inside the container, where they burn the caller's timeout instead of
  being load-balanced onto another instance.
- `--cpu=2` — a headless page load is CPU-bound during parse/layout; one vCPU
  shared across four page loads makes every one of them slow.
- `--min-instances=1` — cold-starting this image means starting Python *and*
  launching Chromium. An agent with a short client timeout gives up before a
  cold instance ever answers, which reads as an unreliable API rather than a
  slow one. One warm instance is the difference between a machine caller
  retrying and a machine caller dropping you.

Scale the whole set together: to serve more parallel audits, raise
`MAX_CONCURRENT_AUDITS`, `--concurrency`, `--memory`, and `--cpu` in step
rather than any one alone.

(Price/event IDs aren't secrets, so `--set-env-vars` is fine for those; the
actual credentials go through `--set-secrets`. `CHECKOUT_SUCCESS_URL` /
`CHECKOUT_CANCEL_URL` default to this same service's own `/billing/success`
and `/billing/cancel` pages — only set them if you're fronting this with a
different public domain. Omit the `X402_*` vars to deploy without x402, and
the `MPP_*` vars to deploy without MPP — the Stripe `X-API-Key` path works
unchanged regardless of either.)

`--allow-unauthenticated` at the Cloud Run/IAM layer is fine here (or
omit it and front the service with your own gateway) — request-level access
control is enforced in the application via `X-API-Key`, which is what
actually meters and gates paid usage. For real production traffic, put a
quota policy (API Gateway or Cloud Armor) in front of this too: the
in-process rate limiter in `app/main.py` is per-instance and won't hold once
Cloud Run scales out to multiple instances.
