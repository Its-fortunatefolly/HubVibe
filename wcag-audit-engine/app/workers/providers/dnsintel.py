"""A domain's public DNS and its TLS certificate, asked of the source.

Every record comes from DNS itself (A, AAAA, CNAME, MX, NS, TXT, CAA, SOA,
plus the _dmarc record), and the certificate from the domain's own HTTPS
server, verified the way a browser would. No registry, WHOIS or third-party
database is used: registries' RDAP/WHOIS terms restrict repackaging their
data, so registration details are left to the caller's own lookup.

Only public addresses are dialled for the certificate (the same guard as the
mail checker), so the check can never be turned on an internal host.
"""

import asyncio
import socket
import ssl
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import dns.resolver

from .. import runtime
from .mailcheck import _public_ips

RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "CAA", "SOA")
_LIFETIME = 6.0


def _rdata(rtype: str, r) -> str:
    if rtype == "MX":
        return f"{int(r.preference)} {str(r.exchange).rstrip('.').lower()}"
    if rtype == "TXT":
        return b"".join(r.strings).decode("utf-8", errors="replace")
    if rtype in ("CNAME", "NS"):
        return str(r.target).rstrip(".").lower()
    if rtype == "SOA":
        return (f"{str(r.mname).rstrip('.').lower()} {str(r.rname).rstrip('.').lower()} "
                f"serial={r.serial} refresh={r.refresh} retry={r.retry} expire={r.expire} minimum={r.minimum}")
    return r.to_text()


async def _resolve(resolver, name: str, rtype: str):
    """(records, status) where status is ok | none | nxdomain | error."""
    try:
        answer = await resolver.resolve(name, rtype)
        return [_rdata(rtype, r) for r in answer], "ok"
    except dns.resolver.NXDOMAIN:
        return [], "nxdomain"
    except dns.resolver.NoAnswer:
        return [], "none"
    except (dns.resolver.NoNameservers, dns.exception.Timeout):
        return [], "error"


def _dn_to_dict(dn) -> dict:
    out = {}
    for rdn in dn or ():
        for key, value in rdn:
            out.setdefault(key, value)
    return out


def _cert_time(text: str):
    try:
        return datetime.strptime(text, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _tls_probe(host: str, ip: str) -> dict:
    """Blocking: one verified TLS handshake to `ip`:443 with SNI `host`."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
                version = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return {"reachable": True, "valid": False, "error": (exc.verify_message or str(exc))[:200]}
    except (ssl.SSLError, OSError) as exc:
        return {"reachable": False, "valid": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    not_before, not_after = _cert_time(cert.get("notBefore")), _cert_time(cert.get("notAfter"))
    now = datetime.now(timezone.utc)
    issuer, subject = _dn_to_dict(cert.get("issuer")), _dn_to_dict(cert.get("subject"))
    return {
        "reachable": True, "valid": True, "error": None, "protocol": version,
        "subject": subject.get("commonName"),
        "issuer": issuer.get("organizationName") or issuer.get("commonName"),
        "names": sorted({v for k, v in cert.get("subjectAltName", ()) if k == "DNS"}),
        "not_before": not_before.isoformat().replace("+00:00", "Z") if not_before else None,
        "not_after": not_after.isoformat().replace("+00:00", "Z") if not_after else None,
        "days_remaining": (not_after - now).days if not_after else None,
    }


class _DnsIntel:
    id = "dns-and-tls"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def inspect(self, domain_ascii: str, check_tls: bool = True) -> runtime.ProviderResult:
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = _LIFETIME
        results = await asyncio.gather(*(_resolve(resolver, domain_ascii, t) for t in RECORD_TYPES),
                                       _resolve(resolver, f"_dmarc.{domain_ascii}", "TXT"))
        statuses = [s for _, s in results[:len(RECORD_TYPES)]]
        if all(s == "error" for s in statuses):
            raise runtime.TransientProviderError(f"No nameserver answered for {domain_ascii}.")
        records = {t.lower(): recs for t, (recs, _) in zip(RECORD_TYPES, results)}
        exists = not any(s == "nxdomain" for s in statuses)
        tls = None
        if check_tls and exists:
            ips = await asyncio.to_thread(_public_ips, domain_ascii)
            if ips:
                tls = await asyncio.to_thread(_tls_probe, domain_ascii, ips[0])
                tls["ip"] = ips[0]
            else:
                tls = {"reachable": False, "valid": False, "ip": None,
                       "error": "The domain has no public address to connect to."}
        value = {"exists": exists, "records": records,
                 "dmarc_records": [r for r in results[-1][0] if r.lower().startswith("v=dmarc1")],
                 "lookup_errors": [t for t, s in zip(RECORD_TYPES, statuses) if s == "error"], "tls": tls}
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"exists={exists} tls={bool(tls and tls.get('valid'))}")


DNSINTEL = _DnsIntel()
PROVIDERS = [DNSINTEL]
