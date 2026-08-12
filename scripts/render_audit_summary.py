"""Render an audit response into a GitHub Actions job summary (Markdown).

Invoked by the root action.yml. Writes to stdout; the action appends that to
$GITHUB_STEP_SUMMARY.

This exists because the job summary is the only surface a reviewer actually
reads. An audit whose findings are buried in raw curl output gets switched off
within a sprint, no matter how correct it is.

Hard rule, inherited from the service itself: never report a check that did not
run as a pass. If the body cannot be parsed, or a dimension is missing, say so
explicitly rather than rendering an empty table that reads as clean.
"""

import argparse
import json
import sys

# Keep the summary bounded. A page with hundreds of axe violations would
# otherwise produce a summary GitHub truncates mid-table, which is worse than
# an explicit "and N more".
_MAX_ROWS_PER_SECTION = 15


def _load(path):
    """Return (body, error_message). Never raises."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return None, f"could not read the response file ({exc})"
    if not text.strip():
        return None, "the service returned an empty body"
    try:
        return json.loads(text), None
    except ValueError:
        preview = text.strip()[:400]
        return None, f"the response was not JSON:\n\n```\n{preview}\n```"


def _escape(value):
    """Make a value safe to drop in a Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _verdict(passed):
    if passed is True:
        return "✅ pass"
    if passed is False:
        return "❌ fail"
    return "⚠️ did not run"


def _violation_rows(items):
    """axe-core violations -> table rows."""
    rows = []
    for item in items[:_MAX_ROWS_PER_SECTION]:
        if not isinstance(item, dict):
            continue
        help_text = item.get("help") or item.get("description") or ""
        help_url = item.get("help_url") or item.get("helpUrl")
        if help_url:
            help_text = f"[{_escape(help_text)}]({help_url})"
        else:
            help_text = _escape(help_text)
        rows.append(
            "| `{id}` | {impact} | {nodes} | {help} |".format(
                id=_escape(item.get("id", "?")),
                impact=_escape(item.get("impact") or "-"),
                nodes=_escape(item.get("nodes_affected", "-")),
                help=help_text,
            )
        )
    return rows


def _finding_rows(items):
    """SEO/security/performance findings -> table rows."""
    rows = []
    for item in items[:_MAX_ROWS_PER_SECTION]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "| `{id}` | {severity} | {detail} |".format(
                id=_escape(item.get("id", "?")),
                severity=_escape(item.get("severity") or "-"),
                detail=_escape(item.get("detail") or item.get("help") or ""),
            )
        )
    return rows


def _render_section(name, section, out):
    if not isinstance(section, dict):
        return
    out.append(f"### {name} — {_verdict(section.get('pass'))}")
    out.append("")

    metrics = section.get("metrics")
    if isinstance(metrics, dict) and metrics:
        out.append(" · ".join(f"**{_escape(k)}**: {_escape(v)}" for k, v in metrics.items()))
        out.append("")

    violations = section.get("violations")
    findings = section.get("findings")

    if isinstance(violations, list) and violations:
        out.append("| Rule | Impact | Nodes | Help |")
        out.append("| --- | --- | --- | --- |")
        out.extend(_violation_rows(violations))
        if len(violations) > _MAX_ROWS_PER_SECTION:
            out.append(f"\n_…and {len(violations) - _MAX_ROWS_PER_SECTION} more._")
        out.append("")
    elif isinstance(findings, list) and findings:
        out.append("| Check | Severity | Detail |")
        out.append("| --- | --- | --- |")
        out.extend(_finding_rows(findings))
        if len(findings) > _MAX_ROWS_PER_SECTION:
            out.append(f"\n_…and {len(findings) - _MAX_ROWS_PER_SECTION} more._")
        out.append("")
    elif section.get("pass") is True:
        out.append("No issues found.")
        out.append("")


def render(body, status, endpoint, url):
    out = ["## HubVibe Site Compliance Audit", ""]
    out.append(f"**Target:** `{_escape(url)}`  ")
    out.append(f"**Audit:** `{_escape(endpoint)}`  ")
    out.append(f"**HTTP:** `{_escape(status)}`")
    out.append("")

    if body is None:
        # `status` is passed through as a string by the shell caller.
        if str(status) == "402":
            out.append(
                "⚠️ **Payment required.** This run was not billed and no audit was "
                "performed. Set the `api-key` input, or pay per call with x402/MPP "
                "— the challenge details are in the raw response in the job log."
            )
        elif str(status) == "000":
            out.append("⚠️ **The request never completed.** No audit was performed and nothing was billed.")
        else:
            out.append(f"⚠️ **No audit result.** HTTP `{_escape(status)}`.")
        return "\n".join(out)

    if not isinstance(body, dict):
        out.append("⚠️ **Unexpected response shape** — no audit result to report.")
        return "\n".join(out)

    overall = body.get("pass")
    out.append(f"**Result:** {_verdict(overall)}")
    out.append("")

    sections = [
        ("Accessibility (WCAG 2.1 A/AA)", body.get("wcag")),
        ("SEO", body.get("seo")),
        ("Security headers", body.get("security")),
        ("Performance", body.get("performance")),
    ]
    if not any(isinstance(section, dict) for _, section in sections):
        # A single-dimension call returns its results at the top level.
        _render_section(endpoint.upper() if endpoint else "Audit", body, out)
    else:
        for name, section in sections:
            _render_section(name, section, out)

    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--endpoint", default="bundle")
    parser.add_argument("--url", default="")
    args = parser.parse_args(argv)

    body, error = _load(args.response)
    text = render(body, args.status, args.endpoint, args.url)
    if error and str(args.status) == "200":
        text += f"\n\n⚠️ The service returned HTTP 200 but {error}"
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
