"""Inspects a caller-specified MCP server: one `initialize`, one `tools/list`.

READ-ONLY AND MINIMAL ON PURPOSE. This never calls a tool the target
exposes, never sends anything but the two handshake methods every compliant
MCP server must answer, and reuses the SAME target guard the extraction
worker uses (`web.target_problem`) rather than a second copy of the SSRF
rule -- a customer paying to "audit someone else's endpoint" is exactly the
shape of request an unguarded fetcher would turn into a cloud-metadata read.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT
from . import web

_TIMEOUT = float(os.environ.get("WORKER_MCP_PROBE_TIMEOUT_SECONDS", "20"))
_PROTOCOL_VERSION = "2025-06-18"


class _McpProbe:
    id = "mcp-probe"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    async def _rpc(self, url: str, method: str, rpc_id: int,
                   params: Optional[dict] = None) -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                return await client.post(url, json=body, headers={
                    "User-Agent": USER_AGENT, "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"{url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"{url} unreachable: {exc}") from exc

    def _parse_json_response(self, response: httpx.Response) -> Optional[dict]:
        """A spec-compliant MCP server over Streamable HTTP may answer with
        `text/event-stream`; either way the payload is one JSON-RPC object,
        so this takes the last `data:` line when SSE-framed."""
        try:
            if "text/event-stream" in (response.headers.get("content-type") or ""):
                last_data = None
                for line in response.text.splitlines():
                    if line.startswith("data:"):
                        last_data = line[len("data:"):].strip()
                if last_data is None:
                    return None
                import json
                return json.loads(last_data)
            return response.json()
        except Exception:
            return None

    async def inspect(self, url: str) -> runtime.ProviderResult:
        problem = web.target_problem(url)
        if problem:
            raise runtime.InvalidRequest(f"`url` {problem}.")

        init_response = await self._rpc(url, "initialize", 1, {
            "protocolVersion": _PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "hubvibe-mcp-inspector", "version": "1.0"}})

        requires_auth = init_response.status_code in (401, 403)
        protocol_version = server_name = server_version = None
        if init_response.status_code == 200:
            init_data = self._parse_json_response(init_response) or {}
            result = init_data.get("result") or {}
            protocol_version = result.get("protocolVersion")
            server_info = result.get("serverInfo") or {}
            server_name = server_info.get("name")
            server_version = server_info.get("version")

        tools: list = []
        tools_error = None
        if not requires_auth and init_response.status_code == 200:
            tools_response = await self._rpc(url, "tools/list", 2)
            if tools_response.status_code == 200:
                tools_data = self._parse_json_response(tools_response) or {}
                tools = (tools_data.get("result") or {}).get("tools") or []
                if not isinstance(tools, list):
                    tools, tools_error = [], "tools/list result was not a list"
            elif tools_response.status_code in (401, 403):
                requires_auth = True
            else:
                tools_error = f"tools/list returned HTTP {tools_response.status_code}"
        elif requires_auth:
            tools_error = "initialize required authentication; tools/list was not attempted"

        not_readonly = [t.get("name") for t in tools
                        if isinstance(t, dict)
                        and not (t.get("annotations") or {}).get("readOnlyHint")]

        return runtime.ProviderResult(
            value={
                "url": url,
                "reachable": init_response.status_code < 500,
                "requires_auth": requires_auth,
                "initialize_status": init_response.status_code,
                "protocol_version": protocol_version,
                "server_name": server_name,
                "server_version": server_version,
                "tool_count": len(tools),
                "tools": [{"name": t.get("name"), "description": t.get("description"),
                          "annotations": t.get("annotations")}
                         for t in tools if isinstance(t, dict)],
                "tools_without_readonly_annotation": [n for n in not_readonly if n],
                "tools_error": tools_error,
            },
            cost_micros=0, cost_measured=True, usage=f"tools={len(tools)}")


PROVIDERS = [_McpProbe()]
