"""Open-source intelligence bees: a phone number, an IP address, a domain.

  phone.parse -- what a phone number itself says: valid or not, country and
                 area, the network it was originally issued to, line type
                 (mobile, fixed, toll-free, VoIP...), time zones and every
                 standard format. Offline; the owner is never looked up.
  ip.lookup   -- where an IP address is (city, region, country, coordinates),
                 who runs its network (ASN and organisation), its reverse DNS
                 name and whether it is public at all. DB-IP Lite data, which
                 every answer credits as its licence requires.
  domain.dns  -- a domain's public DNS (A, AAAA, CNAME, MX, NS, TXT, CAA, SOA),
                 its SPF and DMARC policy, and its HTTPS certificate (issuer,
                 names, expiry, days left), asked of the source.
"""

import ipaddress
import re
from datetime import datetime, timezone

from .. import runtime
from ..providers import dnsintel, iplookup, phone

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_REGION = re.compile(r"^[A-Za-z]{2}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _object(payload) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    return payload


# --- phone.parse -------------------------------------------------------------------

def parse_phone(payload: dict) -> dict:
    payload = _object(payload)
    number = payload.get("number")
    if not isinstance(number, str) or not re.search(r"\d", number or "") or len(number) > 40:
        raise runtime.InvalidRequest("`number` is required: one phone number, such as +44 20 7946 0958.")
    region = payload.get("region")
    if region is not None and (not isinstance(region, str) or not _REGION.match(region)):
        raise runtime.InvalidRequest("`region`, when given, is a two-letter country code such as US or GB.")
    region = region.upper() if region else None
    phone.parse_number(number.strip(), region)  # refuses text that is not a phone number, before payment
    return {"number": number.strip(), "region": region}


async def phone_parse(ctx, payload: dict) -> dict:
    req = parse_phone(payload)

    async def call(provider):
        return await provider.describe(req["number"], req["region"])
    value = await ctx.run("parse", phone.PROVIDERS, call, per_attempt_seconds=10, max_attempts=1)
    notes = []
    if not value["valid"]:
        notes.append("Not a valid number in its country's numbering plan"
                     + (" (the length is possible, the digits are not assigned)." if value["possible"] else "."))
    if value["carrier"]:
        notes.append("Carrier is the network the number range was issued to; the number may since have been ported.")
    if value["line_type"] == "fixed_line_or_mobile":
        notes.append("This country's numbering plan does not distinguish mobile from fixed-line numbers.")
    notes.append("Answered from the number alone (libphonenumber metadata): nothing is dialled and the owner is not looked up.")
    return {"input": req, **value, "notes": notes, "checked_at": _now()}


# --- ip.lookup ---------------------------------------------------------------------

def parse_ip(payload: dict) -> dict:
    payload = _object(payload)
    raw = payload.get("ip")
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest("`ip` is required: one IPv4 or IPv6 address, such as 8.8.8.8.")
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        raise runtime.InvalidRequest(f"`ip` is not an IPv4 or IPv6 address: {raw.strip()[:60]!r}.")
    return {"ip": str(address)}


async def ip_lookup(ctx, payload: dict) -> dict:
    req = parse_ip(payload)
    address = ipaddress.ip_address(req["ip"])
    scope = iplookup.scope_of(address)
    result = {"ip": req["ip"], "version": address.version, "scope": scope, "reverse_dns": [],
              "location": None, "network": None, "database_month": None,
              "attribution": iplookup.ATTRIBUTION, "notes": [], "checked_at": None}
    if scope != "public":
        result["notes"].append(f"A {scope.replace('_', '-')} address: it is not routed on the public internet, "
                               "so it has no public location or network owner.")
        result["checked_at"] = _now()
        return result

    async def call(provider):
        return await provider.locate(req["ip"])
    value = await ctx.run("locate", iplookup.PROVIDERS, call, per_attempt_seconds=300, max_attempts=2)
    result.update(location=value["location"], network=value["network"], database_month=value["database_month"])
    result["reverse_dns"] = await iplookup.reverse_dns(req["ip"])
    if result["location"] is None:
        result["notes"].append("The address is not in the location database.")
    result["notes"].append("IP location is approximate: it is where the network registers the address, "
                           "often a city or data centre, never a street address.")
    result["checked_at"] = _now()
    return result


# --- domain.dns --------------------------------------------------------------------

def parse_domain(payload: dict) -> dict:
    payload = _object(payload)
    raw = payload.get("domain")
    if not isinstance(raw, str) or not raw.strip():
        raise runtime.InvalidRequest("`domain` is required: a domain name such as example.com (a URL is accepted).")
    text = raw.strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text).split("/")[0].split("?")[0].split("#")[0]
    text = text.rsplit("@", 1)[-1].split(":")[0].rstrip(".")
    try:
        ascii_name = text.encode("idna").decode("ascii")
    except UnicodeError:
        raise runtime.InvalidRequest(f"`domain` is not a valid domain name: {raw.strip()[:80]!r}.")
    labels = ascii_name.split(".")
    if len(labels) < 2 or len(ascii_name) > 253 or not all(_LABEL.match(label) for label in labels):
        raise runtime.InvalidRequest(f"`domain` is not a valid domain name: {raw.strip()[:80]!r}.")
    try:
        ipaddress.ip_address(ascii_name)
        raise runtime.InvalidRequest("`domain` is an IP address; use ip.lookup for addresses.")
    except ValueError:
        pass
    check_tls = payload.get("tls", True)
    if not isinstance(check_tls, bool):
        raise runtime.InvalidRequest("`tls`, when given, is true or false.")
    return {"domain": text, "domain_ascii": ascii_name, "tls": check_tls}


def _spf(txt: list):
    records = [r for r in txt if r.lower().startswith("v=spf1")]
    if not records:
        return None
    record = records[0]
    final = re.search(r"\s([-~?+])all\b", " " + record)
    return {"record": record, "all": {"-": "fail", "~": "softfail", "?": "neutral", "+": "pass"}.get(final.group(1))
            if final else None, "multiple": len(records) > 1}


def _dmarc(records: list):
    if not records:
        return None
    tags = dict(t.strip().split("=", 1) for t in records[0].split(";") if "=" in t)
    return {"record": records[0], "policy": tags.get("p"), "subdomain_policy": tags.get("sp"),
            "percent": int(tags["pct"]) if tags.get("pct", "").isdigit() else None,
            "reports_to": tags.get("rua")}


async def domain_dns(ctx, payload: dict) -> dict:
    req = parse_domain(payload)

    async def call(provider):
        return await provider.inspect(req["domain_ascii"], check_tls=req["tls"])
    value = await ctx.run("inspect", dnsintel.PROVIDERS, call, per_attempt_seconds=30, max_attempts=2)
    notes = []
    records = value["records"]
    spf, dmarc = _spf(records["txt"]), _dmarc(value["dmarc_records"])
    if not value["exists"]:
        notes.append("The domain does not exist in DNS (NXDOMAIN).")
    elif not records["mx"]:
        notes.append("No MX record: the domain does not publish a mail server.")
    if value["exists"] and records["mx"] and not spf:
        notes.append("No SPF record: anyone can claim to send mail from this domain.")
    if value["exists"] and records["mx"] and not dmarc:
        notes.append("No DMARC record: receivers get no policy for mail that fails SPF/DKIM.")
    if spf and spf["multiple"]:
        notes.append("More than one SPF record: receivers treat that as an error (RFC 7208).")
    tls = value["tls"]
    if tls and tls.get("valid") and tls.get("days_remaining") is not None and tls["days_remaining"] < 14:
        notes.append(f"The certificate expires in {tls['days_remaining']} days.")
    if value["lookup_errors"]:
        notes.append("No answer in time for: " + ", ".join(value["lookup_errors"]) + ".")
    notes.append("Registration details (registrar, dates) are not included: registries' RDAP/WHOIS terms restrict "
                 "repackaging them; query the registry's RDAP service directly.")
    return {"domain": req["domain"], "domain_ascii": req["domain_ascii"], "exists": value["exists"],
            "records": records, "email_security": {"spf": spf, "dmarc": dmarc},
            "tls": tls, "notes": notes, "checked_at": _now()}


def _precheck(parser):
    def check(payload):
        parser(payload)
    return check


SKILLS = {"phone.parse": phone_parse, "ip.lookup": ip_lookup, "domain.dns": domain_dns}
PRECHECKS = {"phone.parse": _precheck(parse_phone), "ip.lookup": _precheck(parse_ip),
             "domain.dns": _precheck(parse_domain)}
