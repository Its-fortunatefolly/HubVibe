"""identity.check -- one call to vet a person or company before onboarding.

Runs, in parallel, whichever checks the caller has input for:
  name  -> sanctions.screen (OFAC SDN + consolidated, UK, EU lists)
  email -> email.verify (DNS, SMTP mailbox probe, disposable and role checks)
  phone -> phone.parse (validity, country, line type, original carrier)
  ip    -> ip.lookup (country, network owner, public or not)
and returns each check's result, a list of flags, and one risk level. A
check that fails is named in `checks_failed` and the rest still ship; the
call is unbilled only when every check it was asked for failed.

A flag is a lead to review, not a decision about the person.
"""

import asyncio
from datetime import datetime, timezone

from .. import runtime
from . import email as email_skill
from . import osint as osint_skill
from . import sanctions as sanctions_skill

SEVERITY_RANK = {"high": 2, "medium": 1, "low": 0}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    req = {k: payload.get(k) for k in ("name", "email", "phone", "ip", "country", "birth_year", "phone_region")}
    if not any(req[k] for k in ("name", "email", "phone", "ip")):
        raise runtime.InvalidRequest("Give at least one of `name`, `email`, `phone`, `ip`.")
    if req["name"]:
        sanctions_skill.parse({"name": req["name"], "birth_year": req["birth_year"]})
    elif req["birth_year"] is not None:
        raise runtime.InvalidRequest("`birth_year` refines the sanctions screen; it needs `name`.")
    if req["email"]:
        email_skill.parse({"email": req["email"]})
    if req["phone"]:
        osint_skill.parse_phone({"number": req["phone"], "region": req["phone_region"]})
    elif req["phone_region"] is not None:
        raise runtime.InvalidRequest("`phone_region` qualifies `phone`; it needs `phone`.")
    if req["ip"]:
        osint_skill.parse_ip({"ip": req["ip"]})
    country = req["country"]
    if country is not None and (not isinstance(country, str) or not 2 <= len(country.strip()) <= 60):
        raise runtime.InvalidRequest("`country`, when given, is a two-letter code (US) or a country name.")
    req["country"] = country.strip() if country else None
    return req


def precheck(payload: dict) -> None:
    parse(payload)


def _flag(flags: list, code: str, severity: str, detail: str) -> None:
    flags.append({"code": code, "severity": severity, "detail": detail})


def _same_country(a, b) -> bool:
    return bool(a and b) and a.strip().casefold() == b.strip().casefold()


def country_flags(req: dict, phone, ip) -> list:
    """Low-severity flags where the stated, phone and IP countries disagree."""
    seen = {}
    if req["country"]:
        seen["stated"] = req["country"]
    if phone and phone.get("valid") and phone.get("region"):
        seen["phone"] = (phone["region"], phone.get("country"))
    if ip and (ip.get("location") or {}).get("country_code"):
        seen["ip"] = (ip["location"]["country_code"], ip["location"].get("country"))
    flags = []

    def matches(stated, pair):
        return any(_same_country(stated, v) for v in pair if v)
    if "stated" in seen:
        for source in ("phone", "ip"):
            if source in seen and not matches(seen["stated"], seen[source]):
                _flag(flags, f"country_mismatch_{source}", "low",
                      f"Stated country {seen['stated']} but the {source} is in {seen[source][1] or seen[source][0]}.")
    if "phone" in seen and "ip" in seen and seen["phone"][0] != seen["ip"][0]:
        _flag(flags, "country_mismatch_phone_ip", "low",
              f"Phone is in {seen['phone'][1] or seen['phone'][0]}, IP is in {seen['ip'][1] or seen['ip'][0]}.")
    return flags


def summarize(req: dict, results: dict) -> tuple:
    flags = []
    s = results.get("sanctions")
    if s:
        if s["verdict"] == "potential_match":
            top = s["matches"][0]
            _flag(flags, "sanctions_potential_match", "high",
                  f"{s['match_count']} listing(s) match, best: {top['name']} on {top['list_name']} (score {top['score']}).")
        elif s["verdict"] == "incomplete":
            _flag(flags, "sanctions_incomplete", "medium", "No match, but at least one list could not be screened.")
    e = results.get("email")
    if e:
        if e["verdict"] == "undeliverable":
            _flag(flags, "email_undeliverable", "medium", f"Email cannot receive mail ({e['reason']}).")
        elif e.get("disposable"):
            _flag(flags, "email_disposable", "medium", "Email is on a disposable-address domain.")
        elif e["verdict"] == "risky":
            _flag(flags, "email_unconfirmed", "low", f"Mailbox could not be confirmed ({e['reason']}).")
        elif e["verdict"] == "unknown":
            _flag(flags, "email_unconfirmed", "low", f"Mail server did not confirm the mailbox ({e['reason']}).")
        if e.get("did_you_mean"):
            _flag(flags, "email_possible_typo", "low", f"Did they mean {e['did_you_mean']}?")
    p = results.get("phone")
    if p:
        if not p["valid"]:
            _flag(flags, "phone_invalid", "medium", "Not a valid number in its country's numbering plan.")
        elif p["line_type"] == "voip":
            _flag(flags, "phone_voip", "low", "VoIP number: easy to obtain without identity checks.")
        elif p["line_type"] in ("premium_rate", "shared_cost"):
            _flag(flags, "phone_premium", "medium", f"{p['line_type'].replace('_', '-')} number.")
    i = results.get("ip")
    if i and i["scope"] != "public":
        _flag(flags, "ip_not_public", "medium", f"A {i['scope'].replace('_', '-')} address, not a real client address.")
    flags += country_flags(req, p, i)
    top = max((SEVERITY_RANK[f["severity"]] for f in flags), default=-1)
    risk = "high" if top == 2 else "review" if top == 1 else "low"
    return flags, risk


def _brief(name: str, value: dict) -> dict:
    if name == "sanctions":
        return {k: value[k] for k in ("verdict", "match_count", "matches", "lists")}
    if name == "email":
        keep = ("normalized", "verdict", "reason", "disposable", "role_account", "free_provider", "did_you_mean",
                "accepts_mail", "mail_provider")
        return {k: value.get(k) for k in keep}
    if name == "phone":
        return {k: value.get(k) for k in ("valid", "e164", "country", "region", "location", "carrier", "line_type")}
    return {"scope": value["scope"], "location": value["location"], "network": value["network"],
            "reverse_dns": value["reverse_dns"], "attribution": value["attribution"]}


async def check(ctx, payload: dict) -> dict:
    req = parse(payload)
    jobs = {}
    if req["name"]:
        body = {"name": req["name"], "limit": 5}
        if req["birth_year"] is not None:
            body["birth_year"] = req["birth_year"]
        jobs["sanctions"] = sanctions_skill.screen(ctx, body)
    if req["email"]:
        jobs["email"] = email_skill.verify(ctx, {"email": req["email"]})
    if req["phone"]:
        jobs["phone"] = osint_skill.phone_parse(ctx, {"number": req["phone"], "region": req["phone_region"]})
    if req["ip"]:
        jobs["ip"] = osint_skill.ip_lookup(ctx, {"ip": req["ip"]})
    outcomes = await asyncio.gather(*jobs.values(), return_exceptions=True)
    results, failed, notes = {}, [], []
    for name, out in zip(jobs, outcomes):
        if isinstance(out, runtime.WorkerError):
            failed.append(name)
            notes.append(f"{name}: {getattr(out, 'detail', out)}"[:200])
        elif isinstance(out, BaseException):
            raise out
        else:
            results[name] = out
    if not results:
        raise runtime.TransientProviderError("None of the requested checks could be completed: " + "; ".join(notes))
    flags, risk = summarize(req, results)
    if failed:
        notes.append("Checks named in checks_failed did not run to completion; their flags are missing.")
    notes.append("Flags are leads to review, not a determination about the person or company.")
    return {
        "input": {k: req[k] for k in ("name", "email", "phone", "ip", "country", "birth_year")},
        "risk": risk,
        "flags": sorted(flags, key=lambda f: -SEVERITY_RANK[f["severity"]]),
        "checks": {name: (_brief(name, results[name]) if name in results else None)
                   for name in ("sanctions", "email", "phone", "ip")},
        "checks_run": list(results),
        "checks_failed": failed,
        "notes": notes,
        "checked_at": _now(),
    }


SKILLS = {"identity.check": check}
PRECHECKS = {"identity.check": precheck}
