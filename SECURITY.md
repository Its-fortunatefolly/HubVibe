# Security Policy

HubVibe accepts responsible reports about its public integrations and the
production service at `https://hubvibe-io.com`.

## Supported versions

The current production release and the latest version on each published
integration's default branch are supported. Older snapshots and copied listings
may no longer describe the live service.

## Report a vulnerability privately

Use GitHub's **Security → Report a vulnerability** form for this repository, or
email `hubvibe@hubvibe-io.com`. Do not open a public issue for a suspected
vulnerability.

Include the affected endpoint or integration, the observed and expected
behavior, reproducible steps, and the security impact. Remove API keys, wallet
keys, payment credentials, personal data, and customer data from the report.

## Safe testing

- Use accounts, wallets, domains, and data you own or are authorized to test.
- Do not move funds, access another customer's records, degrade availability,
  or perform high-volume testing.
- Stop and report privately if testing could expose secrets or customer data.

We will acknowledge a usable report, investigate it, and coordinate disclosure
after a fix is available. This policy does not create a bug-bounty program or
authorize access beyond normal published interfaces.

## Repository boundary

This public repository contains client integrations, schemas, examples, and
marketplace metadata. HubVibe's private production implementation, credentials,
payment records, customer data, and deployment configuration are not published
here.
