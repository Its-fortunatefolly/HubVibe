# WCAG Audit Engine

A metered A2A agent that audits HTML (or a live URL) against WCAG 2.1 A/AA
using [axe-core](https://github.com/dequelabs/axe-core) — the same
rule-based accessibility engine used by Deque's own tooling and most other
real accessibility scanners — via
[axe-playwright-python](https://pypi.org/project/axe-playwright-python/).
An optional AI layer (Gemini) can generate plain-language remediation notes,
but it never decides pass/fail — that's always axe-core's rule engine.

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

`POST /audit`
Headers: `X-API-Key: <key>` **or** `X-PAYMENT: <x402 signed payment>` **or**
`Authorization: Payment <base64url MPP credential>`
Body: `{"html": "<...>"}` or `{"url": "https://..."}`

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
(that moved to `/healthz`). The page offers:

- **A free instant scan** (`POST /scan/free`, 3/day per IP): runs the real
  axe-core audit but only returns the top 3 issues, clearly labeled as a
  one-time snapshot, not a compliance certification. It's a lead magnet,
  not a way to get the paid endpoint's full output for free.
- **A "start monitoring" button** that kicks off Stripe Checkout via
  `/billing/checkout`.

Every free scan (with or without an email left) is logged to a `leads`
Firestore collection for manual follow-up — it only stores what the visitor
submitted about their own site through the form, nothing scraped or bought.

## Getting paid

Billing is Stripe usage-based (metered) billing: a customer subscribes once
via Checkout, and every real `/audit` call reports one Meter Event. Stripe
owns the balance and the invoicing — this service only stores a thin
`api_key -> Stripe customer_id` mapping in Firestore, so there's no custom
balance-tracking code that can drift from what Stripe actually charges.

Customer-facing flow (all handled by this service, no external pages
required to make it work):

1. `POST /billing/checkout {"email": "..."}` -> `{"checkout_url": "..."}`;
   the landing page redirects the customer there.
2. Stripe redirects back to `/billing/success?session_id=...` (or your own
   `CHECKOUT_SUCCESS_URL`, if you set one to override the default). That
   page polls `GET /billing/api-key` until the webhook lands and displays
   the customer's key with a ready-to-run `curl` example.
3. The customer uses that key as `X-API-Key` on `/audit`. Every successful
   audit reports usage to Stripe; Stripe bills them on its normal cycle.

One-time setup in the Stripe Dashboard (not something this code can do for
you — it needs your Stripe account):

1. **Billing > Meters**: create a meter (e.g. event name `wcag_audit_call`,
   aggregation = count).
2. **Product catalog**: create a recurring Price with `usage_type: metered`
   attached to that meter (e.g. $0.01 per unit). Note the Price ID.
3. **Developers > Webhooks**: add an endpoint at
   `https://<your-service>/billing/webhook` subscribed to
   `checkout.session.completed`. Note the signing secret.

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

Build and deploy:

```bash
gcloud run deploy wcag-audit-engine \
  --source=wcag-audit-engine \
  --region=us-central1 \
  --memory=1Gi \
  --set-env-vars=STRIPE_METERED_PRICE_ID=price_...,STRIPE_METER_EVENT_NAME=wcag_audit_call,X402_FACILITATOR_URL=https://...,X402_PAY_TO_ADDRESS=0x...,X402_NETWORK=eip155:8453,X402_PRICE=\$0.03,MPP_STRIPE_NETWORK_PROFILE_ID=profile_...,MPP_TEMPO_RPC_URL=https://...,MPP_TEMPO_TOKEN_ADDRESS=0x...,MPP_TEMPO_RECIPIENT_ADDRESS=0x... \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,AUDIT_API_KEY=audit-api-key:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest
```

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
