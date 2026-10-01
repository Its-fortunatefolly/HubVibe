"""The purchase book: one durable row per purchase, on every rail.

WHY THIS EXISTS

Until 1.30.0 the node could not answer the owner's four questions about a
sale: who bought, what exactly they asked for, where the request came from,
and whether every payment that reached the wallet was captured. Worker calls
kept a ledger row with a hash of the request and nothing about the client;
site audits kept nothing but an INFO line ("x402 SETTLED ...") that every
container rebuild deleted. Audit sales from 2026-09-14 and 2026-09-27 exist
only on the chain.

This book writes one row per purchase to the persistent volume (the worker
ledger's own SQLite file, in a table of its own): the buyer's wallet and
transaction, the request itself (secrets redacted, oversized fields cut),
the client address and its location from the local DB-IP files, the
User-Agent, and the outcome. `purchase_reconcile` then checks the book
against every payment the chain shows for this node's wallets.

WHAT IT NEVER DOES

It never raises into a paid call and never refuses one: a sale that cannot
be written costs us the row, not the customer's result. It never stores a
raw API key, a payment header or a Solana challenge token. It never sends a
request body or a client address off the box (the optional alert webhook
gets the product, the price, the payer and a coarse location only).

It imports no payment module: app.main hands it what it needs (the MPP
settlement lookup, the facilitator URL), the same way the worker router is
handed the payment gate.
"""

import base64
import csv
import datetime
import hashlib
import io
import ipaddress
import json
import logging
import os
import queue
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
import maxminddb

log = logging.getLogger("hubvibe.purchases")

DEFAULT_PATH = "/data/hubvibe-workers.db"
# The owner's own seeding wallet: its purchases are real transactions, but
# they are the owner paying herself to get listed, not demand.
SEED_WALLET = "0x104fea79f30b4fb4da86b6d65951217f914bdd35"
# Exact key names, case-insensitive, at any depth of a request body. "token"
# alone is NOT here: market and chain workers take token symbols and
# addresses under that name, and those are the request.
_REDACT_KEYS = frozenset({
    "api_key", "apikey", "x-api-key", "access_token", "refresh_token", "auth_token",
    "bearer", "password", "passwd", "secret", "client_secret", "private_key", "mnemonic",
    "seed_phrase", "challenge_token", "authorization"})
_STRING_CAP = 4096
_DEFAULT_REQUEST_MAX_BYTES = 16384
_MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
_TOKEN_PROGRAMS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                   "TokenzQdBNbLqP5VEhdkAS6EPFLC1PazBwBDKHkHQZr"}
# x402 v1 names networks; v2 uses CAIP-2. The book always stores CAIP-2 so a
# v1 and a v2 payment on the same chain read the same.
_V1_NETWORKS = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
}
_USDC_DECIMALS = 6

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_configured_path: Optional[str] = None
_last_error: Optional[str] = None
_degraded = False

_node_version: Optional[str] = None
_mpp_facts: Optional[Callable] = None
_facilitator_host: Optional[str] = None


def _path() -> str:
    return (os.environ.get("PURCHASE_BOOK_PATH") or os.environ.get("WORKER_LEDGER_PATH")
            or DEFAULT_PATH)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    internal INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live',
    route TEXT, product TEXT, transport TEXT,
    price_usd REAL, received_usd REAL,
    rail TEXT, method TEXT,
    network TEXT, asset TEXT, pay_to TEXT, amount_atomic INTEGER,
    payer TEXT, tx_hash TEXT, payment_ref TEXT, payment_nonce TEXT,
    settle_state TEXT, settle_error TEXT, x402_version INTEGER, facilitator TEXT,
    call_id TEXT, receipt_id TEXT, idempotency_key TEXT,
    api_key_hash TEXT, contract_hash TEXT,
    request_json TEXT, request_bytes INTEGER, request_hash TEXT,
    request_truncated INTEGER NOT NULL DEFAULT 0,
    client_ip TEXT, peer_ip TEXT,
    ip_country TEXT, ip_country_code TEXT, ip_region TEXT, ip_city TEXT,
    ip_asn INTEGER, ip_org TEXT,
    user_agent TEXT, referer TEXT,
    chain_status TEXT, reconciled_at REAL,
    note TEXT, node_version TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pur_ref ON purchases(payment_ref)
    WHERE payment_ref IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pur_call ON purchases(call_id)
    WHERE call_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pur_tx ON purchases(tx_hash);
CREATE INDEX IF NOT EXISTS idx_pur_ts ON purchases(ts);
CREATE INDEX IF NOT EXISTS idx_pur_payer ON purchases(payer);
"""

# Every column a row may carry, in table order (id excluded). insert_row
# writes only these, so a caller's stray key can never reach the SQL.
_COLUMNS = (
    "ts", "kind", "outcome", "internal", "source", "route", "product", "transport",
    "price_usd", "received_usd", "rail", "method", "network", "asset", "pay_to",
    "amount_atomic", "payer", "tx_hash", "payment_ref", "payment_nonce", "settle_state",
    "settle_error", "x402_version", "facilitator", "call_id", "receipt_id",
    "idempotency_key", "api_key_hash", "contract_hash", "request_json", "request_bytes",
    "request_hash", "request_truncated", "client_ip", "peer_ip", "ip_country",
    "ip_country_code", "ip_region", "ip_city", "ip_asn", "ip_org", "user_agent",
    "referer", "chain_status", "reconciled_at", "note", "node_version",
)

# Columns added after the table first shipped, as (name, SQL type). CREATE
# TABLE IF NOT EXISTS cannot add them to a book that already exists on the
# box, so _migrate adds each one that is missing. Nullable, always.
_ADDED_COLUMNS: tuple = ()


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row[1] for row in conn.execute("PRAGMA table_info(purchases)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE purchases ADD COLUMN {name} {kind}")
    conn.commit()


def _note(exc: BaseException) -> None:
    global _last_error
    _last_error = f"{type(exc).__name__}: {exc}"
    log.warning("purchase book: %s", _last_error)


def _connect() -> Optional[sqlite3.Connection]:
    """Open the book once, degrading to in-memory if the path is unwritable.

    In-memory rather than off, for the same reason the worker ledger does it:
    a node on a read-only volume still answers "what did this instance sell",
    and /owner/purchases reports the degradation. The fall-back is logged at
    ERROR because it means rows are being lost on the next restart.
    """
    global _conn, _configured_path, _last_error, _degraded
    target = _path()
    if _conn is not None and _configured_path == target:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None
    for candidate, degraded in ((target, False), (":memory:", True)):
        try:
            if candidate != ":memory:":
                Path(candidate).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(candidate, check_same_thread=False, timeout=2.0)
            conn.row_factory = sqlite3.Row
            if candidate != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
            _conn, _configured_path, _degraded = conn, target, degraded
            _last_error = f"purchase book degraded to in-memory: {target} unwritable" \
                if degraded else None
            if degraded:
                log.error("purchase book degraded to in-memory (%s unwritable): "
                          "rows written now are lost on restart", target)
            return _conn
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {exc}"
            continue
    return None


def _safe_connect() -> Optional[sqlite3.Connection]:
    """_connect(), guaranteed not to raise. Every public function uses this."""
    try:
        return _connect()
    except Exception as exc:  # pragma: no cover - defensive
        _note(exc)
        return None


def connect() -> Optional[sqlite3.Connection]:
    """The book's connection, for purchase_reconcile."""
    with _lock:
        return _safe_connect()


def configure(*, node_version: str, mpp_facts: Optional[Callable] = None,
              facilitator_url: Optional[str] = None) -> None:
    """Hand the book what it cannot import: the node's version, the MPP
    settlement lookup and the facilitator's URL (only its host is kept)."""
    global _node_version, _mpp_facts, _facilitator_host
    _node_version = node_version
    _mpp_facts = mpp_facts
    try:
        _facilitator_host = urlparse(facilitator_url).hostname if facilitator_url else None
    except Exception:
        _facilitator_host = None


def reset_for_tests() -> None:
    global _conn, _configured_path, _last_error, _degraded
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _configured_path = _last_error = None
        _degraded = False


def status() -> dict:
    with _lock:
        conn = _safe_connect()
        rows = None
        if conn is not None:
            try:
                rows = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
            except Exception as exc:
                _note(exc)
        return {"available": conn is not None, "degraded": _degraded, "path": _path(),
                "last_error": _last_error, "rows": rows}


# --- request side: cheap, never raises -------------------------------------------------

_DEFAULT_TRUSTED = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
_trusted_cache: tuple = (None, [])


def _trusted() -> list:
    """PURCHASE_TRUSTED_PROXIES as networks, re-parsed only when it changes."""
    global _trusted_cache
    text = os.environ.get("PURCHASE_TRUSTED_PROXIES", _DEFAULT_TRUSTED)
    if _trusted_cache[0] != text:
        nets = []
        for part in text.split(","):
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                continue
        _trusted_cache = (text, nets)
    return _trusted_cache[1]


def _is_trusted(address: Optional[str]) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip in net for net in _trusted())


def client_ip(request) -> tuple:
    """(client_ip, peer_ip): who sent the request, as far as the proxy in
    front of this node vouches for it.

    The TCP peer is believed first. Only when the peer is a trusted proxy
    (Caddy on the box's Docker network; by default any private address) is
    X-Forwarded-For read, from the right: the right-most address that is not
    itself a trusted proxy is the one the proxy saw. A client can prepend
    anything to that header; it cannot append after the proxy. A public peer
    sending X-Forwarded-For is ignored outright -- it is the client, and its
    header is its own claim. No other forwarding header is ever read.
    """
    try:
        client = getattr(request, "client", None) if request is not None else None
        peer = client.host if client else None
        if not _is_trusted(peer):
            return peer, peer
        forwarded = request.headers.get("x-forwarded-for") or ""
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        for hop in reversed(hops):
            if not _is_trusted(hop):
                return hop[:64], peer
        return (hops[0][:64] if hops else peer), peer
    except Exception:
        return None, None


def _cut(value):
    """(value with secrets redacted and long strings cut, whether anything
    was cut)."""
    if isinstance(value, str):
        if len(value) > _STRING_CAP:
            return value[:_STRING_CAP] + f"…[+{len(value) - _STRING_CAP} chars]", True
        return value, False
    if isinstance(value, dict):
        out, cut = {}, False
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _REDACT_KEYS:
                out[key] = "[redacted]"
                continue
            out[key], was_cut = _cut(item)
            cut = cut or was_cut
        return out, cut
    if isinstance(value, (list, tuple)):
        out, cut = [], False
        for item in value:
            cleaned, was_cut = _cut(item)
            out.append(cleaned)
            cut = cut or was_cut
        return out, cut
    return value, False


def _request_max_bytes() -> int:
    try:
        return max(256, int(os.environ.get("PURCHASE_REQUEST_MAX_BYTES",
                                           str(_DEFAULT_REQUEST_MAX_BYTES))))
    except ValueError:
        return _DEFAULT_REQUEST_MAX_BYTES


def _canonical(value) -> bytes:
    """The worker ledger's canonical encoding (ledger.canonical_hash), so a
    book row's request_hash joins to worker_calls.request_hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def body_record(body) -> dict:
    """What the buyer asked for: the canonical hash of the full body, its
    size, and a stored copy with secrets redacted and oversized parts cut.

    `request_truncated` is 1 whenever the stored copy is not the whole body
    (a string over 4,096 characters -- an audit's raw html -- or the whole
    value over PURCHASE_REQUEST_MAX_BYTES). The hash is always over the
    full, unredacted body, so it still proves what was asked."""
    out = {"request_json": None, "request_bytes": None, "request_hash": None,
           "request_truncated": 0}
    try:
        if body is None:
            return out
        if hasattr(body, "model_dump"):
            body = body.model_dump(exclude_unset=True)
        encoded = _canonical(body)
        out["request_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        out["request_bytes"] = len(encoded)
        cleaned, cut = _cut(body)
        stored = _canonical(cleaned)
        cap = _request_max_bytes()
        if len(stored) > cap:
            stored, cut = stored[:cap], True
        out["request_json"] = stored.decode("utf-8", "ignore")
        out["request_truncated"] = 1 if cut else 0
    except Exception as exc:
        _note(exc)
    return out


def _transport(request) -> str:
    try:
        path = request.url.path
    except Exception:
        return "http"
    if path == "/mcp":
        return "mcp"
    if path == "/a2a":
        return "a2a"
    return "http"


def open_sale(request, *, price_usd, route, product, body) -> dict:
    """Start one purchase's record from the request alone: route, product,
    transport, the request, and who sent it. A plain dict, carried on the
    auth context until the sale is final. Never reads a credential header
    (X-API-Key, Authorization, X-PAYMENT, PAYMENT-SIGNATURE). Never raises."""
    sale = {"route": route, "product": product, "transport": "http", "price_usd": price_usd,
            "request_json": None, "request_bytes": None, "request_hash": None,
            "request_truncated": 0, "client_ip": None, "peer_ip": None, "user_agent": None,
            "referer": None, "contract_hash": None, "idempotency_key": None,
            "call_id": None, "receipt_id": None, "booked": False}
    try:
        sale["transport"] = _transport(request)
        sale.update(body_record(body))
        sale["client_ip"], sale["peer_ip"] = client_ip(request)
        headers = getattr(request, "headers", None) or {}
        sale["user_agent"] = (headers.get("user-agent") or "")[:512] or None
        sale["referer"] = (headers.get("origin") or headers.get("referer") or "")[:512] or None
        sale["contract_hash"] = (headers.get("x-hubvibe-contract") or "").strip()[:100] or None
        sale["idempotency_key"] = (headers.get("idempotency-key") or "")[:200] or None
    except Exception as exc:
        _note(exc)
    return sale


# --- helpers -----------------------------------------------------------------------------

def api_key_hash(key: Optional[str]) -> Optional[str]:
    """A prepaid key's fingerprint. The book never stores the key itself."""
    if not key:
        return None
    return "sha256:" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _address_set(env_name: str) -> set:
    raw = os.environ.get(env_name)
    if raw is None:
        return {SEED_WALLET}
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def internal_payers() -> set:
    """Wallets whose purchases are the owner's own (PURCHASE_INTERNAL_PAYERS,
    comma-separated; default the seed wallet). Read on every call."""
    return _address_set("PURCHASE_INTERNAL_PAYERS")


def alert_ignore_payers() -> set:
    """Wallets whose purchases never alert (PURCHASE_ALERT_IGNORE_PAYERS;
    default the seed wallet)."""
    return _address_set("PURCHASE_ALERT_IGNORE_PAYERS")


def _is_internal(payer: Optional[str]) -> int:
    return 1 if payer and str(payer).lower() in internal_payers() else 0


# DB-IP Lite readers, keyed by file path, re-opened when the file changes.
# iplookup replaces the files with os.replace once a month; the reader of the
# old file is dropped (left to the garbage collector, never closed) so a
# lookup racing the swap cannot land on a closed mmap.
_geo_readers: dict = {}
_geo_lock = threading.Lock()


def _geo_dir() -> str:
    """Where the ip.lookup worker keeps the DB-IP files (iplookup._data_dir)."""
    explicit = os.environ.get("DBIP_DATA_DIR")
    if explicit:
        return explicit
    return "/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) \
        else tempfile.gettempdir()


def _reader(dataset: str):
    path = os.path.join(_geo_dir(), f"dbip-{dataset}-lite.mmdb")
    try:
        stat = os.stat(path)
    except OSError:
        return None
    stamp = (stat.st_ino, stat.st_mtime_ns)
    with _geo_lock:
        cached = _geo_readers.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            reader = maxminddb.open_database(path, maxminddb.MODE_MMAP)
        except Exception:
            return None
        _geo_readers[path] = (stamp, reader)
        return reader


def geo(ip_text: Optional[str]) -> dict:
    """Country, region, city and network of a public address, from the DB-IP
    Lite files the ip.lookup worker keeps on the volume. Local reads only:
    no network call, and {} for a private address, a missing file or any
    error. IP geolocation by DB-IP (https://db-ip.com), CC BY 4.0."""
    try:
        if not ip_text:
            return {}
        ip = ipaddress.ip_address(str(ip_text).strip())
        if not ip.is_global:
            return {}
        address = str(ip)
        out = {}
        city = _reader("city")
        record = (city.get(address) if city is not None else None) or {}

        def name(part):
            names = (part or {}).get("names") or {}
            return names.get("en") or next(iter(names.values()), None)

        subdivisions = record.get("subdivisions") or []
        if record:
            out.update({
                "ip_country": name(record.get("country")),
                "ip_country_code": (record.get("country") or {}).get("iso_code"),
                "ip_region": name(subdivisions[0]) if subdivisions else None,
                "ip_city": name(record.get("city")),
            })
        asn_reader = _reader("asn")
        asn = (asn_reader.get(address) if asn_reader is not None else None) or {}
        if asn:
            out["ip_asn"] = asn.get("autonomous_system_number")
            out["ip_org"] = asn.get("autonomous_system_organization")
        return out
    except Exception:
        return {}


def _field(obj, *names):
    """A value by attribute or by key, whichever the object offers -- the
    payment objects belong to the x402 library and arrive as pydantic
    models, dicts or test doubles (router._payment_facts_of reads the same
    way)."""
    if obj is None:
        return None
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value not in (None, ""):
            return value
    return None


def _caip2(network) -> Optional[str]:
    if not network:
        return None
    network = str(network)
    return _V1_NETWORKS.get(network, network)


def _usd(atomic) -> Optional[float]:
    try:
        return int(atomic) / 10 ** _USDC_DECIMALS
    except (TypeError, ValueError):
        return None


def _svm_facts(transaction_b64: Optional[str]) -> dict:
    """Payer, amount and memo of an x402 Solana payment, read from the
    buyer's partially signed transaction.

    The transaction id cannot be known here: it is the first signature, and
    index 0 is the facilitator's fee-payer signature, added at settle. What
    IS known is the memo -- a random nonce the x402 client puts in every
    payment -- and that is the key a settle that ended "unknown" is later
    matched on (purchase_reconcile)."""
    facts = {}
    if not transaction_b64:
        return facts
    try:
        from solders.transaction import VersionedTransaction

        tx = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64))
        message = tx.message
        keys = [str(key) for key in message.account_keys]
        for ix in message.instructions:
            program = keys[ix.program_id_index] if ix.program_id_index < len(keys) else None
            data = bytes(ix.data)
            if program == _MEMO_PROGRAM:
                facts["memo"] = data.decode("utf-8", "replace")
            elif program in _TOKEN_PROGRAMS and data[:1] == bytes([12]) and len(data) >= 9:
                # TransferChecked: [12, amount u64 LE, decimals]; accounts are
                # source, mint, destination, owner (the buyer).
                accounts = list(ix.accounts)
                if len(accounts) >= 4:
                    facts["payer"] = keys[accounts[3]]
                facts["amount"] = int.from_bytes(data[1:9], "little")
        if "payer" not in facts and len(keys) > 1:
            facts["payer"] = keys[1]
    except Exception:
        pass
    return facts


def _x402_row(pending) -> dict:
    row = {"rail": "x402", "facilitator": _facilitator_host}
    payload = getattr(pending, "payload", None)
    version = _field(payload, "x402_version", "x402Version")
    try:
        row["x402_version"] = int(version) if version is not None else None
    except (TypeError, ValueError):
        row["x402_version"] = None
    inner = _field(payload, "payload") or {}
    requirements = getattr(pending, "requirements", None) or []
    accepted = requirements[0] if requirements else None
    network = _caip2(_field(accepted, "network"))
    row["asset"] = _field(accepted, "asset")
    row["pay_to"] = _field(accepted, "pay_to", "payTo")
    amount = _field(accepted, "amount", "max_amount_required", "maxAmountRequired")
    payer = nonce = memo = None
    if isinstance(inner, dict):
        authorization = inner.get("authorization") or {}
        if isinstance(authorization, dict) and authorization:
            payer = authorization.get("from")
            nonce = authorization.get("nonce")
            amount = authorization.get("value") or amount
        elif inner.get("transaction"):
            svm = _svm_facts(inner.get("transaction"))
            payer = svm.get("payer")
            memo = svm.get("memo")
            amount = svm.get("amount") or amount
    # The facilitator's settle response has the last word where it speaks.
    result = getattr(pending, "settle_result", None)
    tx_hash = _field(result, "transaction")
    network = _caip2(_field(result, "network")) or network
    payer = _field(result, "payer") or payer
    amount = _field(result, "amount") or amount
    row["network"] = network
    is_svm = bool(network and network.startswith("solana:")) or memo is not None
    row["method"] = "exact-svm" if is_svm else "exact-evm"
    if payer and not is_svm:
        payer = str(payer).lower()
    row["payer"] = payer
    try:
        row["amount_atomic"] = int(amount) if amount is not None else None
    except (TypeError, ValueError):
        row["amount_atomic"] = None
    if tx_hash:
        tx_hash = str(tx_hash)
        row["tx_hash"] = tx_hash.lower() if tx_hash.startswith("0x") else tx_hash
    net = network or "unknown"
    if is_svm and memo:
        row["payment_nonce"] = memo
        row["payment_ref"] = f"x402:{net}:memo:{memo}"
    elif payer and nonce:
        row["payment_nonce"] = str(nonce).lower()
        row["payment_ref"] = f"x402:{net}:{payer}:{str(nonce).lower()}"
    elif row.get("tx_hash"):
        row["payment_ref"] = f"x402:{net}:tx:{row['tx_hash']}"
    row["settle_state"] = getattr(pending, "settle_state", None)
    row["settle_error"] = getattr(pending, "settle_error", None)
    return row


def _mpp_row(auth) -> dict:
    row = {"rail": "mpp"}
    facts = None
    credential = getattr(auth, "mpp_credential", None)
    if _mpp_facts is not None and credential:
        try:
            facts = _mpp_facts(credential) or None
        except Exception:
            facts = None
    if not facts:
        # Verified, but the settlement lookup has nothing for it (evicted, or
        # a rail that records no facts). The HMAC-bound challenge fixed the
        # price, and the note says the amount came from there.
        sale = getattr(auth, "sale", None)
        row["received_usd"] = sale.get("price_usd") if isinstance(sale, dict) else None
        row["note"] = "amount from the list price: settlement facts unavailable"
        return row
    row["method"] = facts.get("method")
    row["network"] = facts.get("network")
    row["asset"] = facts.get("asset")
    row["pay_to"] = facts.get("pay_to")
    payer = facts.get("payer")
    row["payer"] = str(payer).lower() if payer and str(payer).startswith("0x") else payer
    row["amount_atomic"] = facts.get("amount_atomic")
    tx = facts.get("tx_hash")
    row["tx_hash"] = tx.lower() if isinstance(tx, str) and tx.startswith("0x") else tx
    if facts.get("amount_cents") is not None:
        row["received_usd"] = int(facts["amount_cents"]) / 100
    else:
        row["received_usd"] = _usd(facts.get("amount_atomic"))
    if tx:
        row["payment_ref"] = f"mpp:{facts.get('method') or 'unknown'}:{row['tx_hash']}"
    return row


def _topup_payer(api_hash: Optional[str]) -> Optional[str]:
    """The wallet that bought a prepaid key, from the key's top-up row."""
    if not api_hash:
        return None
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return None
        try:
            found = conn.execute(
                "SELECT payer FROM purchases WHERE kind='topup' AND api_key_hash=? "
                "AND payer IS NOT NULL ORDER BY id LIMIT 1", (api_hash,)).fetchone()
            return found[0] if found else None
        except Exception as exc:
            _note(exc)
            return None


_X402_OUTCOMES = {"settled": "delivered", "pending": "delivered_settle_pending",
                  "unknown": "delivered_settle_unknown", "refused": "settle_refused"}
_SALE_FIELDS = ("route", "product", "transport", "price_usd", "request_json", "request_bytes",
                "request_hash", "request_truncated", "client_ip", "peer_ip", "user_agent",
                "referer", "contract_hash", "idempotency_key", "call_id", "receipt_id")


def _sale_fields(sale: dict) -> dict:
    return {key: sale.get(key) for key in _SALE_FIELDS}


def _call_row(auth, sale: dict, outcome: Optional[str], note: Optional[str]) -> dict:
    row = {"ts": time.time(), "kind": "sale", "source": "live",
           "node_version": _node_version, **_sale_fields(sale)}
    method = getattr(auth, "payment_method", None)
    pending = getattr(auth, "pending_payment", None)
    notes = []
    if pending is not None:
        row.update(_x402_row(pending))
        if outcome is None:
            # _bill marks every refusal on the handle, so no state after it
            # means the settle answered yes (a stub may set only the result).
            state = row.get("settle_state") or "settled"
            row["settle_state"] = state
            outcome = _X402_OUTCOMES.get(state, "delivered_settle_unknown")
            if state == "settled":
                row["received_usd"] = _usd(row.get("amount_atomic"))
            elif state == "refused":
                row["received_usd"] = 0.0
            else:
                # Money may be in flight: confirmed only by purchase_reconcile.
                row["received_usd"] = None
        else:
            row["received_usd"] = 0.0
    elif method == "mpp":
        row.update(_mpp_row(auth))
        if row.get("note"):
            notes.append(row.pop("note"))
        if outcome == "failed_unbilled":
            # The transfer (or Stripe charge) happened at verification and
            # stays; the claim is released so the SAME credential buys the
            # retry. The money did arrive with this row.
            notes.append("claim released; payer may retry with this payment")
        outcome = outcome or "delivered"
    elif method in ("prepaid", "mpp-topup"):
        row["rail"] = "prepaid"
        key = getattr(auth, "prepaid_key", None) or getattr(auth, "issued_key", None)
        row["api_key_hash"] = api_key_hash(key)
        row["received_usd"] = 0.0
        payer = _topup_payer(row["api_key_hash"])
        if payer:
            row["payer"] = payer
            notes.append("payer via top-up")
        outcome = outcome or "delivered"
    elif method == "stripe":
        row["rail"] = "stripe-subscription"
        row["method"] = "stripe"
        row["payer"] = getattr(auth, "customer_id", None)
        row["received_usd"] = 0.0
        outcome = outcome or "delivered"
    else:
        row["rail"] = method
        row["received_usd"] = 0.0
        outcome = outcome or "delivered"
    if outcome == "failed_unbilled" and method != "mpp":
        row["received_usd"] = 0.0
    if note:
        notes.append(str(note)[:300])
    row["outcome"] = outcome
    row["note"] = "; ".join(notes) or None
    row["internal"] = _is_internal(row.get("payer"))
    row.update(geo(row.get("client_ip")))
    return row


# --- write side: synchronous, off the event loop, never raises ---------------------------

def insert_row(row: dict) -> Optional[int]:
    """Write one row. Keyed on payment_ref (else call_id): a second write of
    the same payment updates the first -- an MPP credential whose first job
    failed and whose retry delivered ends as one delivered row -- and a
    delivered row is never downgraded. Returns the row id, or None when
    nothing was written."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return None
        try:
            data = {key: row.get(key) for key in _COLUMNS if key in row}
            data.setdefault("ts", time.time())
            data["internal"] = data.get("internal") or 0
            data["source"] = data.get("source") or "live"
            data["request_truncated"] = data.get("request_truncated") or 0
            columns = list(data)
            sql = (f"INSERT INTO purchases ({', '.join(columns)}) "
                   f"VALUES ({', '.join('?' for _ in columns)})")
            target = "payment_ref" if data.get("payment_ref") else \
                "call_id" if data.get("call_id") else None
            if target is not None:
                updates = [c for c in columns if c != target]
                sql += (f" ON CONFLICT({target}) WHERE {target} IS NOT NULL DO UPDATE SET "
                        + ", ".join(f"{c}=excluded.{c}" for c in updates)
                        + " WHERE purchases.outcome NOT LIKE 'delivered%'")
            cursor = conn.execute(sql, [data[c] for c in columns])
            conn.commit()
            if cursor.rowcount == 0:
                log.info("purchase not rebooked: that payment is already booked as delivered")
                return None
            if target is None:
                return cursor.lastrowid
            found = conn.execute(f"SELECT id FROM purchases WHERE {target}=?",
                                 (data[target],)).fetchone()
            return found[0] if found else None
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _note(exc)
            return None


def _book(row: dict) -> Optional[int]:
    row_id = insert_row(row)
    if row_id is not None:
        # No body, no address: the log is not the book.
        log.info("purchase booked id=%s product=%s rail=%s outcome=%s",
                 row_id, row.get("product"), row.get("rail"), row.get("outcome"))
        _maybe_alert(row, row_id)
    return row_id


def record_call(auth, *, outcome: Optional[str] = None,
                note: Optional[str] = None) -> Optional[int]:
    """Book the call whose sale was opened on `auth`, once, when its billing
    is final: at the end of _bill (delivered, or a settle state) or when the
    job failed unbilled. Owner-key calls are not sales and are not booked."""
    try:
        sale = getattr(auth, "sale", None)
        if not isinstance(sale, dict) or sale.get("booked"):
            return None
        if getattr(auth, "payment_method", None) == "internal":
            return None
        sale["booked"] = True
        return _book(_call_row(auth, sale, outcome, note))
    except Exception as exc:
        _note(exc)
        return None


def record_mpp_topup(auth) -> Optional[int]:
    """Book an MPP (Stripe) top-up the moment it is charged: the credit was
    bought whether or not the call it arrived with then succeeds."""
    try:
        sale = getattr(auth, "sale", None) or {}
        cents = int(getattr(auth, "topup_cents", 0) or 0)
        ref = getattr(auth, "topup_ref", None)
        owed = getattr(auth, "credit_owed", None)
        row = {"ts": time.time(), "kind": "topup", "source": "live",
               "node_version": _node_version, **_sale_fields(sale),
               "product": "prepaid_credit", "price_usd": None,
               "received_usd": cents / 100, "rail": "mpp-topup", "method": "stripe",
               "network": "stripe", "tx_hash": ref,
               "payment_ref": f"mpp-topup:{ref}" if ref else None,
               "api_key_hash": api_key_hash(getattr(auth, "issued_key", None)),
               "outcome": "credit_owed" if owed else "delivered",
               "call_id": None, "receipt_id": None,
               "note": str(owed)[:300] if owed else None}
        row.update(geo(row.get("client_ip")))
        return _book(row)
    except Exception as exc:
        _note(exc)
        return None


def record_solana_topup(result: dict, sale: dict) -> Optional[int]:
    """Book a Solana hash top-up redeemed for a prepaid key. The request kept
    is the transaction signature only: the challenge token is a bearer
    secret, and the key is kept only as its fingerprint."""
    try:
        payment = result.get("payment") or {}
        amount = int(payment.get("amount_atomic") or 0)
        tx = payment.get("tx_hash")
        row = {"ts": time.time(), "kind": "topup", "source": "live",
               "node_version": _node_version, **_sale_fields(sale or {}),
               "product": "prepaid_credit", "price_usd": None,
               "received_usd": amount / 10 ** _USDC_DECIMALS, "rail": "solana-hash",
               "method": "spl", "network": payment.get("network"),
               "asset": payment.get("asset"), "pay_to": payment.get("pay_to"),
               "payer": payment.get("payer"), "amount_atomic": amount, "tx_hash": tx,
               "payment_ref": f"solana-hash:{tx}" if tx else None,
               "api_key_hash": api_key_hash(result.get("api_key")),
               "outcome": "delivered", "call_id": None, "receipt_id": None,
               "note": f"credit_cents={result.get('credit_cents')} "
                       f"uncredited_atomic={result.get('uncredited_atomic')}"}
        row["internal"] = _is_internal(row.get("payer"))
        row.update(geo(row.get("client_ip")))
        return _book(row)
    except Exception as exc:
        _note(exc)
        return None


# --- alerts ------------------------------------------------------------------------------

_alerts: "queue.Queue" = queue.Queue(maxsize=256)
_alert_thread: Optional[threading.Thread] = None
_alert_thread_lock = threading.Lock()


def _post_alert(url: str, payload: dict) -> None:
    httpx.post(url, json=payload, timeout=3.0).raise_for_status()


def _alert_worker() -> None:
    while True:
        url, payload = _alerts.get()
        try:
            _post_alert(url, payload)
        except Exception as exc:
            # The class only: the URL is a secret (a Slack hook is a bearer
            # credential) and exception text can quote it.
            log.warning("purchase alert not delivered: %s", type(exc).__name__)
        finally:
            _alerts.task_done()


def _ensure_alert_thread() -> None:
    global _alert_thread
    with _alert_thread_lock:
        if _alert_thread is None or not _alert_thread.is_alive():
            _alert_thread = threading.Thread(target=_alert_worker,
                                             name="hubvibe-purchase-alerts", daemon=True)
            _alert_thread.start()


def _short_payer(payer: Optional[str]) -> str:
    if not payer:
        return "unknown payer"
    payer = str(payer)
    return payer if len(payer) <= 14 else f"{payer[:6]}…{payer[-4:]}"


def _iso(ts) -> Optional[str]:
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _maybe_alert(row: dict, row_id: int) -> None:
    """Tell the owner about a delivered outside sale or top-up, when
    PURCHASE_ALERT_WEBHOOK is set. Queued for a background thread: a slow
    webhook never delays the paid response. Never raises."""
    try:
        url = os.environ.get("PURCHASE_ALERT_WEBHOOK")
        if not url:
            return
        if row.get("kind") not in ("sale", "topup"):
            return
        if not str(row.get("outcome") or "").startswith("delivered"):
            return
        if row.get("internal"):
            return
        payer = row.get("payer")
        if payer and str(payer).lower() in alert_ignore_payers():
            return
        amount = row.get("price_usd") if row.get("kind") == "sale" else row.get("received_usd")
        where = ", ".join(str(v) for v in (row.get("ip_country"), row.get("ip_city"),
                                           row.get("ip_org")) if v) or "location unknown"
        label = "sale" if row.get("kind") == "sale" else "top-up"
        price = f"${amount:.2f}" if isinstance(amount, (int, float)) else "$?"
        text = (f"HubVibe {label}: {row.get('product') or 'unknown product'} {price} "
                f"({row.get('rail')}, {row.get('network') or 'n/a'}) from "
                f"{_short_payer(payer)}; {where}")
        payload = {
            "event": "hubvibe.purchase", "text": text, "id": row_id,
            "time_utc": _iso(row.get("ts")), "kind": row.get("kind"),
            "product": row.get("product"), "route": row.get("route"),
            "transport": row.get("transport"), "price_usd": row.get("price_usd"),
            "received_usd": row.get("received_usd"), "rail": row.get("rail"),
            "network": row.get("network"), "payer": payer, "tx_hash": row.get("tx_hash"),
            "receipt_id": row.get("receipt_id"), "outcome": row.get("outcome"),
            "country": row.get("ip_country"), "city": row.get("ip_city"),
            "org": row.get("ip_org"), "node_version": _node_version,
        }
        _ensure_alert_thread()
        _alerts.put_nowait((url, payload))
    except queue.Full:
        log.warning("purchase alert dropped: queue full")
    except Exception as exc:
        log.warning("purchase alert not queued: %s", type(exc).__name__)


def wait_for_alerts(timeout: float = 5.0) -> bool:
    """Tests only: True once every queued alert has been attempted."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _alerts.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return _alerts.unfinished_tasks == 0


# --- read side ---------------------------------------------------------------------------

# Money that arrived with a row: a delivered (or backfilled) sale, any top-up,
# and a failed job whose payment stayed (an MPP transfer released for retry).
_MONEY_IN = ("CASE WHEN (kind='sale' AND (outcome LIKE 'delivered%' OR outcome LIKE 'backfilled%')) "
             "OR kind='topup' OR (outcome='failed_unbilled' AND received_usd > 0) "
             "THEN COALESCE(received_usd, 0) ELSE 0 END")


def _where(since=None, until=None, kind=None, internal=None) -> tuple:
    clauses, params = [], []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(float(since))
    if until is not None:
        clauses.append("ts < ?")
        params.append(float(until))
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if internal is not None:
        clauses.append("internal = ?")
        params.append(1 if internal else 0)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _present(row) -> dict:
    """One book row the way the owner reads it (JSON form). A row written
    before the DB-IP files existed gets its location looked up here."""
    data = dict(row)
    if data.get("client_ip") and not data.get("ip_country"):
        data.update(geo(data["client_ip"]))
    request = data.get("request_json")
    if request:
        try:
            request = json.loads(request)
        except (TypeError, ValueError):
            pass
    return {
        "id": data.get("id"),
        "time_utc": _iso(data.get("ts")),
        "product": data.get("product"), "route": data.get("route"),
        "transport": data.get("transport"), "price_usd": data.get("price_usd"),
        "received_usd": data.get("received_usd"), "amount_atomic": data.get("amount_atomic"),
        "asset": data.get("asset"), "rail": data.get("rail"), "method": data.get("method"),
        "network": data.get("network"), "payer": data.get("payer"),
        "tx_hash": data.get("tx_hash"), "receipt_id": data.get("receipt_id"),
        "kind": data.get("kind"), "internal": data.get("internal"),
        "outcome": data.get("outcome"), "settle_state": data.get("settle_state"),
        "chain_status": data.get("chain_status"),
        "country": data.get("ip_country"), "country_code": data.get("ip_country_code"),
        "city": data.get("ip_city"), "region": data.get("ip_region"),
        "org": data.get("ip_org"), "asn": data.get("ip_asn"),
        "client_ip": data.get("client_ip"), "user_agent": data.get("user_agent"),
        "request_json": request, "request_truncated": data.get("request_truncated"),
        "request_hash": data.get("request_hash"),
        "note": data.get("note"), "source": data.get("source"),
    }


def query(*, since=None, until=None, limit=500, kind=None, internal=None,
          order="desc") -> list:
    """Book rows, newest first by default, in the owner's reading form."""
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return []
        try:
            where, params = _where(since, until, kind, internal)
            direction = "ASC" if str(order).lower() == "asc" else "DESC"
            rows = conn.execute(
                f"SELECT * FROM purchases {where} ORDER BY ts {direction}, id {direction} "
                f"LIMIT ?", (*params, int(limit))).fetchall()
        except Exception as exc:
            _note(exc)
            return []
    return [_present(row) for row in rows]


_CSV_COLUMNS = (
    "id", "date_utc", "time_utc", "product", "route", "transport", "price_usd",
    "received_usd", "amount_atomic", "asset", "rail", "method", "network", "payer",
    "tx_hash", "receipt_id", "kind", "internal", "outcome", "settle_state", "chain_status",
    "country", "country_code", "city", "region", "org", "asn", "client_ip", "user_agent",
    "request_json", "request_truncated", "request_hash", "note", "source",
)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # A spreadsheet runs a cell that starts like a formula, and the
        # User-Agent and the request are the buyer's own text.
        return "'" + value
    return value


def to_csv(rows: list) -> str:
    """The rows query() returned, as CSV for a spreadsheet: one line per
    purchase, date and time split, formula-looking cells neutralized."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(_CSV_COLUMNS)
    for row in rows:
        stamp = row.get("time_utc") or ""
        date_part, _, time_part = stamp.partition("T")
        values = dict(row, date_utc=date_part, time_utc=time_part.rstrip("Z"))
        writer.writerow([_cell(values.get(column)) for column in _CSV_COLUMNS])
    return out.getvalue()


def summary(*, since=None, until=None) -> dict:
    """Money in, split between outside buyers and the owner's own wallets,
    and by day, month, product and rail. Transfers (money that reached the
    wallet without buying anything) are counted apart and never as revenue.
    A prepaid spend carries received_usd 0, so money that came in through a
    top-up is counted exactly once, as the top-up."""
    delivered = "(outcome LIKE 'delivered%' OR outcome LIKE 'backfilled%')"
    with _lock:
        conn = _safe_connect()
        if conn is None:
            return {"available": False}
        try:
            where, params = _where(since, until)
            scoped = where or "WHERE 1=1"

            def totals(internal: int) -> dict:
                row = conn.execute(f"""
                    SELECT
                      SUM(CASE WHEN kind='sale' THEN 1 ELSE 0 END) AS sales,
                      SUM(CASE WHEN kind='sale' AND {delivered} THEN 1 ELSE 0 END) AS delivered_sales,
                      COUNT(DISTINCT payer) AS payers,
                      SUM(CASE WHEN kind='sale' AND {delivered}
                               THEN COALESCE(received_usd,0) ELSE 0 END) AS earned_usd,
                      SUM(CASE WHEN kind='topup' THEN COALESCE(received_usd,0) ELSE 0 END) AS topups_usd,
                      SUM(CASE WHEN kind='sale' AND rail='prepaid' AND outcome LIKE 'delivered%'
                               THEN COALESCE(price_usd,0) ELSE 0 END) AS credit_spent_usd,
                      SUM(CASE WHEN outcome='failed_unbilled' AND received_usd > 0
                               THEN received_usd ELSE 0 END) AS arrived_not_delivered_usd
                    FROM purchases {scoped} AND internal=? AND kind IN ('sale','topup')
                """, (*params, internal)).fetchone()
                out = {key: (row[key] or 0) for key in row.keys()}
                for key in ("earned_usd", "topups_usd", "credit_spent_usd",
                            "arrived_not_delivered_usd"):
                    out[key] = round(float(out[key]), 6)
                out["money_in_usd"] = round(out["earned_usd"] + out["topups_usd"]
                                            + out["arrived_not_delivered_usd"], 6)
                return out

            outside, internal_totals = totals(0), totals(1)
            transfers = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(received_usd),0) FROM purchases {scoped} "
                f"AND kind='transfer'", params).fetchone()
            open_row = conn.execute(f"""
                SELECT
                  SUM(CASE WHEN outcome IN ('delivered_settle_pending','delivered_settle_unknown')
                           AND received_usd IS NULL THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome='settle_refused' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN outcome='failed_unbilled' THEN 1 ELSE 0 END)
                FROM purchases {scoped}""", params).fetchone()

            def period(fmt: str) -> list:
                rows = conn.execute(f"""
                    SELECT strftime('{fmt}', ts, 'unixepoch') AS period,
                      ROUND(SUM(CASE WHEN internal=0 THEN {_MONEY_IN} ELSE 0 END), 6)
                        AS outside_money_in_usd,
                      SUM(CASE WHEN internal=0 AND kind='sale' THEN 1 ELSE 0 END) AS outside_sales,
                      ROUND(SUM(CASE WHEN internal=1 THEN {_MONEY_IN} ELSE 0 END), 6) AS internal_usd,
                      ROUND(SUM(CASE WHEN kind='transfer' THEN COALESCE(received_usd,0) ELSE 0 END), 6)
                        AS transfers_usd
                    FROM purchases {scoped} GROUP BY period ORDER BY period""", params).fetchall()
                return [dict(r) for r in rows]

            per_day, per_month = period("%Y-%m-%d"), period("%Y-%m")
            per_product = [dict(r) for r in conn.execute(f"""
                SELECT COALESCE(product, '(unknown)') AS product,
                  SUM(CASE WHEN kind='sale' THEN 1 ELSE 0 END) AS sales,
                  ROUND(SUM(CASE WHEN kind='sale' AND {delivered}
                                 THEN COALESCE(received_usd,0) ELSE 0 END), 6) AS earned_usd,
                  ROUND(SUM(CASE WHEN kind='sale' AND rail='prepaid' AND outcome LIKE 'delivered%'
                                 THEN COALESCE(price_usd,0) ELSE 0 END), 6) AS credit_spent_usd,
                  COUNT(DISTINCT payer) AS payers
                FROM purchases {scoped} AND internal=0 AND kind IN ('sale','topup')
                GROUP BY product ORDER BY earned_usd DESC, sales DESC""", params).fetchall()]
            per_rail = [dict(r) for r in conn.execute(f"""
                SELECT rail, method, COUNT(*) AS count,
                  ROUND(SUM({_MONEY_IN}), 6) AS money_in_usd
                FROM purchases {scoped} GROUP BY rail, method
                ORDER BY money_in_usd DESC, count DESC""", params).fetchall()]
        except Exception as exc:
            _note(exc)
            return {"available": False, "error": _last_error}
    return {
        "since": _iso(since) if since is not None else None,
        "until": _iso(until) if until is not None else None,
        "totals": {
            "outside": outside,
            "internal": internal_totals,
            "transfers": {"count": transfers[0] or 0,
                          "usd": round(float(transfers[1] or 0), 6)},
            "open": {"settle_pending_or_unknown": open_row[0] or 0,
                     "settle_refused": open_row[1] or 0,
                     "failed_unbilled": open_row[2] or 0},
        },
        "per_day": per_day,
        "per_month": per_month,
        "per_product": per_product,
        "per_rail": per_rail,
    }
