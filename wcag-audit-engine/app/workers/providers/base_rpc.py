"""Base L2 chain reads over public JSON-RPC.

Connected, not rebuilt: the chain is the chain. This is an RPC client with a
User-Agent and a fallback endpoint.

THE USER-AGENT IS LOAD-BEARING. Measured 2026-09-14: the public Base RPCs
began answering 403 to requests without one. The repo's paying scripts hit
this first; the same rule applies to every outbound chain read here.

This is READ-ONLY and deliberately so. It is a data worker; it never signs,
never sends value, and shares nothing with the payment path. HubVibe's money
moves only through the existing x402/Coinbase code, which this cannot reach.
"""

import os
from typing import Optional

import httpx

from .. import runtime

USER_AGENT = os.environ.get("WORKER_HTTP_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")

_ENDPOINTS = [e.strip() for e in os.environ.get(
    "WORKER_BASE_RPC_URLS",
    "https://mainnet.base.org,https://base-rpc.publicnode.com").split(",") if e.strip()]

_TIMEOUT = float(os.environ.get("WORKER_RPC_TIMEOUT_SECONDS", "20"))

# Read-only methods only. An allowlist rather than a denylist: a new RPC
# method should have to be considered before a customer can reach it.
ALLOWED_METHODS = {
    "eth_blockNumber", "eth_getBalance", "eth_getTransactionByHash",
    "eth_getTransactionReceipt", "eth_getCode", "eth_getStorageAt",
    "eth_getTransactionCount", "eth_call", "eth_getBlockByNumber",
    "eth_getBlockByHash", "eth_gasPrice", "eth_chainId", "eth_getLogs",
    "eth_estimateGas", "eth_feeHistory",
}


class _BaseRpc:
    def __init__(self, url: str):
        self.url = url
        host = url.split("//")[-1].split("/")[0]
        self.id = f"base-rpc:{host}"

    def available(self) -> bool:
        return True  # keyless public endpoint

    def unavailable_reason(self) -> str:
        return ""

    async def rpc(self, method: str, params: Optional[list] = None) -> runtime.ProviderResult:
        if method not in ALLOWED_METHODS:
            raise runtime.InvalidRequest(
                f"`{method}` is not one of this worker's read-only methods: "
                f"{', '.join(sorted(ALLOWED_METHODS))}")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    self.url, json=payload,
                    headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{self.id} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{self.id} unreachable: {exc}") from exc

        if response.status_code in (403, 429):
            raise runtime.TransientProviderError(
                f"{self.id} refused the read ({response.status_code}); "
                "public RPCs rate-limit by address.", reason="provider_rate_limited")
        if response.status_code >= 400:
            raise runtime.TransientProviderError(f"{self.id} returned {response.status_code}")

        data = response.json()
        if "error" in data:
            message = (data.get("error") or {}).get("message", "unknown")
            # An RPC-level error is the node telling us the CALL is wrong;
            # another endpoint will say the same thing.
            raise runtime.PermanentProviderError(f"RPC error: {message}")
        if "result" not in data:
            raise runtime.InvalidProviderResponse("RPC answered without a result.")

        # Free public endpoint: cost is genuinely zero, and measured as such.
        return runtime.ProviderResult(
            value={"method": method, "result": data["result"], "endpoint": self.url},
            cost_micros=0, cost_measured=True, usage=method)


PROVIDERS = [_BaseRpc(u) for u in _ENDPOINTS]
