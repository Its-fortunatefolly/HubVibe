"""Duffel, keyed: live flight offers and place lookup for the travel bees.

Duffel aggregates airlines (NDC and GDS) behind one API. One access token
(DUFFEL_ACCESS_TOKEN) covers search; searching is free inside Duffel's
1500:1 search-to-book ratio, booking is billed per order and is not sold
here. Stays (hotels) is a separate Duffel product that has to be enabled per
account by Duffel; this adapter reports that state honestly instead of
guessing.

Test tokens (`duffel_test_...`) return PRACTICE offers -- Duffel Airways and
imitations of real carriers -- so the adapter is fail-closed on them: it is
only `available()` with a live token, or with WORKER_DUFFEL_ALLOW_TEST=1 set
on purpose for local proof. Nobody pays the public node for practice data.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_DUFFEL_TIMEOUT_SECONDS", "45"))
BASE = os.environ.get("WORKER_DUFFEL_API_BASE", "https://api.duffel.com")
USER_AGENT = os.environ.get("WORKER_DUFFEL_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
VERSION = "v2"
# Duffel waits for airlines up to this long before answering with what it has.
SUPPLIER_TIMEOUT_MS = int(os.environ.get("WORKER_DUFFEL_SUPPLIER_TIMEOUT_MS", "20000"))


def _token() -> str:
    return os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip()


def live_mode() -> bool:
    return _token().startswith("duffel_live_")


def test_allowed() -> bool:
    return os.environ.get("WORKER_DUFFEL_ALLOW_TEST", "").strip() == "1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Duffel-Version": VERSION,
            "Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT}


def _message(data, status: int) -> str:
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            return str(first.get("message") or first.get("title") or first)[:300]
    if isinstance(data, str) and data.strip():
        return data.strip()[:300]
    return f"HTTP {status}"


async def _request(method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None,
                   what: str = "request") -> dict:
    url = f"{BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, url, params=params, json=body, headers=_headers())
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"Duffel {what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"Duffel {what} unreachable: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = response.text
    status = response.status_code
    if status in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"Duffel {what} returned {status}: {_message(data, status)}",
                                             reason="provider_overloaded" if status == 429 else "provider_transient")
    if status in (401, 403):
        raise runtime.ProviderUnavailable(f"Duffel refused this deployment's token for {what} ({status}): "
                                          f"{_message(data, status)}")
    if status in (400, 422):
        raise runtime.InvalidRequest(f"Duffel rejected the {what}: {_message(data, status)}")
    if status >= 400:
        raise runtime.PermanentProviderError(f"Duffel {what} returned {status}: {_message(data, status)}")
    if not isinstance(data, dict) or "data" not in data:
        raise runtime.InvalidProviderResponse(f"Duffel {what} answered without a data object.")
    return data


class _Duffel:
    id = "duffel-air"

    def available(self) -> bool:
        return bool(_token()) and (live_mode() or test_allowed())

    def unavailable_reason(self) -> str:
        if not _token():
            return "DUFFEL_ACCESS_TOKEN is not set"
        if not live_mode():
            return ("DUFFEL_ACCESS_TOKEN is a test token: Duffel test mode returns practice offers, "
                    "which are never sold; install a live token")
        return ""

    async def places(self, query: str) -> runtime.ProviderResult:
        """Airports and cities matching a name in any language Duffel knows."""
        data = await _request("GET", "/places/suggestions", params={"query": query}, what="place lookup")
        places = [p for p in data["data"] if isinstance(p, dict)]
        return runtime.ProviderResult(value=places, cost_micros=0, cost_measured=True,
                                      usage=f"places={len(places)}")

    async def offers(self, slices: list, passengers: list, cabin_class: Optional[str] = None,
                     max_connections: Optional[int] = None) -> runtime.ProviderResult:
        """One offer request, answered with its offers in the same call."""
        body = {"slices": slices, "passengers": passengers}
        if cabin_class:
            body["cabin_class"] = cabin_class
        if max_connections is not None:
            body["max_connections"] = max_connections
        data = await _request("POST", "/air/offer_requests",
                              params={"return_offers": "true", "supplier_timeout": str(SUPPLIER_TIMEOUT_MS)},
                              body={"data": body}, what="offer request")
        request = data["data"]
        offers = [o for o in (request.get("offers") or []) if isinstance(o, dict)]
        return runtime.ProviderResult(
            value={"id": request.get("id"), "live_mode": bool(request.get("live_mode")), "offers": offers,
                   "slices": request.get("slices") or [], "passengers": request.get("passengers") or []},
            cost_micros=0, cost_measured=True,
            usage=f"offers={len(offers)} live_mode={request.get('live_mode')}")


PROVIDERS = [_Duffel()]
