"""Google Maps Platform, via the Google-managed Maps Grounding Lite MCP
server -- not rebuilt, connected. Google runs `mapstools.googleapis.com`;
this is an MCP client for the three tools it exposes (search_places,
compute_routes, lookup_weather), the same "one call, one JSON-RPC method"
shape mcp_probe.py already speaks to inspect a THIRD PARTY's server.

KEYED SEPARATELY FROM EVERYTHING ELSE GOOGLE HERE. Maps Grounding Lite is
billed and enabled per its own API (Maps Grounding Lite API), not part of
the Vertex/BigQuery `cloud-platform` ADC scope this package's other Google
providers share -- so this needs its own key
(MAPS_GROUNDING_LITE_API_KEY), set once by the operator after enabling that
API, and stays unavailable (never advertised) until it is.
"""

import json
import os
from typing import Optional

import httpx

from .. import runtime
from .base_rpc import USER_AGENT

_ENDPOINT = os.environ.get("WORKER_MAPS_GROUNDING_URL", "https://mapstools.googleapis.com/mcp")
_TIMEOUT = float(os.environ.get("WORKER_MAPS_TIMEOUT_SECONDS", "30"))


def _api_key() -> str:
    return os.environ.get("MAPS_GROUNDING_LITE_API_KEY", "").strip()


class _MapsGroundingLite:
    id = "maps-grounding-lite"

    def available(self) -> bool:
        return bool(_api_key())

    def unavailable_reason(self) -> str:
        return ("MAPS_GROUNDING_LITE_API_KEY is not set (enable the Maps Grounding Lite "
                "API and create a key restricted to it)")

    async def _call_tool(self, tool: str, arguments: dict) -> dict:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    _ENDPOINT, json=body,
                    headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                            "X-Goog-Api-Key": _api_key()})
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Maps Grounding Lite timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Maps Grounding Lite unreachable: {exc}") from exc

        if response.status_code in (401, 403):
            raise runtime.ProviderUnavailable(
                "Maps Grounding Lite refused the key (check it is restricted to the "
                "Maps Grounding Lite API and the API is enabled).")
        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Maps Grounding Lite returned {response.status_code}",
                reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Maps Grounding Lite rejected the call ({response.status_code}): "
                f"{response.text[:200]}")

        data = self._parse(response)
        if data is None:
            raise runtime.InvalidProviderResponse("Maps Grounding Lite answered without JSON.")
        if "error" in data and data["error"] is not None:
            message = (data["error"] or {}).get("message", "unknown error")
            raise runtime.PermanentProviderError(f"Maps Grounding Lite: {message}")

        result = data.get("result") or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in (result.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"]
            raise runtime.PermanentProviderError(
                "Maps Grounding Lite: " + (" ".join(texts) or "tool call failed"))
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        # Fall back to the text content blocks every MCP tool result carries.
        texts = [c.get("text", "") for c in (result.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return {"text": "\n".join(texts)}
        raise runtime.InvalidProviderResponse("Maps Grounding Lite returned an empty result.")

    def _parse(self, response: httpx.Response) -> Optional[dict]:
        try:
            if "text/event-stream" in (response.headers.get("content-type") or ""):
                last = None
                for line in response.text.splitlines():
                    if line.startswith("data:"):
                        last = line[len("data:"):].strip()
                return json.loads(last) if last else None
            return response.json()
        except Exception:
            return None

    async def search_places(self, text_query: str, region_code: Optional[str] = None
                            ) -> runtime.ProviderResult:
        args = {"text_query": text_query}
        if region_code:
            args["region_code"] = region_code
        result = await self._call_tool("search_places", args)
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"query={text_query[:40]}")

    async def compute_routes(self, origin: str, destination: str,
                             travel_mode: str = "DRIVE") -> runtime.ProviderResult:
        result = await self._call_tool("compute_routes", {
            "origin": origin, "destination": destination, "travel_mode": travel_mode})
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"{origin[:20]}->{destination[:20]}")

    async def lookup_weather(self, location: str) -> runtime.ProviderResult:
        result = await self._call_tool("lookup_weather", {"location": {"address": location}})
        return runtime.ProviderResult(value=result, cost_micros=0, cost_measured=False,
                                      usage=f"location={location[:40]}")


PROVIDERS = [_MapsGroundingLite()]
