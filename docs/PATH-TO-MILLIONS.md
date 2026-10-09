# HubVibe: the path to a multimillion-dollar run rate in 12 months

Written 2026-10-08. Every figure below has a source or was measured from the
live node or from Base. Re-check the dated items (deadlines, lawsuits) monthly.

**Target:** $2.1M ARR run rate ($175k MRR) by October 2027, mostly recurring
and mostly prepaid annually.

---

## 1. Where HubVibe actually stands

| Fact | Evidence |
|---|---|
| Engineering is done: 70 paid jobs, all rails live (x402 Base/Solana, MPP evm/Stripe/Tempo, prepaid card key), blue/green deploys, verifiable receipts | `/health`, `scripts/payment-status.sh`, 2026-10-08 |
| Outside revenue is at most $50.77 in 45 days (about $34/month); $11.49 USDC is in the wallets now | Base logs, read 2026-10-08 (see §1a) |
| The one recurring product was switched off on 2026-09-06 ("why would anyone pay that when the scans are 5 cents") | `wcag-audit-engine/app/billing.py:148` |
| Outbound tooling already exists: `prospect_scan.py` and `draft_outreach.py` | `scripts/` |

### 1a. Inflow breakdown (who actually paid)

Every USDC transfer into the pay-to wallet over the 45 days to 2026-10-08,
read from Base logs and grouped by sender:

| Sender | Transfers | USDC | What it is |
|---|---|---|---|
| `0x104f…bdd35` | 250 | $266.77 | **Our own payer wallet** (listing refreshes, self-tests; `PURCHASE_INTERNAL_PAYERS`) |
| `0x0780…560f` | 1 | $80.00 | One-off funding transfer, not a sale |
| `0x1231…f4eae` | 1 | $2.97 | LI.FI bridge, our own funds moving |
| `0x3755…38c0` | 3 | $2.50 | Owner's alternate wallet |
| `0x4b5c…34f6` | 11 | $27.06 | Unidentified: possible buyer |
| `0x72c5…159b` | 115 | $17.25 | Unidentified: $0.15 calls, a bundle/CI-style buyer, active today |
| 10 other senders | 58 | $6.46 | Unidentified, mostly cents |
| **Total in** | **439** | **$403.01** | |

**Revenue from senders not identified as ours: at most $50.77 in 45 days**,
about $34 a month. That is an upper bound: some of it may still be our own
wallets. The wallet holds $10.56 because the rest was moved out.

### 1b. Why agent payments can't reach millions in 12 months

- Coinbase reports about 165M x402 transactions and about $50M in total volume.
  TRM Labs screened the settlements and found only **0.6%–7.5% plausibly
  agentic**, which is about **$5k–$11k a month across the whole ecosystem**.
  ([TRM via spotedcrypto](https://www.spotedcrypto.com/trm-labs-x402-ai-agent-payments-report-2026/),
  [Chainalysis](https://www.chainalysis.com/blog/x402-agentic-payments-adoption/))
- Stripe/Tempo MPP launched 2026-03-18 with Visa, OpenAI, Anthropic and Shopify
  integrations, but **no published merchant volume** yet.
  ([Fortune](https://fortune.com/2026/03/18/stripe-tempo-paradigm-mpp-ai-payments-protocol/))

Even 100% of that market is not millions. **Keep every machine rail live.**
They cost almost nothing to run, they are a moat, and they put HubVibe ahead
when agent spending does take off. But the next 12 months of revenue has to
come from **organizations that already have budget, a deadline and legal
exposure**, paying on a card, by invoice or by ACH, every month or every year.

---

## 2. The window: two federal deadlines inside our 12 months

| Date | Who must meet WCAG 2.1 AA | Source |
|---|---|---|
| **2027-04-26** | State/local governments serving 50k+ people (ADA Title II) | [Federal Register IFR 2026-04-20](https://www.federalregister.gov/documents/2026/04/20/2026-07663/extension-of-compliance-dates-for-nondiscrimination-on-the-basis-of-disability-accessibility-of-web) |
| **2027-05-11** | Every HHS-funded provider with 15+ employees (Section 504): hospitals, clinics, FQHCs, nursing homes, anyone taking Medicare/Medicaid | [Deque](https://www.deque.com/blog/hhs-section-504-deadline-extended-what-did-and-didnt-change-and-what-your-organization-needs-to-do/) |
| 2028-04-26 / 2028-05-10 | Smaller governments and special districts / smaller providers | same |
| Ongoing | Private businesses (ADA Title III): **5,006 federal filings in H1 2026, a record**; about 10 website suits a day | [Seyfarth via search](https://blog.usablenet.com/inside-the-2026-midyear-numbers-where-digital-accessibility-litigation-is-going), [EcomBack](https://www.ecomback.com/ada-website-lawsuits-recap-report/april-2026) |
| Live since 2025-06-28 | EU businesses selling online (European Accessibility Act) | [Level Access](https://www.levelaccess.com/blog/eaa-compliance-in-2026-how-enforcement-has-evolved-and-what-to-expect-next/) |

**Market size, verified:**

- 6,100 US hospitals ([AHA 2026](https://www.aha.org/system/files/media/file/2026/02/Fast-Facts-on-US-Hospitals-2026-Infographics.pdf)).
- **16,832 .gov domains** in CISA's public registry: 9,163 city, 2,674 county,
  1,449 state, 1,122 special district (measured 2026-10-08,
  [`cisagov/dotgov-data`](https://github.com/cisagov/dotgov-data)).
- DOJ's own estimate of compliance cost is $9.6k a year for a small county
  and $18.3k for a municipality. That is the budget that exists for this.

**Risks, and why they don't break the plan:**

- NFB sued DOJ and HHS on 2026-05-21 to undo the extensions (D. Md., no ruling
  yet). If NFB wins, the deadlines come *sooner*, which means more urgency.
- DOJ listed an NPRM to "reconsider" parts of the Title II rule, with a "TBD"
  timetable, and SBA Advocacy wants it withdrawn for small governments. That
  would hit only one segment. Healthcare (HHS), private businesses (Title III
  lawsuits) and the EU are unaffected. **Lead with healthcare and private
  business. Treat government as the upside.**

---

## 3. The original angle: sell evidence, not scans

The 5-cent anchor killed the subscription because the plan sold *scans*.
Buyers in this market are not buying scans. They are buying **defensible
proof** for when a demand letter, an OCR investigation or a board question
arrives.

What is crowded or discredited (stay out):

- **Overlays.** The FTC fined accessiBe $1M (final order April 2025) over its
  claims that a widget makes a site compliant.
- **$29–$199/month white-label scanners for agencies.** This is a race to the
  bottom (AllAccessible, WCAGRepair, PageAudit, Remediate.co).

What nobody else sells, and HubVibe already has in code:

- **Accessibility Evidence Ledger.** Every scan already writes a receipt with
  sha256 hashes of the request and the result, a timestamp, the rule set and
  the node version (`/work/receipts/{id}`). Add a daily Merkle root of all of a
  customer's receipts, anchored on Base for fractions of a cent. The result is
  a **tamper-evident, third-party-timestamped record of continuous monitoring
  and of every fix's date**. A competitor's dashboard is a claim. HubVibe's
  record can be verified by anyone, including opposing counsel, without
  trusting HubVibe.
- **Deterministic and honest.** A check that couldn't run is reported as an
  error, never a pass. That is the exact opposite of what got accessiBe fined.
  Never sell "compliance". Sell "evidence of continuous good-faith conformance
  work", and name automated coverage limits plainly.

---

## 4. The offer (recurring, priced on the outcome)

On human-facing pages, never show per-scan prices. Machine prices stay on the
API, the 402 and the manifests, unchanged.

| Plan | For | Price | What it buys |
|---|---|---|---|
| **Evidence Monitor** | SMBs and e-commerce (Title III, EAA) | **$199/mo** or $1,990/yr | 3 sites, daily WCAG + security scans, Evidence Ledger, monthly PDF |
| **Deadline Program** | Hospitals, clinics, cities, counties | **$799/mo** or **$7,990/yr** | 25 sites/subdomains, ledger, board/council-ready monthly report, deadline-readiness score, remediation tracking |
| **Agency Partner** | Web agencies serving the two groups above | **$499/mo** | 50 client sites, white-label evidence reports. They resell at $150–$250 a site |
| Machine API | Agents, CI | unchanged | x402, MPP, Solana, card credit packs |

- **$7,990/yr is set to fit under common purchasing-card and small-purchase
  limits**, so a city IT lead or hospital compliance officer can buy without
  an RFP. Check each target's threshold, since they vary by jurisdiction.
- Remediation is not done in-house. Refer it to remediation shops for a 15–20%
  revenue share. HubVibe stays software margin (the marginal cost of an audit
  is about $0.00007, per `billing.py`).
- Rails: Stripe subscriptions (card/ACH) and invoice with net-30 for
  governments. Keep MPP (Stripe/Tempo), x402 and Solana top-ups for
  machine-native buyers. No one rail is the business.

### The math to $175k MRR by month 12

| Segment | Customers | Price | MRR |
|---|---|---|---|
| Deadline Program (health + gov) | 120 | $799 | $95.9k |
| Agency Partner | 100 | $499 | 49,900 USD |
| Evidence Monitor | 150 | $199 | $29.9k |
| **Total** | **370** | | **$175.7k ≈ $2.1M ARR** |

- At a conservative 2% close rate on personalized, evidence-led outbound,
  370 customers takes about 18,500 prospects. That is about **75 a business
  day**, which `prospect_scan.py` + `draft_outreach.py` can do with no new
  hires.
- **Push annual prepay** before both deadlines. Government and hospital
  budgets prefer annual, and it pulls cash forward: 120 Deadline annuals alone
  is about $959k collected.
- Be honest about the gap: a $2.1M *run rate* at month 12 is not $2.1M
  *collected* in 12 months. Prepay closes most of that gap.

---

## 5. Distribution: three moves, in this order

1. **Publish a public readiness index (the original growth move).** Scan all
   12,000+ city/county/state .gov domains and every hospital site, then
   publish the **"2027 Accessibility Readiness Index"**: a public scorecard
   page per entity, ranked by state.
   - Each page is a free, true, specific bug report about *their* site, so it
     is the cold email.
   - It is data reporters at GovTech, Route Fifty, Becker's and Fierce
     Healthcare write about before an April/May deadline.
   - It makes HubVibe the source AI answer engines cite for "is [city]'s
     website ADA compliant".
   - Re-scan monthly, so the movement on each entity's score is the news.
2. **Evidence-led outbound, 75 a day.** Use the CISA list for governments, the
   CMS hospital list plus Clay/Apollo/Vibe Prospecting enrichment for
   compliance officers and CIOs, and industries named in recent Title III
   filings for SMBs. Each email opens with the recipient's own failing rules
   and links to their index page. `draft_outreach.py` already refuses any
   claim it can't trace to a finding.
3. **Referral partners who already hold the buyers:**
   - ADA *defense* law firms, whose clients need ongoing monitoring evidence
     after a settlement. Settlements commonly require it.
   - Hospital and municipal web-platform vendors.
   - A government reseller already on cooperative contracts (Carahsoft, SHI or
     CDW-G, via Sourcewell, OMNIA or TIPS). That lets governments buy without
     an RFP. Start now: onboarding takes months, and the buying spike is
     Feb–Apr 2027.

4. **Google Cloud Marketplace (procurement shortcut, weeks 4–8).** Buyers
   with a Google Cloud spending commitment (large health systems, states,
   enterprises) can pay for HubVibe from that commitment, with no new-vendor
   onboarding. A marketplace listing doesn't bring buyers; it removes a
   procurement step for the buyers HubVibe already found.
   - The fee is reported at 3% of each sale (third-party figure,
     [Clazar](https://clazar.io/blog/google-cloud-marketplace-fees); confirm
     in Producer Portal).
   - The live A2A agent card already has the fields Google requires (base
     URL, provider, A2A 1.0).
   - Still to build: OAuth 2.0 or public access; Procurement API + Pub/Sub
     entitlement handling, with a marketplace entitlement issuing a prepaid
     `X-API-Key`; Partner Network membership and the Marketplace Vendor
     Agreement.
   - List the three plans, not 72 skills: enterprises buy one clear thing.
   - Google's rule is that A2A agent listings can't bill professional
     services ([docs](https://docs.cloud.google.com/marketplace/docs/partners/ai-agents)).
   - Professional services must go through US-only private offers that link
     to a product listing.

**Kill list (money pits):**

- Adding more `/work` workers. There are 70 already and no demand signal.
  Freeze it.
- Marketing aimed at the x402 ecosystem.
- Paid consumer social ads.
- Overlays.
- Cheap white-label price wars.
- Any "100% compliant" claim.
- Hand-delivered cloud security/IAM consulting at $5k–$25k per review. It's
  a human consulting business that needs credentials, insurance and access
  to clients' cloud accounts, and it's not what the node does.

---

## 6. 90-day execution

| Weeks | Ship |
|---|---|
| 1–2 | Re-enable `HUMAN_PLANS` with the three plans above. Stripe price IDs already have plumbing in `billing.py`. Add invoice/ACH. Add segment pages `/healthcare`, `/government`, `/agencies`. |
| 2–3 | Evidence Ledger: per-customer receipt rollup, daily Merkle root anchored on Base, monthly PDF report. |
| 3–5 | Readiness Index v1: all .gov city/county/state domains plus hospitals. A public page per entity. Press kit. |
| 3–12 | Outbound at 75/day. Weekly 30-minute "Deadline Readiness" webinar. Sign the first 5 agency partners and 2 law-firm referral partners. |
| 6–12 | Start reseller/co-op contract onboarding. Case study from the first 10 paying orgs. |

**90-day gates:** 30 paying orgs and $15k MRR. If either is missed, change
the offer, not the market.

**Weekly scoreboard:** prospects scanned, reply rate, demos, new orgs, MRR,
annual-prepay %, logo churn.
