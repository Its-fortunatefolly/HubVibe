# Privacy/Cookie Compliance Scanner

A metered agent that scans a live URL for the concrete signals GDPR/CCPA
cookie-consent complaints actually point to: known tracking cookies present
before any consent interaction, whether a known consent-management
platform (CMP) is present, and whether privacy/cookie policy links exist.
See `app/scanner.py` for the full rule set — it's a static, verifiable
lookup against known tracker cookie names and known CMP script signatures
(the same class of technique commercial tools like Cookiebot/Osano use),
not an LLM guessing at compliance. An optional AI layer (Gemini, same
`GEMINI_API_KEY` as `wcag-audit-engine`) turns the rule-based findings into
plain-language remediation notes -- it never decides what the findings
are or whether the scan is "clean."

**This is a risk-reduction signal, not a legal compliance certification.**
It can't see server-side tracking, and its tracker/CMP database isn't
exhaustive. Copy throughout (landing page, API responses) says this
explicitly, for the same reason `wcag-audit-engine` does: overstating what
an automated scan proves is how tools in this exact space (accessibility
overlays) have ended up in FTC and legal trouble.

## Why this is a second product, not a rewrite

Reuses the pattern proven out in `wcag-audit-engine`: real rule-based scan,
Stripe metered billing via Meter Events, Firestore for the
`api_key -> customer_id` mapping only, a landing page with a free-scan lead
magnet, and a checkout flow that actually delivers the API key. Firestore
collections are prefixed `privacy_` so this can share a GCP project with
`wcag-audit-engine` without mixing customer data. Sells to the same buyer
(small business owner worried about a specific legal-risk category) as
WCAG, so the highest-leverage move is offering both to one acquired
customer, not running two separate cold-start funnels.

## API

`POST /scan` — full report (auth required)
Headers: `X-API-Key: <key>`
Body: `{"url": "https://..."}`

```json
{
  "status": "ok",
  "clean": false,
  "pre_consent_trackers": [{"cookie": "_ga", "vendor": "Google Analytics", "category": "analytics"}],
  "detected_cmps": [],
  "has_privacy_link": true,
  "has_cookie_policy_link": false,
  "findings": [
    {"id": "trackers-before-consent", "severity": "high", "detail": "..."},
    {"id": "no-cookie-policy-link", "severity": "low", "detail": "..."}
  ]
}
```

`POST /scan/free` — same scan, IP-rate-limited (3/day), returns only a
finding count + top 3 finding IDs.

On scan failure: HTTP 502, `{"status": "error", "clean": null, "detail": "..."}`.
Failed scans are never billed.

## Getting paid / deployment

Identical flow to `wcag-audit-engine` (see that service's README for the
full walkthrough) with different env var names so both services can share
one Stripe account without colliding:

- `PRIVACY_STRIPE_METERED_PRICE_ID` / `PRIVACY_STRIPE_METER_EVENT_NAME`
  instead of `STRIPE_METERED_PRICE_ID` / `STRIPE_METER_EVENT_NAME`
- `SCANNER_API_KEY` instead of `AUDIT_API_KEY` for the internal/testing key
- `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` are shared if deployed
  under the same Stripe account (each service still needs its own webhook
  endpoint registered in the Stripe Dashboard, pointing at its own
  `/billing/webhook` URL)

```bash
gcloud run deploy privacy-compliance-scanner \
  --source=privacy-compliance-scanner \
  --region=us-central1 \
  --memory=1Gi \
  --set-env-vars=PRIVACY_STRIPE_METERED_PRICE_ID=price_...,PRIVACY_STRIPE_METER_EVENT_NAME=privacy_scan_call \
  --set-secrets=SCANNER_API_KEY=scanner-api-key:latest,STRIPE_SECRET_KEY=stripe-secret-key:latest,STRIPE_WEBHOOK_SECRET=privacy-stripe-webhook-secret:latest
```

## Local development

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
