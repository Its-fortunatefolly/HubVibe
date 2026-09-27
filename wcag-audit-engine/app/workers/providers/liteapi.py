"""LiteAPI (Nuitee Connect), keyed: hotel content and live room rates for
the travel bees. 2M+ properties without supplier contracts.

One key (LITEAPI_KEY). A sandbox key mirrors production content and rates
but its bookings are not real, and LiteAPI stamps every rates response with
`sandbox: true`; the adapter is therefore fail-closed on sandbox keys
(unavailable, never advertised) unless WORKER_LITEAPI_ALLOW_SANDBOX=1 is set
on purpose for local proof. A production key is unlocked in the LiteAPI
dashboard by adding a card; search and rates are free, only bookings bill.
Verified 2026-09-27: /data/hotels (countryCode+cityName, hotelName, aiSearch
free text, latitude/longitude) and POST /hotels/rates.
"""

import os
from typing import Optional

import httpx

from .. import runtime

_TIMEOUT = float(os.environ.get("WORKER_LITEAPI_TIMEOUT_SECONDS", "45"))
BASE = os.environ.get("WORKER_LITEAPI_BASE", "https://api.liteapi.travel/v3.0")
USER_AGENT = os.environ.get("WORKER_LITEAPI_USER_AGENT", "HubVibe-worker/1.0 (+https://hubvibe-io.com)")
MAX_HOTELS = 20


def _key() -> str:
    return os.environ.get("LITEAPI_KEY", "").strip()


def sandbox_key() -> bool:
    """LiteAPI sandbox keys start with `sand_`; production keys with `prod_`."""
    return _key().startswith("sand_")


def sandbox_allowed() -> bool:
    return os.environ.get("WORKER_LITEAPI_ALLOW_SANDBOX", "").strip() == "1"


def _message(data, status: int) -> str:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("description") or err.get("message") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        if isinstance(data.get("message"), str):
            return data["message"][:300]
    if isinstance(data, str) and data.strip():
        return data.strip()[:300]
    return f"HTTP {status}"


async def _request(method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None,
                   what: str = "request") -> dict:
    url = f"{BASE}{path}"
    headers = {"X-API-Key": _key(), "Accept": "application/json", "Content-Type": "application/json",
               "User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(method, url, params=params, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"LiteAPI {what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"LiteAPI {what} unreachable: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = response.text
    status = response.status_code
    if status in (429, 500, 502, 503, 504):
        raise runtime.TransientProviderError(f"LiteAPI {what} returned {status}: {_message(data, status)}",
                                             reason="provider_overloaded" if status == 429 else "provider_transient")
    if status in (401, 403):
        raise runtime.ProviderUnavailable(f"LiteAPI refused this deployment's key for {what} ({status}): {_message(data, status)}")
    if status in (400, 404, 422):
        raise runtime.InvalidRequest(f"LiteAPI rejected the {what}: {_message(data, status)}")
    if status >= 400:
        raise runtime.PermanentProviderError(f"LiteAPI {what} returned {status}: {_message(data, status)}")
    if not isinstance(data, dict):
        raise runtime.InvalidProviderResponse(f"LiteAPI {what} answered without a JSON object.")
    return data


class _LiteApi:
    id = "liteapi-hotels"

    def available(self) -> bool:
        return bool(_key()) and (not sandbox_key() or sandbox_allowed())

    def unavailable_reason(self) -> str:
        if not _key():
            return "LITEAPI_KEY is not set"
        if sandbox_key():
            return ("LITEAPI_KEY is a sandbox key: sandbox results are never sold; add a card in the LiteAPI "
                    "dashboard for the production key")
        return ""

    async def hotels(self, *, country_code: Optional[str] = None, city_name: Optional[str] = None,
                     hotel_name: Optional[str] = None, ai_search: Optional[str] = None,
                     latitude: Optional[float] = None, longitude: Optional[float] = None,
                     distance_m: Optional[int] = None, limit: int = 10) -> runtime.ProviderResult:
        params = {"limit": str(min(max(limit, 1), MAX_HOTELS))}
        if country_code:
            params["countryCode"] = country_code
        if city_name:
            params["cityName"] = city_name
        if hotel_name:
            params["hotelName"] = hotel_name
        if ai_search:
            params["aiSearch"] = ai_search
        if latitude is not None and longitude is not None:
            params["latitude"] = str(latitude)
            params["longitude"] = str(longitude)
            params["distance"] = str(distance_m or 5000)
        data = await _request("GET", "/data/hotels", params=params, what="hotel search")
        hotels = [h for h in (data.get("data") or []) if isinstance(h, dict)]
        return runtime.ProviderResult(value={"hotels": hotels, "total": data.get("total")},
                                      cost_micros=0, cost_measured=True, usage=f"hotels={len(hotels)} total={data.get('total')}")

    async def rates(self, hotel_ids: list, checkin: str, checkout: str, occupancies: list,
                    currency: str = "USD", guest_nationality: str = "US") -> runtime.ProviderResult:
        body = {"hotelIds": hotel_ids, "occupancies": occupancies, "currency": currency,
                "guestNationality": guest_nationality, "checkin": checkin, "checkout": checkout}
        data = await _request("POST", "/hotels/rates", body=body, what="rates request")
        rows = [r for r in (data.get("data") or []) if isinstance(r, dict)]
        return runtime.ProviderResult(value={"rates": rows, "sandbox": bool(data.get("sandbox"))},
                                      cost_micros=0, cost_measured=True,
                                      usage=f"hotels_with_rates={len(rows)} sandbox={data.get('sandbox')}")


PROVIDERS = [_LiteApi()]
