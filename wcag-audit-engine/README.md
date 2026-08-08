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
- **Requires an API key** (`X-API-Key` header, checked with constant-time
  comparison) and rate-limits per key, since this is a metered endpoint
  sitting in front of a paid LLM call — an open, unauthenticated endpoint is
  a wallet-drain vector, not just a security gap.
- **Keeps secrets out of the deploy command.** `GEMINI_API_KEY` and
  `AUDIT_API_KEY` are meant to be provisioned via Secret Manager, not
  `--set-env-vars` (which lands in shell history and Cloud Build logs).

## API

`POST /audit`
Headers: `X-API-Key: <key>`
Body: `{"html": "<...>"}` or `{"url": "https://..."}`

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
  --set-env-vars=STRIPE_METERED_PRICE_ID=price_...,STRIPE_METER_EVENT_NAME=wcag_audit_call \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,AUDIT_API_KEY=audit-api-key:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest
```

(Price/event IDs aren't secrets, so `--set-env-vars` is fine for those; the
actual credentials go through `--set-secrets`. `CHECKOUT_SUCCESS_URL` /
`CHECKOUT_CANCEL_URL` default to this same service's own `/billing/success`
and `/billing/cancel` pages — only set them if you're fronting this with a
different public domain.)

`--allow-unauthenticated` at the Cloud Run/IAM layer is fine here (or
omit it and front the service with your own gateway) — request-level access
control is enforced in the application via `X-API-Key`, which is what
actually meters and gates paid usage. For real production traffic, put a
quota policy (API Gateway or Cloud Armor) in front of this too: the
in-process rate limiter in `app/main.py` is per-instance and won't hold once
Cloud Run scales out to multiple instances.
