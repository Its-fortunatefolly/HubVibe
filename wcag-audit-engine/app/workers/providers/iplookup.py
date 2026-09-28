"""IP address intelligence: where an address is, who runs its network, and
its reverse DNS name.

Location and network owner come from DB-IP's free "Lite" databases (IP to
City Lite and IP to ASN Lite), licensed Creative Commons Attribution 4.0 --
commercial use allowed with the credit "IP Geolocation by DB-IP"
(https://db-ip.com), which every answer carries. The files are updated
monthly; this node downloads the current month's once, keeps them on the
persistent volume, and reads them locally. Reverse DNS is asked of DNS
itself. Private, loopback and other non-public addresses are described from
the address alone and never looked up.
"""

import asyncio
import gzip
import ipaddress
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.reversename
import httpx
import maxminddb

from .. import runtime

ATTRIBUTION = {"text": "IP Geolocation by DB-IP", "url": "https://db-ip.com",
               "license": "Creative Commons Attribution 4.0 International"}
_DATASETS = ("city", "asn")
_UA = {"User-Agent": "HubVibe-worker/1.0 (+https://hubvibe-io.com)"}


def _data_dir() -> str:
    explicit = os.environ.get("DBIP_DATA_DIR")
    if explicit:
        return explicit
    return "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else tempfile.gettempdir()


def _months(now=None) -> list:
    now = now or datetime.now(timezone.utc)
    this = f"{now.year:04d}-{now.month:02d}"
    prev_year, prev_month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return [this, f"{prev_year:04d}-{prev_month:02d}"]


def scope_of(ip) -> str:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if getattr(ip, "ipv4_mapped", None):
        return scope_of(ip.ipv4_mapped)
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "shared"
    if ip.is_private:
        return "private"
    if ip.is_reserved or ip.is_unspecified or not ip.is_global:
        return "reserved"
    return "public"


class _DbIp:
    id = "dbip-lite"

    def __init__(self):
        self._readers = {}   # dataset -> (month, reader)
        self._lock = asyncio.Lock()
        self._checked = 0.0

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    def _path(self, dataset: str) -> str:
        return os.path.join(_data_dir(), f"dbip-{dataset}-lite.mmdb")

    def _meta_path(self) -> str:
        return os.path.join(_data_dir(), "dbip-lite.json")

    def _open_local(self):
        try:
            meta = json.load(open(self._meta_path()))
        except (OSError, ValueError):
            return
        for dataset in _DATASETS:
            month = meta.get(dataset)
            if month and dataset not in self._readers and os.path.exists(self._path(dataset)):
                try:
                    self._readers[dataset] = (month, maxminddb.open_database(self._path(dataset), maxminddb.MODE_MMAP))
                except (OSError, ValueError, maxminddb.InvalidDatabaseError):
                    continue

    async def _download(self, client, dataset: str):
        for month in _months():
            url = f"https://download.db-ip.com/free/dbip-{dataset}-lite-{month}.mmdb.gz"
            tmp = self._path(dataset) + ".download"
            try:
                async with client.stream("GET", url, headers=_UA) as response:
                    if response.status_code != 200:
                        continue
                    with open(tmp + ".gz", "wb") as fh:
                        async for chunk in response.aiter_bytes(1 << 20):
                            fh.write(chunk)
                await asyncio.to_thread(self._gunzip, tmp + ".gz", tmp)
                maxminddb.open_database(tmp).close()  # refuse a broken file before it replaces a good one
                os.replace(tmp, self._path(dataset))
                return month
            except (httpx.HTTPError, OSError, ValueError, maxminddb.InvalidDatabaseError):
                continue
            finally:
                for leftover in (tmp + ".gz", tmp):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
        return None

    @staticmethod
    def _gunzip(src: str, dst: str):
        with gzip.open(src, "rb") as fin, open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)

    async def ensure(self):
        """Open the local files; fetch this month's when missing or a month
        old. A failed download keeps the last good file."""
        if not self._readers:
            await asyncio.to_thread(self._open_local)
        current = _months()[0]
        if all(self._readers.get(d, ("",))[0] == current for d in _DATASETS):
            return
        if self._readers and time.time() - self._checked < 6 * 3600:
            return
        async with self._lock:
            if all(self._readers.get(d, ("",))[0] == current for d in _DATASETS):
                return
            self._checked = time.time()
            meta = {d: m for d, (m, _) in self._readers.items()}
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0), follow_redirects=True) as client:
                for dataset in _DATASETS:
                    if self._readers.get(dataset, ("",))[0] == current:
                        continue
                    month = await self._download(client, dataset)
                    if month:
                        old = self._readers.get(dataset)
                        self._readers[dataset] = (month, maxminddb.open_database(self._path(dataset), maxminddb.MODE_MMAP))
                        meta[dataset] = month
                        if old:
                            old[1].close()
            try:
                with open(self._meta_path(), "w") as fh:
                    json.dump(meta, fh)
            except OSError:
                pass

    async def keep_warm(self, every_seconds: int = 6 * 3600):
        while True:
            try:
                await self.ensure()
            except Exception:  # pragma: no cover
                pass
            await asyncio.sleep(every_seconds)

    async def locate(self, address: str) -> runtime.ProviderResult:
        await self.ensure()
        if "city" not in self._readers:
            raise runtime.TransientProviderError("The IP location database could not be downloaded; try again shortly.")
        city_month, city = self._readers["city"]
        record = city.get(address) or {}
        asn_month, asn_reader = self._readers.get("asn", (None, None))
        asn = (asn_reader.get(address) if asn_reader else None) or {}

        def name(part):
            names = (part or {}).get("names") or {}
            return names.get("en") or next(iter(names.values()), None)

        subdivisions = record.get("subdivisions") or []
        location = {
            "city": name(record.get("city")),
            "region": name(subdivisions[0]) if subdivisions else None,
            "country": name(record.get("country")),
            "country_code": (record.get("country") or {}).get("iso_code"),
            "continent": name(record.get("continent")),
            "latitude": (record.get("location") or {}).get("latitude"),
            "longitude": (record.get("location") or {}).get("longitude"),
        }
        value = {
            "location": location if any(v is not None for v in location.values()) else None,
            "network": {"asn": asn.get("autonomous_system_number"),
                        "organization": asn.get("autonomous_system_organization")} if asn else None,
            "database_month": city_month, "asn_database_month": asn_month,
        }
        return runtime.ProviderResult(value=value, cost_micros=0, cost_measured=True,
                                      usage=f"db={city_month} found={bool(record)}")


async def reverse_dns(address: str) -> list:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = 5
    try:
        answer = await resolver.resolve(dns.reversename.from_address(address), "PTR")
        return sorted({str(r.target).rstrip(".").lower() for r in answer})
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []


DBIP = _DbIp()
PROVIDERS = [DBIP]
