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
`{"status": "error", "pass": null, "detail": "..."}`.

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
echo -n "your-gemini-key"  | gcloud secrets create gemini-api-key  --data-file=-
echo -n "your-audit-key"   | gcloud secrets create audit-api-key   --data-file=-
```

Build and deploy:

```bash
gcloud run deploy wcag-audit-engine \
  --source=wcag-audit-engine \
  --region=us-central1 \
  --memory=1Gi \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,AUDIT_API_KEY=audit-api-key:latest
```

`--allow-unauthenticated` at the Cloud Run/IAM layer is fine here (or
omit it and front the service with your own gateway) — request-level access
control is enforced in the application via `X-API-Key`, which is what
actually meters and gates paid usage. For real production traffic, put a
quota policy (API Gateway or Cloud Armor) in front of this too: the
in-process rate limiter in `app/main.py` is per-instance and won't hold once
Cloud Run scales out to multiple instances.
