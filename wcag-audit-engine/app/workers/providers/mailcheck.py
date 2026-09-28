"""Email deliverability, measured by this node: DNS plus one SMTP conversation.

No third-party data service sits in the path. The domain's own DNS says
whether it takes mail (MX; the implicit A/AAAA fallback of RFC 5321 5.1; a
null MX per RFC 7505 says it never does), and the domain's own mail server
says whether the mailbox exists: EHLO, MAIL FROM, RCPT TO, QUIT. DATA is
never sent, so no message is ever delivered. A second RCPT for a random
mailbox on the same domain separates a catch-all server (accepts every
address, proves nothing) from a real yes.

Only public addresses are dialled: an MX that resolves to a private,
loopback or link-local address is skipped, so the probe cannot be pointed
at anything inside a network.

Disposable domains: the community list at
github.com/disposable-email-domains (CC0 1.0), refreshed at most once a day,
with the snapshot bundled next to this file as the fallback.
"""

import asyncio
import ipaddress
import os
import re
import smtplib
import socket
import ssl
import time
from pathlib import Path
from typing import Optional

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx

from .. import runtime

_DNS_LIFETIME = float(os.environ.get("WORKER_MAILCHECK_DNS_SECONDS", "8"))
_SMTP_TIMEOUT = float(os.environ.get("WORKER_MAILCHECK_SMTP_SECONDS", "12"))
HELO_NAME = os.environ.get("WORKER_MAILCHECK_HELO", "hubvibe-io.com")
MAIL_FROM = os.environ.get("WORKER_MAILCHECK_FROM", "verify@hubvibe-io.com")
MAX_MX_TRIED = 2
DISPOSABLE_URL = os.environ.get(
    "WORKER_DISPOSABLE_LIST_URL",
    "https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/main/disposable_email_blocklist.conf")
_DISPOSABLE_TTL = 24 * 3600
_SNAPSHOT = Path(__file__).with_name("disposable_email_domains.txt")
_SNAPSHOT_DATE = "2026-09-27"

# Enhanced status codes (RFC 3463) that mean "no such mailbox", and the
# words servers use for it when they send no enhanced code.
_NO_MAILBOX_CODES = re.compile(r"\b5\.1\.(0|1|6|10)\b")
_NO_MAILBOX_WORDS = re.compile(
    r"user unknown|unknown user|no such user|does not exist|doesn't exist|not exist|no mailbox|"
    r"mailbox unavailable|mailbox not found|recipient not found|unknown recipient|invalid recipient|"
    r"recipient address rejected|address rejected|not a valid mailbox|account.*disabled|inactive",
    re.IGNORECASE)
_POLICY_WORDS = re.compile(
    r"spamhaus|blocked|blacklist|denylist|\brbl\b|policy|reputation|\bpbl\b|not allowed|access denied|"
    r"5\.7\.\d|relay access|relaying denied|authenticat|too many|rate limit", re.IGNORECASE)

_MAIL_PROVIDERS = (
    ("google", re.compile(r"(^|\.)(google|googlemail)\.com\.?$")),
    ("microsoft", re.compile(r"\.(mail\.protection\.outlook\.com|olc\.protection\.outlook\.com|hotmail\.com)\.?$")),
    ("yahoo", re.compile(r"\.(yahoodns\.net|yahoo\.com)\.?$")),
    ("apple_icloud", re.compile(r"\.(icloud\.com|me\.com)\.?$")),
    ("zoho", re.compile(r"\.zoho(mail)?\.(com|eu|in)\.?$")),
    ("proton", re.compile(r"\.protonmail\.ch\.?$")),
    ("fastmail", re.compile(r"\.messagingengine\.com\.?$")),
    ("amazon_ses", re.compile(r"\.amazonaws\.com\.?$")),
    ("proofpoint", re.compile(r"\.pphosted\.com\.?$")),
    ("mimecast", re.compile(r"\.mimecast\.com\.?$")),
    ("gmx_web_de", re.compile(r"\.(gmx\.net|web\.de)\.?$")),
    ("yandex", re.compile(r"\.yandex\.(ru|net)\.?$")),
    ("naver", re.compile(r"\.naver\.com\.?$")),
    ("qq_tencent", re.compile(r"\.qq\.com\.?$")),
)


def mail_provider(mx_hosts: list) -> Optional[str]:
    for host in mx_hosts:
        for name, pattern in _MAIL_PROVIDERS:
            if pattern.search(host.lower()):
                return name
    return None


# --- disposable list ----------------------------------------------------------

_disposable = {"domains": None, "fetched": 0.0, "as_of": None, "source": None}


def _parse_list(text: str) -> set:
    return {line.strip().lower() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")}


def _snapshot() -> set:
    try:
        return _parse_list(_SNAPSHOT.read_text(encoding="utf-8"))
    except OSError:
        return set()


async def disposable_domains() -> tuple:
    """(domains, as_of, source). The live list at most a day old, else the
    bundled snapshot -- a failed refresh never fails the job."""
    now = time.time()
    if _disposable["domains"] is not None and now - _disposable["fetched"] < _DISPOSABLE_TTL:
        return _disposable["domains"], _disposable["as_of"], _disposable["source"]
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            response = await client.get(DISPOSABLE_URL, headers={"User-Agent": "HubVibe-worker/1.0 (+https://hubvibe-io.com)"})
        domains = _parse_list(response.text) if response.status_code == 200 else set()
    except httpx.HTTPError:
        domains = set()
    if len(domains) > 1000:
        _disposable.update(domains=domains, fetched=now, source="live",
                           as_of=time.strftime("%Y-%m-%d", time.gmtime(now)))
    elif _disposable["domains"] is None:
        _disposable.update(domains=_snapshot(), fetched=now, source="snapshot", as_of=_SNAPSHOT_DATE)
    else:
        _disposable["fetched"] = now  # keep the last good list; retry tomorrow
    return _disposable["domains"], _disposable["as_of"], _disposable["source"]


def is_disposable(domain: str, domains: set) -> bool:
    labels = domain.lower().split(".")
    return any(".".join(labels[i:]) in domains for i in range(len(labels) - 1))


# --- DNS ------------------------------------------------------------------------

class _Dns:
    id = "dns-resolver"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def mail_hosts(self, domain_ascii: str) -> runtime.ProviderResult:
        """Where mail for the domain goes: MX records, the implicit A/AAAA
        fallback, a null MX, or no such domain."""
        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = _DNS_LIFETIME
        value = {"domain_exists": True, "null_mx": False, "mx": [], "implicit": False}
        try:
            answer = await resolver.resolve(domain_ascii, "MX")
            records = sorted(({"host": str(r.exchange).rstrip(".").lower(), "priority": int(r.preference)}
                              for r in answer), key=lambda r: (r["priority"], r["host"]))
            if len(records) == 1 and records[0]["host"] in ("", ".") and records[0]["priority"] == 0:
                value["null_mx"] = True
            else:
                value["mx"] = [r for r in records if r["host"] not in ("", ".")]
        except dns.resolver.NXDOMAIN:
            value["domain_exists"] = False
        except dns.resolver.NoAnswer:
            value["implicit"] = await self._has_address(resolver, domain_ascii)
            if value["implicit"]:
                value["mx"] = [{"host": domain_ascii, "priority": 0}]
        except dns.resolver.NoNameservers as exc:
            raise runtime.TransientProviderError(f"No nameserver answered for {domain_ascii}: {exc}") from exc
        except dns.exception.Timeout as exc:
            raise runtime.TransientProviderError(f"DNS lookup for {domain_ascii} timed out.") from exc
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"mx={len(value['mx'])} exists={value['domain_exists']}")

    @staticmethod
    async def _has_address(resolver, name: str) -> bool:
        for rtype in ("A", "AAAA"):
            try:
                await resolver.resolve(name, rtype)
                return True
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers, dns.exception.Timeout):
                continue
        return False


# --- SMTP -----------------------------------------------------------------------

def _public_ips(host: str) -> list:
    try:
        infos = socket.getaddrinfo(host, 25, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    out = []
    for info in infos:
        ip = info[4][0]
        try:
            if ipaddress.ip_address(ip).is_global and ip not in out:
                out.append(ip)
        except ValueError:
            continue
    return out


def classify(code: Optional[int], message: str) -> str:
    """One RCPT reply -> accepted | no_mailbox | blocked | temporary | unknown."""
    if code is None:
        return "unknown"
    if code in (250, 251):
        return "accepted"
    if 400 <= code < 500:
        return "temporary"
    if 500 <= code < 600:
        if _NO_MAILBOX_CODES.search(message) or (_NO_MAILBOX_WORDS.search(message) and not _POLICY_WORDS.search(message)):
            return "no_mailbox"
        if _POLICY_WORDS.search(message):
            return "blocked"
        return "no_mailbox" if code in (550, 551, 553) else "unknown"
    return "unknown"


def _converse(host: str, ip: str, target: str, decoy: str) -> dict:
    """One SMTP session: EHLO (+STARTTLS when offered), MAIL, RCPT target,
    RCPT decoy, QUIT. Never DATA."""
    session = smtplib.SMTP(timeout=_SMTP_TIMEOUT, local_hostname=HELO_NAME)
    session._host = host  # the name STARTTLS presents (SNI); we dial the checked IP
    try:
        code, banner = session.connect(ip, 25)
        if code != 220:
            return {"stage": "connect", "code": code, "message": banner.decode(errors="replace")}
        session.ehlo()
        if session.has_extn("starttls"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            try:
                session.starttls(context=context)
                session.ehlo()
            except (smtplib.SMTPException, ssl.SSLError, OSError, ValueError):
                pass
        code, message = session.mail(MAIL_FROM)
        if code != 250:
            return {"stage": "mail_from", "code": code, "message": message.decode(errors="replace")}
        code, message = session.rcpt(target)
        result = {"stage": "rcpt", "code": code, "message": message.decode(errors="replace"), "decoy_code": None}
        if code in (250, 251):
            decoy_code, _ = session.rcpt(decoy)
            result["decoy_code"] = decoy_code
        return result
    finally:
        try:
            session.quit()
        except (smtplib.SMTPException, OSError):
            session.close()


class _Smtp:
    id = "smtp-probe"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def probe(self, mx_hosts: list, target: str, decoy: str) -> runtime.ProviderResult:
        """Ask the domain's own mail server about `target`. Never raises for
        an answer the server gives; an unreachable server is reported, not
        retried into the ground."""
        tried = []
        for host in mx_hosts[:MAX_MX_TRIED]:
            ips = await asyncio.to_thread(_public_ips, host)
            if not ips:
                tried.append({"host": host, "outcome": "no_public_address"})
                continue
            try:
                reply = await asyncio.to_thread(_converse, host, ips[0], target, decoy)
            except (smtplib.SMTPException, OSError) as exc:
                tried.append({"host": host, "outcome": f"unreachable: {type(exc).__name__}"})
                continue
            reply["host"] = host
            reply["tried"] = tried
            return runtime.ProviderResult(value=reply, cost_micros=0, cost_measured=True,
                                          usage=f"host={host} stage={reply['stage']} code={reply.get('code')}")
        return runtime.ProviderResult(value={"stage": "unreachable", "code": None, "message": None, "host": None,
                                             "decoy_code": None, "tried": tried},
                                      cost_micros=0, cost_measured=True, usage=f"unreachable tried={len(tried)}")


DNS = _Dns()
SMTP = _Smtp()
PROVIDERS = [DNS, SMTP]
