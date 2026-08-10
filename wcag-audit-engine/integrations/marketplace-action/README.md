# HubVibe Site Compliance Audit (GitHub Action)

Runs a real, deterministic site audit (WCAG accessibility, SEO, security
headers, performance, or all four as a bundle) against a live URL and fails
the build if it doesn't pass. Every check runs against the actual page --
axe-core for accessibility, a real HTTP response for security headers, a
real Playwright page load for performance -- never an LLM guessing at
quality.

This directory is meant to become the **root** of its own dedicated public
repo before publishing to the GitHub Actions Marketplace: Marketplace
requires `action.yml` at repo root and requires the repo contain no other
workflow files, which rules out publishing directly from the HubVibe
monorepo (it has its own CI workflow). Copy this directory's contents to
the root of a new repo (e.g. `hubvibe-audit-action`), tag a release (e.g.
`v1.0.0`), and check "Publish this release to the GitHub Marketplace" when
creating the release.

## Usage

```yaml
- name: HubVibe compliance audit
  uses: its-fortunatefolly/hubvibe-audit-action@v1
  with:
    url: https://your-site.example.com
    api-key: ${{ secrets.HUBVIBE_API_KEY }}
    endpoint: bundle   # or: wcag, seo, security, performance
```

## Inputs

| Input               | Required | Default  | Description                                      |
|---------------------|----------|----------|---------------------------------------------------|
| `url`                | yes      | --       | The live URL to audit.                            |
| `api-key`            | yes      | --       | Your HubVibe API key (`X-API-Key`).                |
| `endpoint`           | no       | `bundle` | `wcag`, `seo`, `security`, `performance`, or `bundle`. |
| `fail-on-violation`  | no       | `true`   | Set `false` to report without failing the build.  |

## Outputs

| Output    | Description                                  |
|-----------|-----------------------------------------------|
| `passed`   | `"true"` or `"false"` -- the audit's overall result. |
| `response` | Raw JSON response body from the audit call.  |

Get an API key at https://hubvibe-831480473793.us-south1.run.app via
`/billing/checkout` ($49/mo, 1,500 scans included), or pay per-call with
x402/MPP directly against the REST endpoints if you'd rather not hold a
subscription key in CI.
