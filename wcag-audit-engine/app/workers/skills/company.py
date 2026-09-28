"""company.enrich -- who a company is, from its domain or its name, in one call.

Wikidata (CC0) finds the company by its official website or its name and
gives the facts people look up: founding year, headcount over time,
industries, headquarters, parent, CEO and founders, stock listings and
official social accounts. The LEI it carries opens the company's legal
record in the global LEI register (legal name, legal form, jurisdiction,
registration number, status); a CIK opens its SEC registration (tickers,
SIC industry, state of incorporation). The company's own homepage adds what
it says about itself (schema.org Organization), marked self-declared and
skipped when its robots.txt says no. Every field names its source.
"""

import asyncio
import re
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .. import runtime
from ..providers import company_data as C
from ..providers import web
from . import commerce

_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_ORG_TYPES = {"Organization", "Corporation", "OnlineBusiness", "LocalBusiness", "NGO", "EducationalOrganization",
              "NewsMediaOrganization", "SoftwareCompany", "Airline", "Store"}
SOCIALS = {"P2002": ("x", "https://x.com/{}"), "P4264": ("linkedin", "https://www.linkedin.com/company/{}"),
           "P2013": ("facebook", "https://www.facebook.com/{}"), "P2003": ("instagram", "https://www.instagram.com/{}"),
           "P2037": ("github", "https://github.com/{}"), "P2397": ("youtube", "https://www.youtube.com/channel/{}")}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_domain(raw: str):
    text = raw.strip().lower()
    host = urlsplit(text if "://" in text else f"https://{text}").hostname or ""
    host = host[4:] if host.startswith("www.") else host
    return host if _DOMAIN.match(host) else None


def parse(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise runtime.InvalidRequest("The request body must be a JSON object.")
    domain, name = payload.get("domain"), payload.get("name")
    if domain is None and name is None:
        raise runtime.InvalidRequest("Send `domain` (stripe.com) or `name` (Stripe).")
    if domain is not None:
        if not isinstance(domain, str) or not normalize_domain(domain):
            raise runtime.InvalidRequest("`domain` must be a company domain such as stripe.com.")
        domain = normalize_domain(domain)
    if name is not None and (not isinstance(name, str) or not 2 <= len(name.strip()) <= 120):
        raise runtime.InvalidRequest("`name` must be 2 to 120 characters.")
    return {"domain": domain, "name": name.strip() if name else None}


def precheck(payload: dict) -> None:
    parse(payload)


# --- Wikidata claims ------------------------------------------------------------

def _statements(ent: dict, pid: str) -> list:
    rows = [s for s in (ent.get("claims") or {}).get(pid, []) if s.get("rank") != "deprecated"]
    preferred = [s for s in rows if s.get("rank") == "preferred"]
    return preferred or rows


def _value(statement: dict):
    return ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")


def _ids(ent: dict, pid: str) -> list:
    return [v["id"] for v in (_value(s) for s in _statements(ent, pid)) if isinstance(v, dict) and "id" in v]


def _strings(ent: dict, pid: str) -> list:
    return [v for v in (_value(s) for s in _statements(ent, pid)) if isinstance(v, str)]


def _year(time_value) -> str:
    if not isinstance(time_value, dict) or not time_value.get("time"):
        return None
    t, precision = time_value["time"].lstrip("+"), time_value.get("precision", 11)
    return t[:4] if precision <= 9 else t[:7] if precision == 10 else t[:10]


def is_company(ent) -> bool:
    if not isinstance(ent, dict):
        return False
    claims = ent.get("claims") or {}
    return any(k in claims for k in ("P1278", "P5531", "P414", "P1128")) or bool(set(_ids(ent, "P31")) & set(C.COMPANY_TYPES))


def employees(ent: dict):
    best = None
    for s in (ent.get("claims") or {}).get("P1128", []):
        if s.get("rank") == "deprecated" or not isinstance(_value(s), dict):
            continue
        amount = _value(s).get("amount")
        when = (((s.get("qualifiers") or {}).get("P585") or [{}])[0].get("datavalue") or {}).get("value") or {}
        key = (when.get("time") or "", s.get("rank") == "preferred")
        if amount and (best is None or key > best[0]):
            best = (key, int(float(amount)), _year(when) if when.get("time") else None)
    return {"count": best[1], "as_of": best[2], "source": "wikidata"} if best else None


def listings(ent: dict) -> list:
    out = []
    for s in _statements(ent, "P414"):
        ex = _value(s)
        tick = ((((s.get("qualifiers") or {}).get("P249") or [{}])[0].get("datavalue") or {}).get("value"))
        if isinstance(ex, dict) and tick:
            out.append({"ticker": tick, "exchange_id": ex["id"]})
    return out


# --- homepage JSON-LD -------------------------------------------------------------

def organization(html: str):
    for block in commerce._jsonld_blocks(html):
        for node in commerce._nodes(block):
            types = node.get("@type")
            types = set(types) if isinstance(types, list) else {types}
            if types & _ORG_TYPES and node.get("name"):
                return node
    return None


def _text(v):
    if isinstance(v, dict):
        return v.get("url") or v.get("@id") or v.get("name")
    return v if isinstance(v, str) else None


async def homepage(ctx, domain: str, notes: list):
    """The company's own homepage, bare domain first, then www."""
    last_problem = None
    for host in (domain, f"www.{domain}"):
        try:
            org, final_url = await _homepage(ctx, host, notes)
            return org, final_url
        except runtime.InvalidRequest as exc:  # does not resolve, blocked target
            last_problem = exc
    notes.append(f"Homepage not read: {getattr(last_problem, 'detail', last_problem)}"[:200])
    return None, None


async def _homepage(ctx, domain: str, notes: list):
    base = f"https://{domain}"

    async def fetch(url):
        async def call(provider):
            return await provider.fetch(url, max_chars=1_500_000)
        return await ctx.run("website", web.FETCH_PROVIDERS, call, per_attempt_seconds=15, max_attempts=1)
    try:
        robots = await fetch(f"{base}/robots.txt")
        if robots.get("status") == 200 and robots.get("text"):
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(robots["text"].splitlines())
            if not rp.can_fetch("HubVibe", f"{base}/"):
                notes.append("The company's robots.txt asks automated readers not to fetch its homepage; it was not read.")
                return None, None
        page = await fetch(f"{base}/")
    except runtime.InvalidRequest:
        raise
    except runtime.WorkerError as exc:
        notes.append(f"Homepage not read: {getattr(exc, 'detail', exc)}"[:200])
        return None, None
    if page.get("status") != 200 or not page.get("text"):
        return None, page.get("final_url")
    html = page["text"]
    org = organization(html)
    if org is None:
        # No schema.org block: what the page says about itself in its head and
        # links, still self-declared.
        name = _meta(html, "og:site_name") or _title(html)
        if name:
            org = {"name": name, "description": _meta(html, "og:description") or _meta(html, "description"),
                   "sameAs": sorted(set(_SOCIAL_LINK.findall(html)))[:20], "_derived": True}
    return org, page.get("final_url")


_SOCIAL_LINK = re.compile(r'href="(https?://(?:www\.)?(?:x\.com|twitter\.com|linkedin\.com/company|facebook\.com|'
                          r'instagram\.com|github\.com|youtube\.com)/[^"?#]+)"', re.I)


def _meta(html: str, key: str):
    m = (re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)', html, re.I)
         or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(key)}["\']', html, re.I))
    return m.group(1).strip() if m else None


def _title(html: str):
    m = re.search(r"<title[^>]*>([^<]{1,200})</title>", html, re.I)
    if not m:
        return None
    return re.split(r"\s[|\-–—:]\s", m.group(1).strip())[0].strip() or None


# --- the job ----------------------------------------------------------------------

async def enrich(ctx, payload: dict) -> dict:
    req = parse(payload)
    notes, sources, field_src = [], [], {}

    async def run(step, provider, fn, attempts=2):
        return await ctx.run(step, [provider], fn, per_attempt_seconds=20, max_attempts=attempts)

    qid, ent = None, None
    if req["domain"]:
        ids = await run("wikidata_find", C.WIKIDATA, lambda p: p.by_domain(req["domain"]))
        for cand_id in ids[:3]:
            cand = await run("wikidata_entity", C.WIKIDATA, lambda p, q=cand_id: p.entity(q))
            if is_company(cand):
                qid, ent = cand_id, cand
                break
    if qid is None and req["name"]:
        hits = await run("wikidata_search", C.WIKIDATA, lambda p: p.search(req["name"]))
        for hit in hits[:3]:
            cand = await run("wikidata_entity", C.WIKIDATA, lambda p, h=hit: p.entity(h["id"]))
            if is_company(cand):
                qid, ent = hit["id"], cand
                break
    if qid and ent is None:
        ent = await run("wikidata_entity", C.WIKIDATA, lambda p: p.entity(qid))


    domain = req["domain"]
    if ent and not domain:
        site = next(iter(_strings(ent, "P856")), None)
        domain = normalize_domain(site) if site else None
    jobs = {"home": homepage(ctx, domain, notes) if domain else asyncio.sleep(0, (None, None))}
    lei = next(iter(_strings(ent, "P1278")), None) if ent else None
    cik = next(iter(_strings(ent, "P5531")), None) if ent else None
    home_org, final_url = await jobs["home"]
    final_host = normalize_domain(final_url) if final_url else None
    if ent is None and final_host and final_host != domain:
        # The site moved (toyota.co.jp -> global.toyota): look the company up where it lives now.
        ids = await run("wikidata_find", C.WIKIDATA, lambda p: p.by_domain(final_host))
        for cand_id in ids[:3]:
            cand = await run("wikidata_entity", C.WIKIDATA, lambda p, q=cand_id: p.entity(q))
            if is_company(cand):
                qid, ent = cand_id, cand
                break
        if ent:
            lei = next(iter(_strings(ent, "P1278")), None)
            cik = next(iter(_strings(ent, "P5531")), None)
    if not lei and home_org and isinstance(home_org.get("leiCode"), str):
        lei = home_org["leiCode"].strip().upper()
        field_src["lei"] = "website"

    async def gleif():
        if lei:
            return await run("gleif", C.LEI, lambda p: p.by_lei(lei))
        if req["name"] and not ent:
            rows = await run("gleif_search", C.LEI, lambda p: p.search(req["name"]))
            return rows[0] if rows else None
        return None

    async def sec():
        return await run("sec", C.SEC, lambda p: p.submissions(int(cik))) if cik else None

    async def safely(coro, name):
        try:
            return await coro
        except runtime.WorkerError as exc:
            notes.append(f"{name}: {getattr(exc, 'detail', exc)}"[:200])
            return None
    lei_rec, filer = await asyncio.gather(safely(gleif(), "LEI register"), safely(sec(), "SEC"))

    if not ent and not lei_rec and not home_org:
        raise runtime.InvalidRequest("No company was found for this domain or name in Wikidata, the LEI register or "
                                     "on the site itself. Nothing was charged.")

    # labels for every item Wikidata refers to
    refs = []
    if ent:
        for pid in ("P452", "P159", "P17", "P749", "P169", "P112"):
            refs += _ids(ent, pid)
        refs += [x["exchange_id"] for x in listings(ent)]
    labels = await run("wikidata_labels", C.WIKIDATA, lambda p: p.labels(sorted(set(refs)))) if refs else {}
    lab = lambda q: (labels.get(q) or {}).get("label")  # noqa: E731

    le = ((lei_rec or {}).get("attributes") or {}).get("entity") or {}
    lei_attrs = (lei_rec or {}).get("attributes") or {}
    legal_form = None
    if le.get("legalForm", {}).get("id") and le["legalForm"]["id"] not in ("8888", "9999"):
        try:
            legal_form = await run("gleif_form", C.LEI, lambda p: p.legal_form(le["legalForm"]["id"]), attempts=1)
        except runtime.WorkerError:
            legal_form = None
    legal_form = legal_form or (le.get("legalForm") or {}).get("other")

    def pick(field, *candidates):
        for src, val in candidates:
            if val not in (None, "", []):
                field_src[field] = src
                return val
        return None

    wd_name = (((ent or {}).get("labels") or {}).get("en") or {}).get("value") if ent else None
    wd_desc = (((ent or {}).get("descriptions") or {}).get("en") or {}).get("value") if ent else None
    hq_ids = _ids(ent, "P159") if ent else []
    country_ids = _ids(ent, "P17") if ent else []
    hq_addr = le.get("headquartersAddress") or {}
    home_addr = (home_org or {}).get("address") if isinstance((home_org or {}).get("address"), dict) else {}
    emp = employees(ent) if ent else None
    if emp is None and home_org and home_org.get("numberOfEmployees"):
        n = home_org["numberOfEmployees"]
        n = n.get("value") if isinstance(n, dict) else n
        try:
            emp = {"count": int(float(n)), "as_of": None, "source": "website"}
        except (TypeError, ValueError):
            emp = None
    socials = {}
    if ent:
        for pid, (key, fmt) in SOCIALS.items():
            v = next(iter(_strings(ent, pid)), None)
            if v:
                socials[key] = fmt.format(v)
    for link in ((home_org or {}).get("sameAs") or []) if isinstance((home_org or {}).get("sameAs"), list) else []:
        if isinstance(link, str):
            for key, host in (("x", "x.com"), ("x", "twitter.com"), ("linkedin", "linkedin.com"), ("facebook", "facebook.com"),
                              ("instagram", "instagram.com"), ("github", "github.com"), ("youtube", "youtube.com")):
                if host in link and key not in socials:
                    socials[key] = link
    logo = next(iter(_strings(ent, "P154")), None) if ent else None
    company = {
        "name": pick("name", ("wikidata", wd_name), ("website", (home_org or {}).get("name")),
                     ("lei_register", (le.get("legalName") or {}).get("name")), ("sec", (filer or {}).get("name"))),
        "legal_name": pick("legal_name", ("lei_register", (le.get("legalName") or {}).get("name")),
                           ("sec", (filer or {}).get("name")), ("website", (home_org or {}).get("legalName"))),
        "description": pick("description", ("wikidata", wd_desc), ("website", (home_org or {}).get("description"))),
        "website": pick("website", ("wikidata", next(iter(_strings(ent, "P856")), None) if ent else None),
                        ("website", final_url)),
        "domain": domain,
        "founded": pick("founded", ("wikidata", _year(_value(_statements(ent, "P571")[0])) if ent and _statements(ent, "P571") else None),
                        ("website", (home_org or {}).get("foundingDate")),
                        ("lei_register", (le.get("creationDate") or lei_attrs.get("registration", {}).get("initialRegistrationDate") or "")[:10] or None)),
        "employees": emp,
        "industries": [lab(q) for q in (_ids(ent, "P452") if ent else []) if lab(q)]
                      or ([filer["sicDescription"]] if filer and filer.get("sicDescription") else []),
        "headquarters": {
            "city": pick("hq_city", ("wikidata", lab(hq_ids[0]) if hq_ids else None), ("lei_register", hq_addr.get("city")),
                         ("website", (home_addr or {}).get("addressLocality"))),
            "country": pick("hq_country", ("wikidata", lab(country_ids[0]) if country_ids else None),
                            ("lei_register", hq_addr.get("country")), ("website", _text((home_addr or {}).get("addressCountry")))),
            "country_code": (labels.get(country_ids[0]) or {}).get("iso2") if country_ids else hq_addr.get("country"),
            "address": ", ".join(x for x in (hq_addr.get("addressLines") or []) + [hq_addr.get("city"), hq_addr.get("postalCode")] if x) or None,
        },
        "parent": lab(_ids(ent, "P749")[0]) if ent and _ids(ent, "P749") else None,
        "ceo": lab(_ids(ent, "P169")[0]) if ent and _ids(ent, "P169") else None,
        "founders": [lab(q) for q in (_ids(ent, "P112") if ent else []) if lab(q)],
        "logo_url": ("https://commons.wikimedia.org/wiki/Special:FilePath/" + logo.replace(" ", "_")) if logo else _text((home_org or {}).get("logo")),
        "status": le.get("status"),
        "legal_form": legal_form,
        "jurisdiction": le.get("jurisdiction"),
    }
    identifiers = {
        "wikidata": qid, "lei": lei if lei_rec else lei,
        "cik": str(int(cik)) if cik else None,
        "registration_number": le.get("registeredAs"),
        "registration_authority": (le.get("registeredAt") or {}).get("id"),
        "listings": [{"ticker": x["ticker"], "exchange": lab(x["exchange_id"])} for x in (listings(ent) if ent else [])],
        "sec_tickers": (filer or {}).get("tickers") or [],
        "sic": (filer or {}).get("sic"), "sic_description": (filer or {}).get("sicDescription"),
        "state_of_incorporation": (filer or {}).get("stateOfIncorporation") or None,
    }
    for label, used in (("Wikidata (CC0)", ent), ("Global LEI register (CC0)", lei_rec), ("SEC EDGAR", filer),
                        ("Company website (self-declared)", home_org)):
        if used:
            sources.append(label)
    return {"query": {"domain": req["domain"], "name": req["name"]}, "company": company, "identifiers": identifiers,
            "socials": {k: socials.get(k) for k in ("x", "linkedin", "facebook", "instagram", "github", "youtube")},
            "sources": sources, "field_sources": field_src, "notes": notes, "checked_at": _now()}


SKILLS = {"company.enrich": enrich}
PRECHECKS = {"company.enrich": precheck}
