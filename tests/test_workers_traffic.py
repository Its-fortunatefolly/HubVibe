"""traffic.route -- traffic-aware travel time and distance (Google Routes API).

Pinned: the computeRoutes response VERIFIED from Cloud Shell on 2026-09-27
parsed into the value the skill sells (duration with traffic, static
duration, distance, localized texts); the request body and field mask sent;
"lat,lng" mapped to a latLng waypoint; Google's error statuses mapped to
the caller's error / unavailable / transient; duration parsing; input
refused before the gate (bad mode, bad traffic, past or zoneless departure,
missing fields, coordinates out of range); no routingPreference for WALK,
BICYCLE and TRANSIT; the delay computed only when both durations exist;
warnings passed through; checked_at and as_of stamped per call; and the
result matching the delivery contract this test carries for contract.py.
"""

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"

# Captured 2026-09-27 from routes.googleapis.com (DRIVE, TRAFFIC_AWARE, future departureTime).
ROUTES_RESPONSE = {"routes": [{"distanceMeters": 17011, "duration": "1152s", "staticDuration": "1281s",
                               "travelAdvisory": {},
                               "localizedValues": {"distance": {"text": "17.0 km"}, "duration": {"text": "19 mins"},
                                                   "staticDuration": {"text": "21 mins"}}}]}
INVALID_ARGUMENT = {"error": {"code": 400, "message": "Departure time must be in the future.",
                              "status": "INVALID_ARGUMENT"}}
PERMISSION_DENIED = {"error": {"code": 403, "message": "Routes API has not been used in project resolver-time before or it is disabled.",
                               "status": "PERMISSION_DENIED"}}
NOT_FOUND = {"error": {"code": 404, "message": "Address not found: nowhere at all", "status": "NOT_FOUND"}}


def _load_workers():
    cached = sys.modules.get("wcag_audit_engine_workers")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "wcag_audit_engine_workers", PKG / "__init__.py",
        submodule_search_locations=[str(PKG)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["wcag_audit_engine_workers"] = module
    spec.loader.exec_module(module)
    return module


W = _load_workers()


def _load_member(dotted: str, path: Path):
    """Load a provider or skill module by file path under the package's own
    name, so its relative imports resolve, whether or not the package's
    __init__ already imports it."""
    name = W.__name__ + dotted
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GR = _load_member(".providers.google_routes", PKG / "providers" / "google_routes.py")
setattr(W.providers, "google_routes", GR)
S = _load_member(".skills.traffic", PKG / "skills" / "traffic.py")
setattr(W.skills, "traffic", S)

C = W.catalog.contract
# The OUTPUT_SCHEMAS["traffic.route"] value, verbatim, built with contract.py's helpers.
OUTPUT_SCHEMA = C._obj({
    "origin": C._s("Origin as given.", "Ferry Building, San Francisco, CA"),
    "destination": C._s("Destination as given.", "Oakland City Hall, Oakland, CA"),
    "travel_mode": C._enum(["DRIVE", "TWO_WHEELER", "WALK", "BICYCLE", "TRANSIT"], "Travel mode used.", "DRIVE"),
    "traffic": C._enum(["aware", "optimal", "none"],
                       "Traffic handling used: aware (live traffic), optimal (live traffic, best route quality), none (no traffic; always none for WALK, BICYCLE and TRANSIT).",
                       "aware"),
    "departure_time": C._s("Departure time sent to Google (RFC 3339, UTC); null when the trip starts now.",
                           "2026-09-27T16:30:00Z", nullable=True),
    "distance_meters": C._i("Route length in metres.", 17011),
    "duration_seconds": C._i("Travel time in seconds, with traffic when `traffic` is aware or optimal.", 1152),
    "static_duration_seconds": C._i("Travel time in seconds without traffic; null when Google does not state it.",
                                    1281, nullable=True),
    "delay_seconds": C._i("duration_seconds minus static_duration_seconds (negative when traffic is lighter than typical); null when either is unknown.",
                          -129, nullable=True),
    "duration_text": C._s("Google's localized travel time.", "19 mins", nullable=True),
    "static_duration_text": C._s("Google's localized travel time without traffic.", "21 mins", nullable=True),
    "distance_text": C._s("Google's localized distance (metric).", "17.0 km", nullable=True),
    "description": C._s("Route description as Google names it (main roads); null when not given.",
                        "I-80 E and I-580 E", nullable=True),
    "warnings": C._arr(C._s("A warning Google attaches to the route.", "This route has tolls."),
                       "Google's warnings for the route, in the order given; empty when none.", []),
    "advisory": C._nobj({}, [], "Google's travelAdvisory object for the route (tolls, speed reading intervals, fuel) exactly as returned, {} when it has nothing to say; null when absent."),
    "source": C._const("google-routes", "Data source: Google Routes API computeRoutes."),
    "as_of": C._s("The departure time the answer was computed for: `departure_time`, else `checked_at`.",
                  "2026-09-27T16:30:00Z"),
    "checked_at": C._CHECKED_AT,
}, ["origin", "destination", "travel_mode", "traffic", "departure_time", "distance_meters", "duration_seconds",
    "static_duration_seconds", "delay_seconds", "duration_text", "static_duration_text", "distance_text",
    "description", "warnings", "advisory", "source", "as_of", "checked_at"])


def _future(hours=2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _Ctx:
    """Serves the compute_routes step from a fixture; records the step, the
    provider and the arguments the skill sent."""

    def __init__(self, answer):
        self.answer = answer  # provider value dict | Exception
        self.steps, self.providers_used, self.attempts, self.calls = [], [], [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def compute(self, origin, destination, travel_mode, departure_time=None, routing_preference=None):
                outer.calls.append({"origin": origin, "destination": destination, "travel_mode": travel_mode,
                                    "departure_time": departure_time, "routing_preference": routing_preference})
                if isinstance(outer.answer, Exception):
                    raise outer.answer
                return W.runtime.ProviderResult(value=dict(outer.answer), cost_micros=None,
                                                cost_measured=False, usage="requests=1")

        result = await call(_P())
        self.providers_used.append(providers[0].id)
        return result.value


def _parsed_value(**overrides):
    value = GR._route_value(ROUTES_RESPONSE["routes"][0])
    value.update(overrides)
    return value


def _stub_google(monkeypatch, status, data, sent):
    """The credential and the wire, both faked: no ADC lookup, no network."""
    async def fake_headers():
        return {"Authorization": "Bearer test-token", "X-Goog-User-Project": "", "Content-Type": "application/json"}

    async def fake_post(url, headers, body):
        sent.append({"url": url, "headers": headers, "body": body})
        return status, data

    monkeypatch.setattr(GR.google_auth, "configured", lambda: True)
    monkeypatch.setattr(GR.google_auth, "project", lambda: None)
    monkeypatch.setattr(GR.google_auth, "headers", fake_headers)
    monkeypatch.setattr(GR, "_post_json", fake_post)


# --- the provider: request sent, verified response parsed ------------------------

def test_the_verified_response_is_parsed_and_the_request_is_the_verified_shape(monkeypatch):
    sent = []
    _stub_google(monkeypatch, 200, ROUTES_RESPONSE, sent)
    when = "2026-09-27T16:30:00Z"
    result = asyncio.run(GR.PROVIDERS[0].compute("Ferry Building, San Francisco, CA", "37.8044,-122.2712", "DRIVE",
                                                  departure_time=when, routing_preference="TRAFFIC_AWARE"))
    assert result.cost_micros is None and result.cost_measured is False and result.usage == "requests=1"
    value = result.value
    assert value["distance_meters"] == 17011 and value["duration_seconds"] == 1152
    assert value["static_duration_seconds"] == 1281
    assert value["duration_text"] == "19 mins" and value["static_duration_text"] == "21 mins"
    assert value["distance_text"] == "17.0 km"
    assert value["description"] is None and value["warnings"] == [] and value["advisory"] == {}
    assert value["travel_mode"] == "DRIVE" and value["routing_preference"] == "TRAFFIC_AWARE"
    assert value["departure_time"] == when
    # the request: verified body and headers, with "lat,lng" mapped to latLng
    assert len(sent) == 1
    assert sent[0]["url"] == "https://routes.googleapis.com/directions/v2:computeRoutes"
    assert sent[0]["body"] == {
        "origin": {"address": "Ferry Building, San Francisco, CA"},
        "destination": {"location": {"latLng": {"latitude": 37.8044, "longitude": -122.2712}}},
        "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE", "departureTime": when,
        "computeAlternativeRoutes": False, "units": "METRIC"}
    headers = sent[0]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["X-Goog-User-Project"] == "resolver-time"  # the box's project when the credential names none
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Goog-FieldMask"] == ("routes.duration,routes.staticDuration,routes.distanceMeters,"
                                           "routes.travelAdvisory,routes.localizedValues,routes.description,routes.warnings")


def test_no_routing_preference_and_no_departure_time_are_left_out_of_the_body(monkeypatch):
    sent = []
    _stub_google(monkeypatch, 200, ROUTES_RESPONSE, sent)
    monkeypatch.setattr(GR.google_auth, "project", lambda: "other-project")
    value = asyncio.run(GR.PROVIDERS[0].compute("A", "B", "WALK")).value
    assert "routingPreference" not in sent[0]["body"] and "departureTime" not in sent[0]["body"]
    assert sent[0]["headers"]["X-Goog-User-Project"] == "other-project"
    assert value["routing_preference"] is None and value["departure_time"] is None


def test_the_provider_refuses_a_traffic_preference_with_a_mode_the_api_rejects(monkeypatch):
    sent = []
    _stub_google(monkeypatch, 200, ROUTES_RESPONSE, sent)
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(GR.PROVIDERS[0].compute("A", "B", "WALK", routing_preference="TRAFFIC_AWARE"))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(GR.PROVIDERS[0].compute("A", "B", "FLY"))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(GR.PROVIDERS[0].compute("A", "B", "DRIVE", routing_preference="FASTEST"))
    assert sent == []  # nothing was sent


def test_googles_statuses_are_mapped_to_the_callers_error_unavailable_or_transient(monkeypatch):
    cases = [(400, INVALID_ARGUMENT, W.runtime.InvalidRequest),
             (404, NOT_FOUND, W.runtime.InvalidRequest),
             (403, PERMISSION_DENIED, W.runtime.ProviderUnavailable),
             (401, {"error": {"code": 401, "message": "no token", "status": "UNAUTHENTICATED"}}, W.runtime.ProviderUnavailable),
             (429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}, W.runtime.TransientProviderError),
             (503, None, W.runtime.TransientProviderError),
             (409, {"error": {"code": 409, "message": "odd", "status": "ABORTED"}}, W.runtime.PermanentProviderError),
             (200, {}, W.runtime.InvalidRequest),  # no route between the places
             (200, {"routes": [{"distanceMeters": 5}]}, W.runtime.InvalidProviderResponse),  # no duration
             (200, "not json", W.runtime.InvalidProviderResponse)]
    for status, data, expected in cases:
        _stub_google(monkeypatch, status, data, [])
        with pytest.raises(expected) as exc:
            asyncio.run(GR.PROVIDERS[0].compute("A", "B", "DRIVE", routing_preference="TRAFFIC_AWARE"))
        if status == 400:
            assert "Departure time must be in the future." in exc.value.detail
        if status == 404:
            assert "nowhere at all" in exc.value.detail


def test_an_unconfigured_deployment_is_unavailable_before_any_request(monkeypatch):
    sent = []
    _stub_google(monkeypatch, 200, ROUTES_RESPONSE, sent)
    monkeypatch.setattr(GR.google_auth, "configured", lambda: False)
    monkeypatch.setattr(GR.google_auth, "unavailable_reason", lambda: "no Google credentials on this deployment")
    assert GR.PROVIDERS[0].available() is False
    assert GR.PROVIDERS[0].unavailable_reason() == "no Google credentials on this deployment"
    with pytest.raises(W.runtime.ProviderUnavailable):
        asyncio.run(GR.PROVIDERS[0].compute("A", "B", "DRIVE"))
    assert sent == []


def test_durations_and_waypoints_are_parsed_robustly():
    parse = GR.parse_duration
    assert parse("1152s") == 1152 and parse("1152.5s") == 1153 and parse(" 7s ") == 7 and parse("0s") == 0
    assert parse("1152.4s") == 1152 and parse(1281) == 1281 and parse(12.6) == 13
    assert parse("1152") is None and parse("") is None and parse(None) is None and parse(True) is None
    assert parse("s") is None and parse("1152 m") is None
    assert GR.waypoint("37.7955, -122.3937") == {"location": {"latLng": {"latitude": 37.7955, "longitude": -122.3937}}}
    assert GR.waypoint("-33.8688,151.2093") == {"location": {"latLng": {"latitude": -33.8688, "longitude": 151.2093}}}
    assert GR.waypoint(" 1 Market St, San Francisco ") == {"address": "1 Market St, San Francisco"}
    assert GR.waypoint("Oakland, CA") == {"address": "Oakland, CA"}
    for bad in ("91,0", "0,181", "-90.5,10"):
        with pytest.raises(W.runtime.InvalidRequest):
            GR.waypoint(bad, "origin")


def test_a_route_value_reads_every_field_and_treats_an_omitted_distance_as_zero():
    value = GR._route_value({"distanceMeters": 17011, "duration": "1152s", "staticDuration": "1281s",
                             "description": " I-80 E and I-580 E ", "warnings": ["This route has tolls.", " ", 3],
                             "travelAdvisory": {"tollInfo": {"estimatedPrice": [{"currencyCode": "USD", "units": "7"}]}},
                             "localizedValues": {"distance": {"text": "17.0 km"}, "duration": {"text": "19 mins"}}})
    assert value["description"] == "I-80 E and I-580 E" and value["warnings"] == ["This route has tolls."]
    assert value["advisory"] == {"tollInfo": {"estimatedPrice": [{"currencyCode": "USD", "units": "7"}]}}
    assert value["static_duration_text"] is None and value["distance_text"] == "17.0 km"
    same_place = GR._route_value({"duration": "0s", "staticDuration": "0s"})
    assert same_place["distance_meters"] == 0 and same_place["duration_seconds"] == 0 and same_place["advisory"] is None
    with pytest.raises(W.runtime.InvalidProviderResponse):
        GR._route_value({"duration": "1s", "distanceMeters": "far"})


# --- the skill: input refused before the gate, result shaped and stamped ---------

def test_the_skill_sells_the_verified_route_with_delay_as_of_and_checked_at():
    ctx = _Ctx(_parsed_value())
    r = asyncio.run(S.route(ctx, {"origin": " Ferry Building, San Francisco, CA ",
                                  "destination": "Oakland City Hall, Oakland, CA"}))
    assert ctx.steps == ["compute_routes"] and ctx.providers_used == ["google-routes"]
    assert ctx.calls == [{"origin": "Ferry Building, San Francisco, CA", "destination": "Oakland City Hall, Oakland, CA",
                          "travel_mode": "DRIVE", "departure_time": None, "routing_preference": "TRAFFIC_AWARE"}]
    assert r["origin"] == "Ferry Building, San Francisco, CA" and r["destination"] == "Oakland City Hall, Oakland, CA"
    assert r["travel_mode"] == "DRIVE" and r["traffic"] == "aware" and r["departure_time"] is None
    assert r["distance_meters"] == 17011 and r["duration_seconds"] == 1152 and r["static_duration_seconds"] == 1281
    assert r["delay_seconds"] == 1152 - 1281 == -129
    assert r["duration_text"] == "19 mins" and r["static_duration_text"] == "21 mins" and r["distance_text"] == "17.0 km"
    assert r["description"] is None and r["warnings"] == [] and r["advisory"] == {}
    assert r["source"] == "google-routes"
    assert r["checked_at"].endswith("Z") and r["as_of"] == r["checked_at"]  # leaving now
    datetime.strptime(r["checked_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert list(r) == OUTPUT_SCHEMA["required"]  # every key emitted is in the contract, and required
    assert C.check(OUTPUT_SCHEMA, r) is None


def test_a_future_departure_optimal_traffic_and_warnings_flow_through():
    when = _future(3)
    ctx = _Ctx(_parsed_value(warnings=["This route has tolls.", "This route includes a ferry."],
                             description="I-80 E and I-580 E", advisory={"tollInfo": {}}))
    r = asyncio.run(S.route(ctx, {"origin": "37.7955,-122.3937", "destination": "Oakland City Hall, Oakland, CA",
                                  "travel_mode": "TWO_WHEELER", "traffic": "optimal", "departure_time": when}))
    assert ctx.calls[0]["routing_preference"] == "TRAFFIC_AWARE_OPTIMAL" and ctx.calls[0]["departure_time"] == when
    assert ctx.calls[0]["travel_mode"] == "TWO_WHEELER"
    assert r["traffic"] == "optimal" and r["departure_time"] == when and r["as_of"] == when
    assert r["warnings"] == ["This route has tolls.", "This route includes a ferry."]
    assert r["description"] == "I-80 E and I-580 E" and r["advisory"] == {"tollInfo": {}}
    assert C.check(OUTPUT_SCHEMA, r) is None
    # traffic "none" on DRIVE sends TRAFFIC_UNAWARE
    ctx = _Ctx(_parsed_value(static_duration_seconds=1152))
    r = asyncio.run(S.route(ctx, {"origin": "A", "destination": "B", "traffic": "none"}))
    assert ctx.calls[0]["routing_preference"] == "TRAFFIC_UNAWARE" and r["traffic"] == "none" and r["delay_seconds"] == 0


def test_walk_bicycle_and_transit_send_no_routing_preference_and_report_traffic_none():
    for mode in ("WALK", "BICYCLE", "TRANSIT"):
        ctx = _Ctx(_parsed_value(static_duration_seconds=None, static_duration_text=None))
        r = asyncio.run(S.route(ctx, {"origin": "A", "destination": "B", "travel_mode": mode, "traffic": "aware"}))
        assert ctx.calls[0]["routing_preference"] is None and ctx.calls[0]["travel_mode"] == mode
        assert r["travel_mode"] == mode and r["traffic"] == "none"
        assert r["static_duration_seconds"] is None and r["delay_seconds"] is None  # never invented
        assert C.check(OUTPUT_SCHEMA, r) is None
    parsed = S.parse({"origin": "A", "destination": "B", "travel_mode": "WALK"})
    assert parsed["routing_preference"] is None and parsed["traffic"] == "none"


def test_bad_input_is_refused_before_the_gate():
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    for bad in ({}, {"origin": "A"}, {"destination": "B"}, {"origin": " ", "destination": "B"},
                {"origin": "A", "destination": 5}, {"origin": "A" * 301, "destination": "B"},
                {"origin": "A", "destination": "B", "travel_mode": "drive"},
                {"origin": "A", "destination": "B", "travel_mode": "FLY"},
                {"origin": "A", "destination": "B", "travel_mode": None},
                {"origin": "A", "destination": "B", "traffic": "TRAFFIC_AWARE"},
                {"origin": "A", "destination": "B", "traffic": True},
                {"origin": "A", "destination": "B", "departure_time": past},
                {"origin": "A", "destination": "B", "departure_time": "2030-01-01T10:00:00"},  # no zone
                {"origin": "A", "destination": "B", "departure_time": "tomorrow at 5"},
                {"origin": "A", "destination": "B", "departure_time": 1790000000},
                {"origin": "91,0", "destination": "B"}, {"origin": "A", "destination": "0,181"}):
        with pytest.raises(W.runtime.InvalidRequest):
            S.precheck(bad)
    assert S.PRECHECKS["traffic.route"] is S.precheck and S.SKILLS["traffic.route"] is S.route


def test_departure_time_is_normalized_to_utc_z_and_defaults_are_applied():
    parsed = S.parse({"origin": "A", "destination": "B"})
    assert parsed == {"origin": "A", "destination": "B", "travel_mode": "DRIVE", "traffic": "aware",
                      "routing_preference": "TRAFFIC_AWARE", "departure_time": None}
    year = datetime.now(timezone.utc).year + 1
    parsed = S.parse({"origin": "A", "destination": "B", "departure_time": f"{year}-06-01T09:30:00.250+02:00"})
    assert parsed["departure_time"] == f"{year}-06-01T07:30:00Z"
    parsed = S.parse({"origin": "A", "destination": "B", "departure_time": f"{year}-06-01T09:30:00z"})
    assert parsed["departure_time"] == f"{year}-06-01T09:30:00Z"


def test_a_provider_failure_is_not_turned_into_a_result():
    ctx = _Ctx(W.runtime.TransientProviderError("Google Routes returned 503"))
    with pytest.raises(W.runtime.TransientProviderError):
        asyncio.run(S.route(ctx, {"origin": "A", "destination": "B"}))
    ctx = _Ctx(W.runtime.InvalidRequest("Google Routes found no WALK route between these places."))
    with pytest.raises(W.runtime.InvalidRequest):
        asyncio.run(S.route(ctx, {"origin": "A", "destination": "B", "travel_mode": "WALK"}))


def test_the_contract_schema_is_a_valid_draft_2020_12_schema_with_every_key_required():
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(OUTPUT_SCHEMA)
    assert set(OUTPUT_SCHEMA["properties"]) == set(OUTPUT_SCHEMA["required"])
    assert OUTPUT_SCHEMA["properties"]["checked_at"] is C._CHECKED_AT
    assert OUTPUT_SCHEMA["properties"]["source"]["const"] == "google-routes"
