"""Check the purchase book against every payment the chains show.

Run inside the container, next to the book:

    docker compose exec hubvibe python -m app.purchase_reconcile            # report only
    docker compose exec hubvibe python -m app.purchase_reconcile --backfill # and fill the book

For each wallet this node is paid into, every incoming USDC payment is
listed from a public source and marked:

  recorded     the book has it (by transaction, or an open x402 row whose
               payer + nonce / memo the chain now resolves)
  ledger_only  only an older ledger has it (worker_calls, the MPP spent-hash
               ledger, the Solana top-up ledger)
  missing      no HubVibe record at all

Sources:
  Base (x402 and MPP evm)  Blockscout's token-transfer listing for the payTo
  Solana (x402, top-ups)   the public RPC: the payTo's USDC token accounts
  Tempo (MPP tempo)        eth_getLogs on the Tempo RPC, 100,000 blocks a query

--backfill writes what the book lacks: worker_calls rows first (they know
the product and the receipt, but only a hash of the request), then Solana
top-ups, then every remaining chain payment with what the chain knows --
payer, transaction, amount, time -- and nothing else. Request text and
client details are never invented for a past sale.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import httpx

try:
    from . import purchase_book, solana_hash_verifier
except ImportError:  # loaded by file path (scripts/reconcile-purchases.py, tests)
    def _sibling(name: str):
        unique = f"wcag_audit_engine_{name}"
        if unique in sys.modules:
            return sys.modules[unique]
        spec = importlib.util.spec_from_file_location(unique, Path(__file__).with_name(f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[unique] = module
        spec.loader.exec_module(module)
        return module

    purchase_book = _sibling("purchase_book")  # type: ignore
    solana_hash_verifier = _sibling("solana_hash_verifier")  # type: ignore

BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SOLANA_USDC = solana_hash_verifier.USDC_MINT
# transferWithAuthorization (both signature forms) and receiveWithAuthorization:
# the EIP-3009 calls an x402 facilitator submits.
X402_SELECTORS = {"0xe3ee160e", "0xcf092995", "0xef55bec6"}
X402_NAMES = {"transferwithauthorization", "receivewithauthorization"}
# USDC's AuthorizationUsed(authorizer, nonce): an x402 settle inside a batch.
AUTH_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
AA_SELECTORS = {"0x1fad948c", "0x765e827f"}
TRANSFER_SELECTORS = {"0xa9059cbb", "0x23b872dd"}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TEMPO_MAX_BLOCKS = 100_000
SALE_CLASSES = {"x402", "x402_batched", "x402_svm", "mpp_evm", "mpp_tempo"}


@dataclass
class ChainPayment:
    network: str
    tx_hash: str
    log_index: Optional[int]
    ts: float
    payer: str
    pay_to: str
    asset: str
    amount_atomic: int
    method: str = ""
    classification: str = ""
    nonce: Optional[str] = None
    memo: Optional[str] = None
    fee_payer: Optional[str] = None
    status: str = "missing"
    matched_id: Optional[int] = None


# --- network ---------------------------------------------------------------------

def http_get_json(url: str, params: Optional[dict] = None, *, attempts: int = 8) -> dict:
    """GET JSON with retries: Blockscout answers 500 now and then."""
    last = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), 8))
        try:
            response = httpx.get(url, params=params, timeout=30,
                                 headers={"User-Agent": "HubVibe-reconcile/1.0 (+https://hubvibe-io.com)",
                                          "Accept": "application/json"})
            if response.status_code == 429 or response.status_code >= 500:
                last = RuntimeError(f"HTTP {response.status_code} from {url}")
                continue
            response.raise_for_status()
            time.sleep(0.25)
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"{url}: {last}")


def jsonrpc(url: str, *, attempts: int = 5) -> Callable:
    def call(method: str, params: list):
        last = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2 ** (attempt - 1), 8))
            try:
                response = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                      timeout=30, headers={"User-Agent": "HubVibe-reconcile/1.0"})
                if response.status_code == 429 or response.status_code >= 500:
                    last = RuntimeError(f"HTTP {response.status_code}")
                    continue
                body = response.json()
                if body.get("error"):
                    raise RuntimeError(f"{method}: {body['error']}")
                return body.get("result")
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"{method} on {url}: {last}")
    return call


def _iso_ts(text: str) -> float:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


# --- Base ------------------------------------------------------------------------

def base_transfers(pay_to: str, *, get_json=http_get_json, blockscout: str = "https://base.blockscout.com",
                   since_ts: Optional[float] = None) -> Iterator[ChainPayment]:
    """Every USDC transfer into `pay_to`, newest first. Blockscout's
    Etherscan-style listing first (one request per 1,000 transfers), its
    paged v2 listing if that fails: both answer HTTP 500 now and then."""
    try:
        found = list(_base_transfers_flat(pay_to, get_json=get_json, blockscout=blockscout, since_ts=since_ts))
    except RuntimeError:
        found = None
    if found is not None:
        yield from found
        return
    yield from _base_transfers_v2(pay_to, get_json=get_json, blockscout=blockscout, since_ts=since_ts)


def _base_transfers_flat(pay_to: str, *, get_json, blockscout: str,
                         since_ts: Optional[float]) -> Iterator[ChainPayment]:
    target = pay_to.lower()
    page_number, per_page = 1, 1000
    rows: list = []
    while True:
        body = get_json(f"{blockscout}/api", {
            "module": "account", "action": "tokentx", "address": pay_to, "contractaddress": BASE_USDC,
            "sort": "desc", "page": page_number, "offset": per_page})
        page = body.get("result") if isinstance(body, dict) else None
        if not isinstance(page, list):
            if str(body.get("message", "")).lower().startswith("no transactions"):
                break
            raise RuntimeError(f"tokentx: {str(body)[:200]}")
        rows.extend(page)
        if len(page) < per_page:
            break
        page_number += 1
    seen_in_tx: dict = {}
    for item in rows:
        if (item.get("to") or "").lower() != target:
            continue
        ts = float(item["timeStamp"])
        if since_ts and ts < since_ts:
            continue
        tx = item["hash"].lower()
        seen_in_tx[tx] = seen_in_tx.get(tx, -1) + 1   # no log index in this listing: number repeats
        yield ChainPayment(
            network=BASE, tx_hash=tx, log_index=seen_in_tx[tx], ts=ts, payer=(item.get("from") or "").lower(),
            pay_to=target, asset=BASE_USDC, amount_atomic=int(item.get("value") or 0),
            method=(item.get("input") or "")[:10].lower())


def _base_transfers_v2(pay_to: str, *, get_json, blockscout: str,
                       since_ts: Optional[float]) -> Iterator[ChainPayment]:
    url = f"{blockscout}/api/v2/addresses/{pay_to}/token-transfers"
    params = {"type": "ERC-20", "filter": "to", "token": BASE_USDC}
    while True:
        page = get_json(url, params)
        for item in page.get("items") or []:
            ts = _iso_ts(item["timestamp"])
            if since_ts and ts < since_ts:
                return
            yield ChainPayment(
                network=BASE, tx_hash=item["transaction_hash"].lower(), log_index=item.get("log_index"),
                ts=ts, payer=((item.get("from") or {}).get("hash") or "").lower(),
                pay_to=((item.get("to") or {}).get("hash") or pay_to).lower(), asset=BASE_USDC,
                amount_atomic=int((item.get("total") or {}).get("value") or 0),
                method=str(item.get("method") or "").lower())
        nxt = page.get("next_page_params")
        if not nxt:
            return
        params = {"type": "ERC-20", "filter": "to", "token": BASE_USDC, **nxt}


def _pad(address: str) -> str:
    return "0x" + "0" * 24 + address.lower()[2:]


def base_payment_from_receipt(tx_hash: str, pay_to: str, *, rpc) -> list:
    """USDC transfers into `pay_to` in one Base transaction, straight from its
    receipt: the authority when the explorer's listing lacks a transfer
    (seen 2026-09-29: four settled payments missing from Blockscout)."""
    receipt = rpc("eth_getTransactionReceipt", [tx_hash]) or {}
    if not receipt or receipt.get("status") not in ("0x1", 1):
        return []
    block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False]) or {}
    ts = float(int(block.get("timestamp") or "0x0", 16))
    tx = rpc("eth_getTransactionByHash", [tx_hash]) or {}
    found = []
    for log_item in receipt.get("logs") or []:
        topics = log_item.get("topics") or []
        if ((log_item.get("address") or "").lower() == BASE_USDC and len(topics) > 2
                and topics[0].lower() == TRANSFER_TOPIC and topics[2].lower() == _pad(pay_to)):
            found.append(ChainPayment(
                network=BASE, tx_hash=tx_hash.lower(), log_index=int(log_item.get("logIndex") or "0x0", 16),
                ts=ts, payer=("0x" + topics[1][-40:]).lower(), pay_to=pay_to.lower(), asset=BASE_USDC,
                amount_atomic=int(log_item.get("data") or "0x0", 16),
                method=(tx.get("input") or "")[:10].lower()))
    return found


def base_log_scan(pay_to: str, *, rpc, since_ts: float, chunk: int = 1000) -> Iterator[ChainPayment]:
    """Every USDC Transfer log into `pay_to` since `since_ts`, from the public
    RPC in `chunk`-block windows (mainnet.base.org refuses much wider ones)."""
    latest = int(rpc("eth_blockNumber", []), 16)
    block_ts: dict = {}

    def ts_of(number: int) -> int:
        if number not in block_ts:
            block_ts[number] = int(rpc("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)
        return block_ts[number]

    low, high = 0, latest
    while low < high:
        middle = (low + high) // 2
        if ts_of(middle) < since_ts:
            low = middle + 1
        else:
            high = middle
    for start in range(low, latest + 1, chunk):
        end = min(start + chunk - 1, latest)
        logs = rpc("eth_getLogs", [{"address": BASE_USDC, "fromBlock": hex(start), "toBlock": hex(end),
                                    "topics": [TRANSFER_TOPIC, None, _pad(pay_to)]}]) or []
        for log_item in logs:
            tx = rpc("eth_getTransactionByHash", [log_item["transactionHash"]]) or {}
            yield ChainPayment(
                network=BASE, tx_hash=log_item["transactionHash"].lower(),
                log_index=int(log_item.get("logIndex") or "0x0", 16),
                ts=float(ts_of(int(log_item["blockNumber"], 16))),
                payer=("0x" + log_item["topics"][1][-40:]).lower(), pay_to=pay_to.lower(), asset=BASE_USDC,
                amount_atomic=int(log_item.get("data") or "0x0", 16),
                method=(tx.get("input") or "")[:10].lower())


def receipt_logs(rpc) -> Callable:
    """The logs of a Base transaction, from its receipt on the public RPC."""
    def logs_of(tx_hash: str) -> list:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash]) or {}
        return [{"address": lg.get("address"), "topics": lg.get("topics") or []} for lg in receipt.get("logs") or []]
    return logs_of


def classify_base(p: ChainPayment, *, logs_of: Callable, bridges: frozenset = frozenset(),
                  mpp_hashes: Optional[dict] = None, want_nonce: bool = False) -> None:
    method = p.method
    if (mpp_hashes or {}).get(p.tx_hash) == "evm":
        p.classification = "mpp_evm"
        return
    if method in X402_SELECTORS or method in X402_NAMES:
        p.classification = "x402"
        if not want_nonce:
            return
    elif p.payer in bridges:
        p.classification = "bridge"
        return
    try:
        logs = logs_of(p.tx_hash) or []
    except RuntimeError:
        logs = []
    for log_item in logs:
        topics = log_item.get("topics") or []
        address = log_item.get("address")
        address = (address.get("hash") if isinstance(address, dict) else address) or ""
        if (address.lower() == BASE_USDC and len(topics) > 2
                and str(topics[0]).lower() == AUTH_USED and ("0x" + str(topics[1])[-40:]).lower() == p.payer):
            p.nonce = str(topics[2]).lower()
            if p.classification != "x402":
                p.classification = "x402_batched"
            return
    if p.classification:
        return
    if method in AA_SELECTORS or method == "handleops":
        p.classification = "aa_userop"
    elif method in TRANSFER_SELECTORS or method in ("transfer", "transferfrom"):
        p.classification = "plain_transfer"
    else:
        p.classification = f"other:{method or 'unknown'}"


# --- Solana ----------------------------------------------------------------------

def solana_transfers(owner: str, *, rpc, since_ts: Optional[float] = None) -> Iterator[ChainPayment]:
    accounts = rpc("getTokenAccountsByOwner", [owner, {"mint": SOLANA_USDC}, {"encoding": "jsonParsed"}]) or {}
    for account in accounts.get("value") or []:
        before = None
        while True:
            options = {"limit": 1000}
            if before:
                options["before"] = before
            signatures = rpc("getSignaturesForAddress", [account["pubkey"], options]) or []
            for entry in signatures:
                if entry.get("err") is not None:
                    continue
                block_time = entry.get("blockTime") or 0
                if since_ts and block_time and block_time < since_ts:
                    return
                tx = rpc("getTransaction", [entry["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                                                 "maxSupportedTransactionVersion": 0}])
                if not tx:
                    continue
                moved = solana_hash_verifier.usdc_movement(tx, owner)
                if moved.get("received_atomic", 0) <= 0:
                    continue
                memo = entry.get("memo")
                if isinstance(memo, str) and memo.startswith("[") and "] " in memo:
                    memo = memo.split("] ", 1)[1]
                keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
                first = keys[0] if keys else None
                fee_payer = first.get("pubkey") if isinstance(first, dict) else first
                yield ChainPayment(
                    network=SOLANA, tx_hash=entry["signature"], log_index=None, ts=float(block_time),
                    payer=moved.get("payer") or "", pay_to=owner, asset=SOLANA_USDC,
                    amount_atomic=int(moved["received_atomic"]), memo=memo or None, fee_payer=fee_payer)
            if len(signatures) < 1000:
                break
            before = signatures[-1]["signature"]


def classify_solana(p: ChainPayment, *, topups: dict, open_memos: dict) -> None:
    if p.tx_hash in topups:
        p.classification = "solana_hash"
    elif p.memo and (p.memo in open_memos or (p.fee_payer and p.fee_payer != p.payer)):
        p.classification = "x402_svm"
    else:
        p.classification = "direct"


# --- Tempo -----------------------------------------------------------------------

def tempo_transfers(recipient: str, token: str, *, rpc, since_ts: float,
                    network: str = "eip155:4217") -> Iterator[ChainPayment]:
    latest = int(rpc("eth_blockNumber", []), 16)
    block_ts: dict = {}

    def ts_of(number: int) -> int:
        if number not in block_ts:
            block_ts[number] = int(rpc("eth_getBlockByNumber", [hex(number), False])["timestamp"], 16)
        return block_ts[number]

    low, high = 0, latest
    while low < high:
        middle = (low + high) // 2
        if ts_of(middle) < since_ts:
            low = middle + 1
        else:
            high = middle
    to_topic = "0x" + "0" * 24 + recipient.lower()[2:]
    for start in range(low, latest + 1, TEMPO_MAX_BLOCKS):
        end = min(start + TEMPO_MAX_BLOCKS - 1, latest)
        logs = rpc("eth_getLogs", [{"address": token, "fromBlock": hex(start), "toBlock": hex(end),
                                    "topics": [TRANSFER_TOPIC, None, to_topic]}]) or []
        for log_item in logs:
            number = int(log_item["blockNumber"], 16)
            yield ChainPayment(
                network=network, tx_hash=log_item["transactionHash"].lower(),
                log_index=int(log_item.get("logIndex") or "0x0", 16), ts=float(ts_of(number)),
                payer=("0x" + log_item["topics"][1][-40:]).lower(), pay_to=recipient.lower(),
                asset=token.lower(), amount_atomic=int(log_item.get("data") or "0x0", 16))


# --- what HubVibe already has ------------------------------------------------------

@dataclass
class Known:
    book_tx: dict = field(default_factory=dict)        # tx_hash -> purchases.id
    ledger_tx: set = field(default_factory=set)        # worker_calls.tx_hash
    mpp: dict = field(default_factory=dict)            # tx_hash -> method
    topups: dict = field(default_factory=dict)         # signature -> solana_hash_claims row
    open_nonces: dict = field(default_factory=dict)    # (payer, nonce) -> purchases.id
    open_memos: dict = field(default_factory=dict)     # memo -> purchases.id


def _rows(db_path: Optional[str], sql: str) -> list:
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _norm_tx(tx: Optional[str]) -> Optional[str]:
    if not tx:
        return None
    return tx.lower() if tx.startswith("0x") else tx


def load_known(conn: sqlite3.Connection, *, worker_db: str, mpp_db: str, solana_db: str) -> Known:
    known = Known()
    for row in conn.execute("SELECT id, tx_hash, payer, payment_nonce, method, outcome FROM purchases"):
        tx = _norm_tx(row["tx_hash"])
        if tx:
            known.book_tx.setdefault(tx, row["id"])
        elif row["outcome"] in ("delivered_settle_unknown", "delivered_settle_pending") and row["payment_nonce"]:
            if row["method"] == "exact-svm":
                known.open_memos[row["payment_nonce"]] = row["id"]
            else:
                known.open_nonces[((row["payer"] or "").lower(), row["payment_nonce"].lower())] = row["id"]
    known.ledger_tx = {_norm_tx(r["tx_hash"]) for r in _rows(
        worker_db, "SELECT tx_hash FROM worker_calls WHERE tx_hash IS NOT NULL")}
    known.mpp = {_norm_tx(r["tx_hash"]): r["method"] for r in _rows(
        mpp_db, "SELECT tx_hash, method FROM mpp_hash_claims")}
    known.topups = {r["tx_hash"]: r for r in _rows(solana_db, "SELECT * FROM solana_hash_claims")}
    return known


# --- backfill ----------------------------------------------------------------------

def backfill_from_ledgers(*, worker_db: str, solana_db: str, dry_run: bool = False) -> dict:
    """worker_calls rows and Solana top-ups the book does not have yet."""
    added = {"worker_calls": 0, "solana_topups": 0, "skipped_no_rail": 0}
    for r in _rows(worker_db, "SELECT * FROM worker_calls WHERE tx_hash IS NOT NULL OR settled=1"):
        if not r.get("rail"):
            added["skipped_no_rail"] += 1   # prepaid or owner key: cannot be told apart here
            continue
        payer = r.get("payer")
        row = {
            "ts": r.get("finished_at") or r.get("started_at"), "kind": "sale",
            "outcome": "backfilled_from_ledger", "source": "ledger", "route": r.get("path"),
            "product": r.get("worker"), "price_usd": (r.get("price_micros") or 0) / 1e6, "rail": r.get("rail"),
            "network": purchase_book._caip2(r.get("network")), "asset": r.get("asset"),
            "pay_to": r.get("pay_to"), "amount_atomic": r.get("amount_atomic"),
            "payer": payer.lower() if payer and payer.startswith("0x") else payer,
            "tx_hash": _norm_tx(r.get("tx_hash")), "call_id": r.get("call_id"),
            "receipt_id": "rcpt_" + r["call_id"] if r.get("call_id") else None,
            "idempotency_key": r.get("idempotency_key"), "request_hash": r.get("request_hash"),
            "node_version": r.get("node_version"), "internal": purchase_book._is_internal(payer),
            "note": "request text and client not recorded before 1.30.0",
        }
        if not dry_run and purchase_book.insert_row(row) is not None:
            added["worker_calls"] += 1
    for r in _rows(solana_db, "SELECT * FROM solana_hash_claims WHERE status='issued'"):
        row = {
            "ts": r.get("issued_at") or r.get("claimed_at"), "kind": "topup",
            "outcome": "backfilled_from_ledger", "source": "ledger", "route": "/pay/solana/redeem",
            "product": "prepaid_credit", "rail": "solana-hash", "network": r.get("network"),
            "asset": r.get("asset"), "pay_to": r.get("pay_to"), "amount_atomic": r.get("amount_atomic"),
            "received_usd": (r.get("amount_atomic") or 0) / 1e6, "payer": r.get("payer"),
            "tx_hash": r.get("tx_hash"), "payment_ref": f"solana-hash:{r.get('tx_hash')}",
            "api_key_hash": purchase_book.api_key_hash(r.get("api_key")),
            "internal": purchase_book._is_internal(r.get("payer")),
            "note": f"credit_cents={r.get('credit_cents')}; request and client not recorded before 1.30.0",
        }
        if not dry_run and purchase_book.insert_row(row) is not None:
            added["solana_topups"] += 1
    return added


def _chain_row(p: ChainPayment) -> dict:
    kind = ("sale" if p.classification in SALE_CLASSES
            else "topup" if p.classification == "solana_hash" else "transfer")
    note = ("MPP claim: product unknown (audit or worker)" if p.classification in ("mpp_evm", "mpp_tempo")
            else "route unknown: chain shows payment only" if kind == "sale" else "not a sale")
    return {
        "ts": p.ts, "kind": kind, "outcome": "backfilled_from_chain", "source": "chain", "rail": "chain",
        "method": p.classification, "network": p.network, "asset": p.asset, "pay_to": p.pay_to,
        "amount_atomic": p.amount_atomic, "received_usd": p.amount_atomic / 1e6, "payer": p.payer or None,
        "tx_hash": p.tx_hash, "payment_nonce": p.nonce or p.memo,
        "payment_ref": f"chain:{p.network}:{p.tx_hash}:{p.log_index if p.log_index is not None else 0}",
        "internal": purchase_book._is_internal(p.payer), "chain_status": "seen", "reconciled_at": time.time(),
        "note": note,
    }


# --- reconcile ----------------------------------------------------------------------

def reconcile(conn: sqlite3.Connection, payments: list, known: Known, *, backfill: bool,
              dry_run: bool) -> dict:
    now = time.time()
    resolved = 0
    for p in payments:
        tx = _norm_tx(p.tx_hash)
        if tx in known.book_tx:
            p.status, p.matched_id = "recorded", known.book_tx[tx]
        elif p.nonce and (p.payer, p.nonce) in known.open_nonces:
            p.status, p.matched_id = "recorded", known.open_nonces[(p.payer, p.nonce)]
            resolved += 1
        elif p.memo and p.memo in known.open_memos:
            p.status, p.matched_id = "recorded", known.open_memos[p.memo]
            resolved += 1
        elif tx in known.ledger_tx or tx in known.mpp or p.tx_hash in known.topups:
            p.status = "ledger_only"
        else:
            p.status = "missing"
        if dry_run or p.status != "recorded":
            continue
        conn.execute(
            "UPDATE purchases SET chain_status='seen', reconciled_at=?, "
            "received_usd=COALESCE(received_usd, ?), tx_hash=COALESCE(tx_hash, ?), "
            "note=CASE WHEN outcome IN ('delivered_settle_unknown','delivered_settle_pending') "
            "THEN TRIM(COALESCE(note,'') || ' settled on chain (found by reconcile)') ELSE note END, "
            "outcome=CASE WHEN outcome IN ('delivered_settle_unknown','delivered_settle_pending') "
            "THEN 'delivered' ELSE outcome END "
            "WHERE id=?", (now, p.amount_atomic / 1e6, tx, p.matched_id))
    if not dry_run:
        conn.commit()
    added = 0
    if backfill and not dry_run:
        for p in payments:
            if p.status in ("missing", "ledger_only") and purchase_book.insert_row(_chain_row(p)) is not None:
                added += 1
    not_seen = []
    networks = {p.network for p in payments}
    seen = {_norm_tx(p.tx_hash) for p in payments}
    for row in conn.execute("SELECT id, tx_hash, network, ts FROM purchases WHERE tx_hash IS NOT NULL "
                            "AND kind IN ('sale','topup')").fetchall():
        if row["network"] in networks and _norm_tx(row["tx_hash"]) not in seen and now - (row["ts"] or now) > 3600:
            not_seen.append(row["id"])
            if not dry_run:
                conn.execute("UPDATE purchases SET chain_status='not_seen', reconciled_at=? WHERE id=?",
                             (now, row["id"]))
    if not dry_run:
        conn.commit()
    counts: dict = {}
    for p in payments:
        counts.setdefault(p.network, {"recorded": 0, "ledger_only": 0, "missing": 0})[p.status] += 1
    return {"counts": counts, "backfilled_from_chain": added, "unknown_settles_resolved": resolved,
            "book_rows_not_seen_on_chain": not_seen,
            "missing": [asdict(p) for p in payments if p.status == "missing"]}


# --- CLI -------------------------------------------------------------------------------

def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check the purchase book against the chains.")
    parser.add_argument("--rails", default="base,solana,tempo")
    parser.add_argument("--since", default=None, help="ISO date or epoch; default all history")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--scan", action="store_true",
                        help="also read every USDC Transfer log from the Base RPC (slow; the explorer can miss some)")
    args = parser.parse_args(argv)
    rails = {r.strip() for r in args.rails.split(",") if r.strip()}
    since = None
    if args.since:
        try:
            since = float(args.since)
        except ValueError:
            since = _iso_ts(args.since if "T" in args.since else args.since + "T00:00:00+00:00")

    worker_db = _env("WORKER_LEDGER_PATH", default="/data/hubvibe-workers.db")
    mpp_db = _env("MPP_HASH_LEDGER_PATH", default="/data/hubvibe-mpp-hashes.db")
    solana_db = _env("SOLANA_HASH_LEDGER_PATH", default="/data/hubvibe-solana-hashes.db")
    report: dict = {"rails": {}, "errors": {}}
    try:
        conn = purchase_book.connect()
        if conn is None:
            raise RuntimeError("the purchase book could not be opened")
        if args.backfill:
            report["backfilled_from_ledgers"] = backfill_from_ledgers(
                worker_db=worker_db, solana_db=solana_db, dry_run=args.dry_run)
        known = load_known(conn, worker_db=worker_db, mpp_db=mpp_db, solana_db=solana_db)
        payments: list = []
        if "base" in rails:
            blockscout = _env("PURCHASE_BLOCKSCOUT_URL", default="https://base.blockscout.com").rstrip("/")
            bridges = frozenset(a.strip().lower() for a in _env(
                "PURCHASE_BRIDGE_ADDRESSES", default="0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae").split(",")
                if a.strip())
            wanted = {payer for payer, _ in known.open_nonces}
            base_rpc = jsonrpc(_env("MPP_EVM_RPC_URL", default="https://mainnet.base.org"))
            logs_of = receipt_logs(base_rpc)
            addresses = {}
            for address in (_env("X402_PAY_TO_ADDRESS"), _env("MPP_EVM_RECIPIENT_ADDRESS")):
                if address:
                    addresses.setdefault(address.lower(), address)
            for pay_to in sorted(addresses.values()):
                try:
                    found = list(base_transfers(pay_to, blockscout=blockscout, since_ts=since))
                    listed = {p.tx_hash for p in found}
                    if args.scan:
                        for p in base_log_scan(pay_to, rpc=base_rpc,
                                               since_ts=since or _iso_ts("2026-09-01T00:00:00+00:00")):
                            if p.tx_hash not in listed:
                                found.append(p)
                                listed.add(p.tx_hash)
                    # Payments HubVibe has on record that the listing lacks: ask the chain.
                    ours = {tx for tx in set(known.book_tx) | known.ledger_tx | set(known.mpp)
                            if tx and tx.startswith("0x") and tx not in listed}
                    for tx in sorted(ours):
                        for p in base_payment_from_receipt(tx, pay_to, rpc=base_rpc):
                            if not since or p.ts >= since:
                                found.append(p)
                                listed.add(p.tx_hash)
                    for p in found:
                        classify_base(p, logs_of=logs_of, bridges=bridges, mpp_hashes=known.mpp,
                                      want_nonce=p.payer in wanted)
                    payments.extend(found)
                    report["rails"][f"base:{pay_to}"] = len(found)
                except RuntimeError as exc:
                    report["errors"][f"base:{pay_to}"] = str(exc)[:300]
        if "solana" in rails:
            owner = _env("SOLANA_HASH_PAY_TO", "X402_SOLANA_PAY_TO_ADDRESS")
            if owner:
                try:
                    rpc = jsonrpc(_env("SOLANA_RPC_URL", default="https://api.mainnet-beta.solana.com"))
                    found = list(solana_transfers(owner, rpc=rpc, since_ts=since))
                    for p in found:
                        classify_solana(p, topups=known.topups, open_memos=known.open_memos)
                    payments.extend(found)
                    report["rails"][f"solana:{owner}"] = len(found)
                except RuntimeError as exc:
                    report["errors"][f"solana:{owner}"] = str(exc)[:300]
        if "tempo" in rails:
            recipient = _env("MPP_TEMPO_RECIPIENT_ADDRESS")
            if recipient:
                token = _env("MPP_TEMPO_TOKEN_ADDRESS", default="0x20C000000000000000000000b9537d11c60E8b50")
                start = since or _iso_ts("2026-09-27T00:00:00+00:00")
                try:
                    rpc = jsonrpc(_env("MPP_TEMPO_RPC_URL", default="https://rpc.tempo.xyz"))
                    found = list(tempo_transfers(recipient, token, rpc=rpc, since_ts=start))
                    for p in found:
                        p.classification = "mpp_tempo" if known.mpp.get(p.tx_hash) == "tempo" else "tempo_transfer"
                    payments.extend(found)
                    report["rails"][f"tempo:{recipient}"] = len(found)
                except RuntimeError as exc:
                    report["errors"][f"tempo:{recipient}"] = (
                        f"{str(exc)[:240]} -- the Tempo rail could not be reconciled; tempo claims on record: "
                        f"{sum(1 for m in known.mpp.values() if m == 'tempo')}")
        report.update(reconcile(conn, payments, known, backfill=args.backfill, dry_run=args.dry_run))
    except Exception as exc:
        print(f"reconcile failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print("Purchase book vs chain" + (" (dry run)" if args.dry_run else ""))
    for name, count in report["rails"].items():
        print(f"  {name}: {count} incoming payments")
    for network, c in report["counts"].items():
        print(f"  {network}: recorded {c['recorded']}, ledger only {c['ledger_only']}, missing {c['missing']}")
    for name, error in report["errors"].items():
        print(f"  ERROR {name}: {error}")
    if report.get("backfilled_from_ledgers"):
        print(f"  backfilled from ledgers: {report['backfilled_from_ledgers']}")
    print(f"  backfilled from chain: {report['backfilled_from_chain']}; unknown settles resolved: "
          f"{report['unknown_settles_resolved']}; book rows not seen on chain: "
          f"{len(report['book_rows_not_seen_on_chain'])}")
    for m in report["missing"]:
        when = datetime.datetime.fromtimestamp(m["ts"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"  MISSING {when} ${m['amount_atomic'] / 1e6:.2f} {m['classification']} "
              f"from {m['payer']} tx {m['tx_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
