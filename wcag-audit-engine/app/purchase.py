"""Purchase Verification Contract -- what a buyer is about to pay for.

DISCOVER -> VERIFY -> AUTHORIZE -> PAY -> EXECUTE -> PROVE. The receipt
(/work/receipts/{id}) is PROVE; this module is VERIFY: one machine-readable
object per capability, derived from the same catalog rows, schemas and
payment configuration the 402, openapi.json, the MCP tools, the A2A card,
agent.json and ard.json are built from -- never a second copy of any of
them. main.py gathers those pieces and hands them here; this module only
shapes, hashes and compares. No I/O.

`contract_hash` is the receipt's own algorithm (sha256 over canonical JSON,
injected as `hash_fn`) over every field except `issued_at` and the hash
itself, so two nodes serving the same catalog and terms produce the same
hash, and any change to price, schema, recipient, network or asset changes
it. A buyer that verified hash H can send `X-HubVibe-Contract: H` with its
paid request; the node refuses to take payment if it no longer sells H.
"""

from datetime import datetime, timezone
from typing import Any, Callable, Optional

CONTRACT_VERSION = "1"
HEADER = "X-HubVibe-Contract"

# The fields a buyer may state in `expect`, and where each lives in the
# contract. Rail-level fields are checked against the rail whose network
# (or protocol) the buyer named, or against every rail when it named none.
_TOP_LEVEL = {
    "capability_id": ("capability", "id"),
    "id": ("capability", "id"),
    "path": ("capability", "path"),
    "url": ("capability", "url"),
    "mcp_tool": ("capability", "mcp_tool"),
    "price_usd": ("price", "usd"),
    "contract_hash": ("contract_hash",),
    "provider": ("provider", "name"),
    "base_url": ("provider", "base_url"),
    "input_schema_hash": ("input_schema_hash",),
    "output_schema_hash": ("output_schema_hash",),
}
_RAIL_LEVEL = ("network", "asset", "pay_to", "amount_atomic", "protocol", "scheme")


def _same(expected: Any, actual: Any) -> bool:
    """Equality with the two tolerances a buyer legitimately needs: EVM
    addresses compare case-insensitively, and a price may be given as a
    number or its string."""
    if isinstance(expected, str) and isinstance(actual, str):
        if expected.startswith("0x") and actual.startswith("0x"):
            return expected.lower() == actual.lower()
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) < 1e-9
    if isinstance(expected, str) and isinstance(actual, (int, float)):
        try:
            return abs(float(expected) - float(actual)) < 1e-9
        except ValueError:
            return False
    return expected == actual


def _get(obj: dict, path: tuple):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def assemble(fields: dict, hash_fn: Optional[Callable[[Any], Optional[str]]],
             issued_at: Optional[str] = None) -> dict:
    """The contract: `fields` (capability, provider, schemas, price, payment,
    constraints, guarantees, proof, sources) plus the version, the schema
    hashes, the timestamp and the contract hash."""
    contract = {"contract_version": CONTRACT_VERSION, **fields}
    if hash_fn is not None:
        contract["input_schema_hash"] = hash_fn(fields.get("input_schema"))
        contract["output_schema_hash"] = hash_fn(fields.get("output_schema"))
        contract["contract_hash"] = hash_fn(contract)
    else:  # a node without the worker network has no canonical hash function
        contract["input_schema_hash"] = contract["output_schema_hash"] = None
        contract["contract_hash"] = None
    contract["issued_at"] = issued_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return contract


def rail_for(contract: dict, network: Optional[str] = None,
             protocol: Optional[str] = None) -> list:
    """The rails in the contract a buyer's expectation applies to."""
    rails = (contract.get("payment") or {}).get("rails") or []
    if network:
        rails = [r for r in rails if r.get("network") == network]
    if protocol:
        rails = [r for r in rails if r.get("protocol") == protocol]
    return rails


def verify(contract: dict, expect: Optional[dict], input_body: Any,
           input_check: Optional[Callable[[Optional[dict], Any], Optional[str]]]) -> dict:
    """Compare what the buyer expects with what the node sells, and the
    buyer's input with the input contract. Deterministic; the caller gates
    payment on `verified`."""
    mismatches = []
    checks = []
    expect = expect if isinstance(expect, dict) else {}
    for key, value in expect.items():
        if key in _TOP_LEVEL:
            actual = _get(contract, _TOP_LEVEL[key])
            ok = _same(value, actual)
            checks.append({"field": key, "expected": value, "actual": actual, "ok": ok})
            if not ok:
                mismatches.append({"field": key, "expected": value, "actual": actual})
        elif key in _RAIL_LEVEL:
            continue  # handled below, together
        else:
            mismatches.append({"field": key, "expected": value, "actual": None,
                               "detail": "not a field of this contract"})
    rail_expect = {k: v for k, v in expect.items() if k in _RAIL_LEVEL}
    if rail_expect:
        candidates = rail_for(contract, rail_expect.get("network"), rail_expect.get("protocol"))
        matched = None
        for rail in candidates:
            if all(_same(v, rail.get(k)) for k, v in rail_expect.items()):
                matched = rail
                break
        if matched is None:
            offered = [{k: r.get(k) for k in ("protocol", "network", "asset", "pay_to", "amount_atomic")}
                       for r in (candidates or rail_for(contract))]
            mismatches.append({"field": "rail", "expected": rail_expect, "actual": offered,
                               "detail": "no rail in the contract matches every expected value"})
        checks.append({"field": "rail", "expected": rail_expect,
                       "actual": matched and {k: matched.get(k) for k in _RAIL_LEVEL}, "ok": matched is not None})

    input_result = {"checked": False, "valid": None, "problem": None}
    if input_body is not None:
        if input_check is None:
            input_result["problem"] = "this node cannot validate inputs (no validator)"
        else:
            problem = input_check(contract.get("input_schema"), input_body)
            input_result = {"checked": True, "valid": problem is None, "problem": problem}
            if problem is not None:
                mismatches.append({"field": "input", "expected": "a body matching input_schema",
                                   "actual": problem})
    return {
        "capability": (contract.get("capability") or {}).get("id"),
        "contract_hash": contract.get("contract_hash"),
        "verified": not mismatches,
        "checks": checks,
        "mismatches": mismatches,
        "input": input_result,
    }


def challenge_problem(contract: dict, accepts: list, rail_network_prefix: Optional[str] = None,
                      resource_url: Optional[str] = None) -> Optional[str]:
    """Does a live 402 (its `accepts` entries, v1 or v2 shape) offer exactly
    the terms the contract states? None when every accepted requirement the
    buyer could pay -- optionally only those on `rail_network_prefix`
    ("eip155:" / "solana:") -- has a rail in the contract with the same
    network, asset, recipient and amount, and the resource named is the
    capability's URL. Otherwise one line naming the first difference. Pure,
    so a buyer-side client can run the same comparison."""
    rails = (contract.get("payment") or {}).get("rails") or []
    url = (contract.get("capability") or {}).get("url")
    if resource_url and url and resource_url != url:
        return f"resource {resource_url!r} is not the capability's URL {url!r}"
    considered = 0
    for entry in accepts or []:
        if not isinstance(entry, dict):
            return "malformed accepts entry"
        network = entry.get("network")
        if rail_network_prefix and not str(network).startswith(rail_network_prefix):
            continue
        considered += 1
        amount = entry.get("amount") or entry.get("maxAmountRequired")
        pay_to = entry.get("payTo") or entry.get("pay_to")
        asset = entry.get("asset")
        match = next((r for r in rails if r.get("protocol") == "x402" and r.get("network") == network
                      and _same(asset, r.get("asset")) and _same(pay_to, r.get("pay_to"))
                      and str(amount) == str(r.get("amount_atomic"))), None)
        if match is None:
            return (f"challenge offers network={network} asset={asset} payTo={pay_to} "
                    f"amount={amount}, which the contract does not")
        entry_resource = entry.get("resource")
        if isinstance(entry_resource, str) and url and entry_resource and entry_resource != url:
            return f"challenge resource {entry_resource!r} is not the capability's URL {url!r}"
    if considered == 0:
        return "the challenge offers no rail this buyer can pay"
    return None
