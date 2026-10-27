# HubVibe recurring audit for n8n

This importable workflow buys one HubVibe audit bundle for a public site on a
daily schedule. The bundle runs WCAG, SEO, security-header, and performance
checks for **$0.15 per successful delivery** through HubVibe's existing prepaid
API-key rail. A request that does not deliver a result is not billed under the
service's existing payment contract.

The workflow is **inactive when imported**. Nothing is purchased until you test
it and publish it.

## Install

1. Download
   [`hubvibe-recurring-audit.json`](hubvibe-recurring-audit.json).
2. In n8n, choose **Import from File** and select the downloaded JSON.
3. Create an n8n **Header Auth** credential:
   - Name: any name that does not contain the key.
   - Header name: `X-API-Key`.
   - Header value: your HubVibe prepaid API key from
     <https://hubvibe-io.com/start>.
4. Open **Buy HubVibe audit bundle** and select that Header Auth credential.
   The published JSON contains no credential ID, credential name, API key,
   wallet, or customer data.
5. Open **Set site URL** and replace `https://example.com` with the public URL
   you are authorized to audit.
6. Select **Test workflow manually** and run the workflow once. Confirm that
   **Require delivered result** finishes successfully and inspect the returned
   findings.
7. Adjust the schedule if needed. It defaults to 09:00 UTC every day.
8. Publish the workflow only after the manual test succeeds. n8n does not run a
   Schedule Trigger until its workflow is published.

## What each execution does

```text
manual test or daily schedule
  -> set one public URL
  -> POST /audit/bundle with the selected Header Auth credential
  -> require status="ok" and a boolean pass result
```

The HTTP node sends only this customer-supplied input:

```json
{"url": "https://example.com"}
```

It also sends a distinct `Idempotency-Key` for the n8n execution. The HTTP
node's one transient retry reuses that key, so a retry within the execution
returns the original delivery instead of buying the audit twice. If n8n reports
an unknown outcome after the HTTP request, inspect that execution before
starting an entirely new one; a new execution has a new purchase identity.

The final Code node rejects an HTTP response unless it contains the delivered
audit shape (`status: "ok"` and a boolean `pass`). This prevents a malformed or
partial response from appearing as a successful workflow execution.

## Example: Daily production storefront check

A small ecommerce team can set **Set site URL** to its public storefront and
run the bundle every morning before staff begin work. One execution checks the
rendered page for accessibility violations, missing SEO metadata, unsafe or
missing security headers, and abnormal page weight or request count. If HubVibe
does not deliver the documented result shape, the workflow fails at **Require
delivered result** instead of reporting a successful check.

The team first runs the manual trigger and reviews the findings. It then
publishes the daily schedule. A Slack, email, or ticketing node can be attached
after the validation node to route a failed audit to the site owner. The
default workflow makes at most one $0.15 purchase per execution. Thirty
successful daily runs cost $4.50 before any n8n operating costs; the prepaid
balance and chosen schedule define the buyer's total spending limit.

## Cost examples

These are price calculations, not revenue or profit forecasts:

| Schedule | Maximum successful bundles | Price per bundle | Spend |
|---|---:|---:|---:|
| Daily for 30 days | 30 | $0.15 | $4.50 |
| Weekdays for four weeks | 20 | $0.15 | $3.00 |
| Weekly for 13 weeks | 13 | $0.15 | $1.95 |

Only publish a cadence whose maximum spend you authorize. This workflow uses a
prepaid key so its maximum total spend is also bounded by the credit placed on
that key.

## Adoption measurement

Every call carries these non-secret identifiers:

- `User-Agent: hubvibe-n8n-recurring-audit/1.0`
- `X-HubVibe-Integration: n8n-recurring-audit/1.0`

HubVibe can aggregate delivered calls, distinct payers, repeat purchases,
received amount, and unbilled failures for this integration from its existing
request and purchase records. The workflow adds no analytics service, tracking
pixel, customer identifier, or callback.

## Safety and limits

- Store the API key in n8n credentials, never in a node field or exported JSON.
- The template does not include x402 wallet material and does not alter any
  HubVibe payment rail or price.
- One execution audits one URL. Duplicate targets are not expanded into
  multiple purchases.
- The audit is automated evidence, not a compliance certification, manual
  accessibility audit, penetration test, or Lighthouse run.
- Do not add n8n's whole-workflow automatic retry after an unknown settlement
  outcome. The built-in HTTP-node retry is already bounded and idempotent.

The workflow uses only built-in n8n nodes. No community node or new service is
required.

## n8n template-library submission

The JSON includes the on-canvas documentation currently required by n8n's
Creator Hub: one yellow 100–300 word overview note and neutral section notes
for a workflow with four or more executable nodes. Submission is an external
publication action and is not performed by this repository.

Before submitting:

1. Register or sign in at <https://creators.n8n.io> and read the current
   Creator Hub guidelines.
2. Import this JSON into a fresh, current n8n instance.
3. Create and select a Header Auth credential, set an authorized public URL,
   and complete one end-to-end manual delivery test. This is a paid $0.15 call
   and requires separate spending approval.
4. Confirm the schedule is still unpublished and the workflow remains
   inactive after the test.
5. Export the tested workflow again. Remove the selected credential reference,
   execution data, account names, personal data, and resource identifiers.
6. Re-import that sanitized JSON into another fresh workflow and repeat the
   setup instructions without executing another paid call.
7. In the Creator Dashboard, submit the sanitized JSON for review. n8n reviews
   it and either publishes it or returns changes by email.

Do not submit the exported file if it contains a credential name or ID. The
repository version deliberately contains no `credentials` object.
