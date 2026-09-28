"""email.verify -- will mail to this address arrive? Checked now, at the source.

Syntax, then the domain's own DNS (does it take mail at all, and where),
then the domain's own mail server (does this mailbox exist, or does the
server accept every address), plus the flags an outreach agent filters on:
disposable domain, role account (info@, sales@...), free provider, and a
likely typo of a common provider. No message is ever sent.

The verdict is honest about what could not be proved: a server that
accepts every address, refuses to talk to us, or answers "try later" makes
the mailbox `unknown`, never `deliverable`.
"""

import re
import secrets
from datetime import datetime, timezone

from .. import runtime
from ..providers import mailcheck

MAX_LENGTH = 320
# Deliberately simple and strict: what real mailboxes use, RFC 5321 lengths.
_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

ROLE_ACCOUNTS = frozenset({
    "abuse", "admin", "administrator", "billing", "careers", "contact", "customerservice", "enquiries",
    "feedback", "finance", "hello", "help", "hostmaster", "hr", "info", "inquiries", "jobs", "legal",
    "mail", "marketing", "media", "no-reply", "noreply", "office", "orders", "postmaster", "press",
    "privacy", "recruiting", "sales", "security", "service", "support", "team", "webmaster",
})
FREE_PROVIDERS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.jp", "yahoo.fr", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "gmx.net", "web.de", "mail.com",
    "yandex.ru", "yandex.com", "mail.ru", "qq.com", "163.com", "126.com", "naver.com", "daum.net",
    "hanmail.net", "orange.fr", "free.fr", "libero.it", "comcast.net", "att.net", "verizon.net", "zoho.com",
})
_TYPO_TARGETS = sorted(FREE_PROVIDERS)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): gmial -> gmail is 1."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def suggest(local: str, domain: str):
    """A likely intended address when the domain is one edit or two from a
    common provider and is not itself one."""
    if domain in FREE_PROVIDERS:
        return None
    best = min(_TYPO_TARGETS, key=lambda t: (_distance(domain, t), t))
    limit = 1 if len(domain) <= 8 else 2
    return f"{local}@{best}" if _distance(domain, best) <= limit else None


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    raw = payload.get("email")
    if not isinstance(raw, str) or "@" not in raw or not raw.strip():
        raise runtime.InvalidRequest("`email` is required: one address, such as jane@example.com.")
    email = raw.strip()
    if len(email) > MAX_LENGTH:
        raise runtime.InvalidRequest(f"`email` is longer than {MAX_LENGTH} characters, the most an address can be.")
    local, _, domain = email.rpartition("@")
    if domain.startswith("["):
        raise runtime.InvalidRequest("Address literals (user@[192.0.2.1]) are not checked; send an address with a domain.")
    return {"email": email, "local": local, "domain": domain}


def precheck(payload: dict) -> None:
    parse(payload)


def syntax(local: str, domain: str):
    """(valid, domain_ascii, problem)."""
    if not local or len(local) > 64:
        return False, None, "The part before @ is empty or longer than 64 characters."
    if not _LOCAL.match(local):
        return False, None, "The part before @ has characters or dots a mailbox cannot have."
    try:
        domain_ascii = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return False, None, "The domain is not a valid internationalised domain name."
    labels = domain_ascii.split(".")
    if len(domain_ascii) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels) \
            or labels[-1].isdigit():
        return False, None, "The domain is not a valid domain name."
    return True, domain_ascii, None


def _mailbox_default() -> dict:
    return {"checked": False, "exists": None, "catch_all": None, "smtp_code": None,
            "smtp_message": None, "mx_host": None}


async def verify(ctx, payload: dict) -> dict:
    req = parse(payload)
    email, local, domain = req["email"], req["local"], req["domain"]
    notes = []
    valid, domain_ascii, problem = syntax(local, domain)
    domains, list_as_of, list_source = await mailcheck.disposable_domains()
    if list_source == "snapshot":
        notes.append(f"The disposable-domain list could not be refreshed; checked against the {list_as_of} snapshot.")
    result = {
        "email": email,
        "normalized": f"{local}@{domain_ascii}" if valid else None,
        "local_part": local,
        "domain": domain,
        "domain_ascii": domain_ascii,
        "verdict": "undeliverable",
        "reason": "invalid_syntax",
        "syntax_valid": valid,
        "domain_exists": None,
        "accepts_mail": None,
        "mx": [],
        "mail_provider": None,
        "mailbox": _mailbox_default(),
        "disposable": bool(valid and mailcheck.is_disposable(domain_ascii, domains)),
        "role_account": local.split("+", 1)[0].lower() in ROLE_ACCOUNTS,
        "free_provider": bool(valid and domain_ascii in FREE_PROVIDERS),
        "did_you_mean": suggest(local, domain_ascii) if valid else None,
        "disposable_list_as_of": list_as_of,
        "notes": notes,
        "checked_at": None,
    }
    if not valid:
        notes.append(problem)
        result["checked_at"] = _now()
        return result

    async def lookup(provider):
        return await provider.mail_hosts(domain_ascii)
    hosts = await ctx.run("dns", [mailcheck.DNS], lookup, per_attempt_seconds=10, max_attempts=2)
    result["domain_exists"] = hosts["domain_exists"]
    result["mx"] = hosts["mx"]
    result["mail_provider"] = mailcheck.mail_provider([m["host"] for m in hosts["mx"]])
    if not hosts["domain_exists"]:
        result.update(reason="domain_not_found", accepts_mail=False, checked_at=_now())
        return result
    if hosts["null_mx"]:
        notes.append("The domain publishes a null MX record: it declares that it accepts no email.")
        result.update(reason="domain_accepts_no_mail", accepts_mail=False, checked_at=_now())
        return result
    if not hosts["mx"]:
        result.update(reason="no_mail_server", accepts_mail=False, checked_at=_now())
        return result
    result["accepts_mail"] = True
    if hosts["implicit"]:
        notes.append("The domain has no MX record; mail falls back to its address record (RFC 5321).")

    decoy = f"hv-{secrets.token_hex(6)}@{domain_ascii}"

    async def converse(provider):
        return await provider.probe([m["host"] for m in hosts["mx"]], f"{local}@{domain_ascii}", decoy)
    reply = await ctx.run("smtp", [mailcheck.SMTP], converse, per_attempt_seconds=40, max_attempts=1)
    mailbox = result["mailbox"]
    mailbox.update(mx_host=reply.get("host"), smtp_code=reply.get("code"),
                   smtp_message=(reply.get("message") or "")[:300] or None)
    outcome = mailcheck.classify(reply.get("code"), reply.get("message") or "") if reply["stage"] == "rcpt" else "unreachable"
    if reply["stage"] in ("connect", "mail_from"):
        outcome = "blocked"
    mailbox["checked"] = outcome in ("accepted", "no_mailbox")
    if outcome == "accepted":
        catch_all = reply.get("decoy_code") in (250, 251)
        mailbox["catch_all"] = catch_all
        mailbox["exists"] = None if catch_all else True
    elif outcome == "no_mailbox":
        mailbox["exists"] = False

    if mailbox["exists"] is False:
        result.update(verdict="undeliverable", reason="mailbox_not_found")
    elif result["disposable"]:
        result.update(verdict="risky", reason="disposable_domain")
    elif mailbox["catch_all"]:
        result.update(verdict="risky", reason="accept_all_domain")
        notes.append("The server accepts mail for any address on this domain, so this mailbox cannot be confirmed.")
    elif mailbox["exists"]:
        result.update(verdict="deliverable", reason="mailbox_exists")
    else:
        reason = {"blocked": "smtp_refused", "temporary": "smtp_try_later",
                  "unreachable": "smtp_unreachable"}.get(outcome, "smtp_inconclusive")
        result.update(verdict="unknown", reason=reason)
        notes.append({"smtp_refused": "The mail server refused to answer the mailbox question.",
                      "smtp_try_later": "The mail server asked to try later (greylisting or rate limiting).",
                      "smtp_unreachable": "No mail server for the domain could be reached on port 25.",
                      "smtp_inconclusive": "The mail server's reply did not say whether the mailbox exists."}[reason])
    if result["role_account"]:
        notes.append("Role address: it usually reaches a team or a shared inbox, not one person.")
    result["checked_at"] = _now()
    return result


SKILLS = {"email.verify": verify}
PRECHECKS = {"email.verify": precheck}
