"""Google Routes API (computeRoutes), for traffic-aware travel time and
distance between two places.

One POST per call to `directions/v2:computeRoutes`, authenticated with the
package's one Google credential (google_auth) and the quota project the
box bills to. Enabled on the project and verified from Cloud Shell on
2026-09-27: a DRIVE request with TRAFFIC_AWARE and a future departureTime
answered `duration` (with traffic), `staticDuration` (without),
`distanceMeters` and the localized texts.

NEVER A CACHED ANSWER: each call is a fresh request; the skill stamps
`checked_at`. NEVER A GUESS: a field Google leaves out is null in the
value, never filled in. The one documented exception is `distanceMeters`,
which proto3 JSON omits when it is zero (origin and destination resolve
to the same point) -- absent means 0 in that encoding, and is read as 0.

Rules of the API this adapter enforces or maps:
  * routingPreference (TRAFFIC_AWARE / TRAFFIC_AWARE_OPTIMAL /
    TRAFFIC_UNAWARE) is only accepted with DRIVE or TWO_WHEELER; the skill
    sends none for WALK, BICYCLE and TRANSIT.
  * departureTime must be RFC 3339 and in the future; omitting it means now.
  * origin/destination: an address string, or "lat,lng" mapped to latLng.
  * 400 INVALID_ARGUMENT and 404 NOT_FOUND are the caller's input
    (InvalidRequest); 401/403 mean this deployment cannot use the API
    (ProviderUnavailable); 429 and 5xx are transient.

COST: Google bills Routes per request (Compute Routes Essentials/Advanced
SKUs, tiered); the per-call price is not measured here, so every result
records cost_measured=False with usage "requests=1".
"""

import math
import os
import re
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

_TIMEOUT = float(os.environ.get("WORKER_ROUTES_TIMEOUT_SECONDS", "30"))
ROUTES_URL = os.environ.get("WORKER_GOOGLE_ROUTES_URL",
                            "https://routes.googleapis.com/directions/v2:computeRoutes")
# The project the request is billed to when the credential itself carries
# none (user-flavoured ADC). The box's project, verified 2026-09-27.
QUOTA_PROJECT = os.environ.get("WORKER_ROUTES_QUOTA_PROJECT", "resolver-time")
# Everything the skill reads, nothing more: Google prices by field mask.
FIELD_MASK = ("routes.duration,routes.staticDuration,routes.distanceMeters,"
              "routes.travelAdvisory,routes.localizedValues,routes.description,routes.warnings")

TRAVEL_MODES = ("DRIVE", "TWO_WHEELER", "WALK", "BICYCLE", "TRANSIT")
# Modes the API accepts a routingPreference for.
TRAFFIC_MODES = ("DRIVE", "TWO_WHEELER")
ROUTING_PREFERENCES = ("TRAFFIC_AWARE", "TRAFFIC_AWARE_OPTIMAL", "TRAFFIC_UNAWARE")

_LATLNG = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*,\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
_DURATION = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*s\s*$")


def parse_duration(text) -> Optional[int]:
    """'1152s' -> 1152; '1152.5s' -> 1153 (half up). None for anything else:
    a missing or malformed duration is reported as unknown, never as 0."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return int(math.floor(float(text) + 0.5))
    if not isinstance(text, str):
        return None
    match = _DURATION.match(text)
    if not match:
        return None
    return int(math.floor(float(match.group(1)) + 0.5))


def waypoint(text: str, field: str = "place") -> dict:
    """A Routes API Waypoint from what the caller gave: "lat,lng" (two
    decimals) becomes a latLng location; anything else is an address for
    Google to geocode. Coordinates out of range are the caller's error."""
    match = _LATLNG.match(text)
    if not match:
        return {"address": text.strip()}
    lat, lng = float(match.group(1)), float(match.group(2))
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lng <= 180.0:
        raise runtime.InvalidRequest(
            f"`{field}` looks like coordinates but is out of range: latitude -90..90, longitude -180..180.")
    return {"location": {"latLng": {"latitude": lat, "longitude": lng}}}


async def _post_json(url: str, headers: dict, body: dict) -> tuple:
    """(status, decoded JSON or None). Network failures are transient."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise runtime.TransientProviderError(f"Google Routes timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise runtime.TransientProviderError(f"Google Routes unreachable: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = None
    return response.status_code, data


def _error_of(data, status: int) -> tuple:
    """(google status string, message) from a Google error body."""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "", f"HTTP {status}"
    return str(error.get("status") or ""), str(error.get("message") or f"HTTP {status}")


def _text(value) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _localized(values, key: str) -> Optional[str]:
    if not isinstance(values, dict):
        return None
    entry = values.get(key)
    return _text(entry.get("text")) if isinstance(entry, dict) else None


def _route_value(route: dict) -> dict:
    duration = parse_duration(route.get("duration"))
    if duration is None:
        raise runtime.InvalidProviderResponse("Google Routes answered a route without a duration.")
    distance = route.get("distanceMeters")
    if distance is None:
        # proto3 JSON omits an int32 that is 0; the field is in the mask.
        distance = 0
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        raise runtime.InvalidProviderResponse("Google Routes answered a route with a non-numeric distance.")
    localized = route.get("localizedValues")
    advisory = route.get("travelAdvisory")
    warnings = [w.strip() for w in (route.get("warnings") or []) if isinstance(w, str) and w.strip()] \
        if isinstance(route.get("warnings"), list) else []
    return {
        "distance_meters": int(distance),
        "duration_seconds": duration,
        "static_duration_seconds": parse_duration(route.get("staticDuration")),
        "duration_text": _localized(localized, "duration"),
        "static_duration_text": _localized(localized, "staticDuration"),
        "distance_text": _localized(localized, "distance"),
        "description": _text(route.get("description")),
        "warnings": warnings,
        "advisory": advisory if isinstance(advisory, dict) else None,
    }


class _GoogleRoutes:
    id = "google-routes"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def compute(self, origin: str, destination: str, travel_mode: str,
                      departure_time: Optional[str] = None,
                      routing_preference: Optional[str] = None) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        if travel_mode not in TRAVEL_MODES:
            raise runtime.InvalidRequest(f"`travel_mode` must be one of {list(TRAVEL_MODES)}.")
        if routing_preference is not None:
            if routing_preference not in ROUTING_PREFERENCES:
                raise runtime.InvalidRequest(f"routing preference must be one of {list(ROUTING_PREFERENCES)}.")
            if travel_mode not in TRAFFIC_MODES:
                raise runtime.InvalidRequest(
                    f"a traffic routing preference is only accepted with {list(TRAFFIC_MODES)}.")

        body = {
            "origin": waypoint(origin, "origin"),
            "destination": waypoint(destination, "destination"),
            "travelMode": travel_mode,
            "computeAlternativeRoutes": False,
            "units": "METRIC",
        }
        if routing_preference is not None:
            body["routingPreference"] = routing_preference
        if departure_time is not None:
            body["departureTime"] = departure_time

        headers = await google_auth.headers()
        headers["X-Goog-User-Project"] = google_auth.project() or QUOTA_PROJECT
        headers["Content-Type"] = "application/json"
        headers["X-Goog-FieldMask"] = FIELD_MASK

        status, data = await _post_json(ROUTES_URL, headers, body)
        if status in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Google Routes returned {status}", reason="provider_overloaded" if status == 429 else None)
        if status >= 500:
            raise runtime.TransientProviderError(f"Google Routes returned {status}")
        if status >= 400:
            google_status, message = _error_of(data, status)
            if status in (401, 403) or google_status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
                raise runtime.ProviderUnavailable(f"Google Routes refused this deployment ({status}): {message}")
            if google_status in ("INVALID_ARGUMENT", "NOT_FOUND") or status == 404:
                raise runtime.InvalidRequest(f"Google Routes rejected the request: {message}")
            raise runtime.PermanentProviderError(f"Google Routes returned {status}: {message}")
        if not isinstance(data, dict):
            raise runtime.InvalidProviderResponse("Google Routes did not return JSON.")

        routes = data.get("routes")
        if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
            # An empty body is how computeRoutes says "no route": the places
            # resolved but nothing connects them for this travel mode.
            raise runtime.InvalidRequest(
                f"Google Routes found no {travel_mode} route between these places.")
        value = _route_value(routes[0])
        value["travel_mode"] = travel_mode
        value["routing_preference"] = routing_preference
        value["departure_time"] = departure_time
        return runtime.ProviderResult(value=value, cost_micros=None, cost_measured=False,
                                      usage="requests=1")


PROVIDERS = [_GoogleRoutes()]
