"""travel.flights -- live airline offers, any language in, always current.

Pinned: a real Duffel offer-request response (captured 2026-09-27, test
mode, LHR->JFK) normalised into offers with money, segments, conditions and
expiry; ISO durations to seconds; place resolution (IATA passthrough, city
over airport, no match refused); passenger typing (under-2s as infants);
price/duration sorting; every bad input refused free before the gate;
fail-closed on a test token unless allowed on purpose; live_mode disclosed;
the route, the MCP tool and the static manifests sell the same row.
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "main.py"
PKG = REPO_ROOT / "wcag-audit-engine" / "app" / "workers"
TEST_PAY_TO = "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"
WORKER, PATH, TOOL = "travel.flights", "/work/travel/flights", "hubvibe_travel_flights"
TODAY = date(2026, 9, 27)

DUFFEL_RESPONSE = json.loads(r'''{
 "id": "orq_0000BAq4P274r6608zdzps",
 "live_mode": false,
 "slices": [
  {
   "destination_type": "airport",
   "origin_type": "airport",
   "departure_date": "2026-11-10",
   "destination": {
    "city_name": "New York",
    "icao_code": "KJFK",
    "iata_city_code": "NYC",
    "iata_country_code": "US",
    "iata_code": "JFK",
    "latitude": 40.640556,
    "longitude": -73.778519,
    "city": {
     "city_name": null,
     "icao_code": null,
     "iata_city_code": "NYC",
     "iata_country_code": "US",
     "iata_code": "NYC",
     "latitude": null,
     "longitude": null,
     "time_zone": null,
     "type": "city",
     "name": "New York",
     "id": "cit_nyc_us"
    },
    "time_zone": "America/New_York",
    "type": "airport",
    "name": "John F. Kennedy International Airport",
    "id": "arp_jfk_us"
   },
   "origin": {
    "city_name": "London",
    "icao_code": "EGLL",
    "iata_city_code": "LON",
    "iata_country_code": "GB",
    "iata_code": "LHR",
    "latitude": 51.470311,
    "longitude": -0.458118,
    "city": {
     "city_name": null,
     "icao_code": null,
     "iata_city_code": "LON",
     "iata_country_code": "GB",
     "iata_code": "LON",
     "latitude": null,
     "longitude": null,
     "time_zone": null,
     "type": "city",
     "name": "London",
     "id": "cit_lon_gb"
    },
    "time_zone": "Europe/London",
    "type": "airport",
    "name": "Heathrow Airport",
    "id": "arp_lhr_gb"
   }
  }
 ],
 "passengers": [
  {
   "loyalty_programme_accounts": [],
   "fare_type": null,
   "family_name": null,
   "given_name": null,
   "user_id": null,
   "age": null,
   "type": "adult",
   "id": "pas_0000BAq4P274r6608zdzq0"
  }
 ],
 "cabin_class": "economy",
 "offers": [
  {
   "id": "off_0000BAq4P2Ku1h9eps72O0",
   "owner": {
    "conditions_of_carriage_url": "https://duffelairways.com/dummy-url/conditions-of-carriage",
    "iata_code": "ZZ",
    "id": "arl_00009VME7D6ivUu8dn35WK",
    "logo_lockup_url": null,
    "logo_symbol_url": "https://assets.duffel.com/img/airlines/for-light-background/full-color-logo/ZZ.svg",
    "name": "Duffel Airways"
   },
   "total_amount": "218.93",
   "total_currency": "USD",
   "base_amount": "185.53",
   "tax_amount": "33.40",
   "expires_at": "2026-09-27T16:18:23.827932Z",
   "live_mode": false,
   "conditions": {
    "change_before_departure": {
     "allowed": false,
     "penalty_amount": null,
     "penalty_currency": null
    },
    "refund_before_departure": {
     "allowed": true,
     "penalty_amount": "40.00",
     "penalty_currency": "USD"
    }
   },
   "payment_requirements": {
    "requires_instant_payment": false,
    "payment_required_by": "2026-09-30T15:48:23Z",
    "price_guarantee_expires_at": "2026-09-29T15:48:23Z"
   },
   "total_emissions_kg": "637",
   "passenger_identity_documents_required": false,
   "slices": [
    {
     "origin": {
      "iata_code": "LHR",
      "name": "Heathrow Airport",
      "city_name": "London",
      "type": "airport"
     },
     "destination": {
      "iata_code": "JFK",
      "name": "John F. Kennedy International Airport",
      "city_name": "New York",
      "type": "airport"
     },
     "duration": "PT7H58M",
     "fare_brand_name": "Basic",
     "conditions": {
      "change_before_departure": {
       "allowed": false,
       "penalty_amount": null,
       "penalty_currency": null
      },
      "advance_seat_selection": null,
      "priority_boarding": null,
      "priority_check_in": null
     },
     "segments": [
      {
       "marketing_carrier": {
        "iata_code": "ZZ",
        "name": "Duffel Airways"
       },
       "marketing_carrier_flight_number": "9368",
       "operating_carrier": {
        "iata_code": "ZZ",
        "name": "Duffel Airways"
       },
       "operating_carrier_flight_number": "9368",
       "departing_at": "2026-11-10T06:58:00",
       "arriving_at": "2026-11-10T09:56:00",
       "duration": "PT7H58M",
       "distance": "5539.8359982030115",
       "aircraft": null,
       "origin": {
        "iata_code": "LHR",
        "name": "Heathrow Airport",
        "city_name": "London"
       },
       "destination": {
        "iata_code": "JFK",
        "name": "John F. Kennedy International Airport",
        "city_name": "New York"
       }
      }
     ]
    }
   ]
  },
  {
   "id": "off_0000BAq4P2Lbz3ios4RbUF",
   "owner": {
    "conditions_of_carriage_url": "https://www.iberia.com/gb/bills/conditions/",
    "iata_code": "IB",
    "id": "arl_00009VME7DCOaPRQvNhcMu",
    "logo_lockup_url": "https://assets.duffel.com/img/airlines/for-light-background/full-color-lockup/IB.svg",
    "logo_symbol_url": "https://assets.duffel.com/img/airlines/for-light-background/full-color-logo/IB.svg",
    "name": "Iberia"
   },
   "total_amount": "219.45",
   "total_currency": "USD",
   "base_amount": "185.97",
   "tax_amount": "33.48",
   "expires_at": "2026-09-27T16:18:23.830019Z",
   "live_mode": false,
   "conditions": {
    "change_before_departure": {
     "allowed": false,
     "penalty_amount": null,
     "penalty_currency": null
    },
    "refund_before_departure": {
     "allowed": true,
     "penalty_amount": "40.00",
     "penalty_currency": "USD"
    }
   },
   "payment_requirements": {
    "requires_instant_payment": false,
    "payment_required_by": "2026-09-30T15:48:23Z",
    "price_guarantee_expires_at": "2026-09-29T15:48:23Z"
   },
   "total_emissions_kg": "513",
   "passenger_identity_documents_required": false,
   "slices": [
    {
     "origin": {
      "iata_code": "LHR",
      "name": "Heathrow Airport",
      "city_name": "London",
      "type": "airport"
     },
     "destination": {
      "iata_code": "JFK",
      "name": "John F. Kennedy International Airport",
      "city_name": "New York",
      "type": "airport"
     },
     "duration": "PT7H58M",
     "fare_brand_name": "Basic",
     "conditions": {
      "change_before_departure": {
       "allowed": false,
       "penalty_amount": null,
       "penalty_currency": null
      },
      "advance_seat_selection": null,
      "priority_boarding": null,
      "priority_check_in": null
     },
     "segments": [
      {
       "marketing_carrier": {
        "iata_code": "IB",
        "name": "Iberia"
       },
       "marketing_carrier_flight_number": "3177",
       "operating_carrier": {
        "iata_code": "IB",
        "name": "Iberia"
       },
       "operating_carrier_flight_number": "3177",
       "departing_at": "2026-11-10T06:58:00",
       "arriving_at": "2026-11-10T09:56:00",
       "duration": "PT7H58M",
       "distance": "5539.8359982030115",
       "aircraft": null,
       "origin": {
        "iata_code": "LHR",
        "name": "Heathrow Airport",
        "city_name": "London"
       },
       "destination": {
        "iata_code": "JFK",
        "name": "John F. Kennedy International Airport",
        "city_name": "New York"
       }
      }
     ]
    }
   ]
  }
 ]
}''')
PLACES_TOKYO = [{"iata_code": "TYO", "name": "Tokyo", "type": "city", "iata_country_code": "JP", "city_name": None},
                {"iata_code": "NRT", "name": "Narita International Airport", "type": "airport", "iata_country_code": "JP", "city_name": "Tokyo"},
                {"iata_code": "HND", "name": "Haneda Airport", "type": "airport", "iata_country_code": "JP", "city_name": "Tokyo"}]


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
T = W.skills.travel


class _Ctx:
    """Serves the provider's two methods from fixtures and records the calls."""

    def __init__(self, places=None, offers=None):
        self.places = places if places is not None else PLACES_TOKYO
        self.offers = offers if offers is not None else DUFFEL_RESPONSE
        self.steps, self.calls = [], []

    def remaining(self):
        return 999

    async def run(self, step, providers, call, **kwargs):
        self.steps.append(step)
        outer = self

        class _P:
            id = providers[0].id

            async def places(self, query):
                outer.calls.append(("places", query))
                return W.runtime.ProviderResult(value=outer.places, cost_micros=0, cost_measured=True)

            async def offers(self, slices, passengers, cabin_class=None, max_connections=None):
                outer.calls.append(("offers", slices, passengers, cabin_class, max_connections))
                data = dict(outer.offers)
                return W.runtime.ProviderResult(value={"id": data["id"], "live_mode": data["live_mode"],
                                                       "offers": data["offers"], "slices": data["slices"],
                                                       "passengers": data["passengers"]},
                                                cost_micros=0, cost_measured=True)

        result = await call(_P())
        return result.value


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# --- parsing and free refusals ------------------------------------------------

def test_dates_places_and_passengers_are_validated_before_the_gate():
    ok = T.parse({"origin": "LHR", "destination": "JFK", "departure_date": "2026-11-10"}, today=TODAY)
    assert ok["departure_date"] == "2026-11-10" and ok["adults"] == 1 and ok["cabin_class"] == "economy"
    assert T.parse({"origin": "LHR", "destination": "JFK", "days_ahead": 30}, today=TODAY)["departure_date"] == "2026-10-27"
    for bad in ({"origin": "LHR"}, {"origin": "LHR", "destination": "LHR", "days_ahead": 1},
                {"origin": "LHR", "destination": "JFK"},
                {"origin": "LHR", "destination": "JFK", "departure_date": "2026-09-01"},
                {"origin": "LHR", "destination": "JFK", "departure_date": "2026/11/10"},
                {"origin": "LHR", "destination": "JFK", "departure_date": "2026-02-30"},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 30, "departure_date": "2026-11-10"},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 400},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "return_date": "2026-09-27"},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "adults": 0},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "children_ages": [18]},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "cabin_class": "coach"},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "max_connections": 3},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "sort": "cheapest"},
                {"origin": "LHR", "destination": "JFK", "days_ahead": 1, "max_offers": 0}):
        with pytest.raises(W.runtime.InvalidRequest):
            T.parse(bad, today=TODAY)


def test_passengers_are_typed_the_way_airlines_price_them():
    assert T.passengers_for(2, [1, 8]) == [{"type": "adult"}, {"type": "adult"}, {"type": "infant_without_seat"}, {"age": 8}]


def test_iso_durations_become_seconds():
    assert T.duration_seconds("PT7H58M") == 28680 and T.duration_seconds("P1DT2H") == 93600
    assert T.duration_seconds("PT45S") == 45 and T.duration_seconds(None) is None and T.duration_seconds("weird") is None


def test_place_resolution_prefers_an_exact_code_then_a_city_then_an_airport():
    assert T.pick_place(PLACES_TOKYO, "東京")["iata_code"] == "TYO"
    assert T.pick_place(PLACES_TOKYO, "hnd")["iata_code"] == "HND"
    assert T.pick_place([p for p in PLACES_TOKYO if p["type"] == "airport"], "Tokyo")["iata_code"] == "NRT"
    assert T.pick_place([], "Nowhere") is None


# --- the real response, normalised --------------------------------------------

def test_a_real_offer_is_normalised_with_money_segments_conditions_and_expiry():
    offer = T.normalize_offer(DUFFEL_RESPONSE["offers"][0])
    assert offer["id"].startswith("off_") and offer["airline"]["name"] == "Duffel Airways"
    assert offer["total_amount"] == 218.93 and offer["base_amount"] == 185.53 and offer["tax_amount"] == 33.4
    assert offer["currency"] == "USD" and offer["expires_at"].endswith("Z")
    leg = offer["slices"][0]
    assert leg["origin"]["iata_code"] == "LHR" and leg["destination"]["iata_code"] == "JFK"
    assert leg["duration_seconds"] == 28680 and leg["fare_brand"] == "Basic"
    seg = leg["segments"][0]
    assert seg["flight_number"] == "9368" and seg["departing_at"] == "2026-11-10T06:58:00"
    assert seg["distance_km"] == 5539.8 and seg["carrier"]["iata_code"] == "ZZ"
    assert offer["stops"] == 0 and offer["total_duration_seconds"] == 28680
    assert offer["refund_before_departure"] == {"allowed": True, "penalty_amount": 40.0, "penalty_currency": "USD"}
    assert offer["change_before_departure"]["allowed"] is False
    assert offer["emissions_kg"] == 637.0 and offer["instant_payment_required"] is False
    assert offer["identity_documents_required"] is False


def test_sorting_by_price_and_by_duration():
    a = {"total_amount": 300.0, "total_duration_seconds": 100}
    b = {"total_amount": 200.0, "total_duration_seconds": 500}
    c = {"total_amount": None, "total_duration_seconds": None}
    assert [o["total_amount"] for o in T.sort_offers([a, b, c], "price")] == [200.0, 300.0, None]
    assert [o["total_duration_seconds"] for o in T.sort_offers([a, b, c], "duration")] == [100, 500, None]


def test_the_skill_resolves_names_searches_once_and_discloses_practice_mode():
    ctx = _Ctx()
    result = _run(T.flights(ctx, {"origin": "LHR", "destination": "東京", "days_ahead": 30, "max_offers": 1,
                                  "adults": 1, "children_ages": [1], "cabin_class": "business", "max_connections": 0}))
    assert ctx.steps == ["place:destination", "offers"]
    assert ctx.calls[0] == ("places", "東京")
    _, slices, passengers, cabin, max_conn = ctx.calls[1]
    assert slices[0]["origin"] == "LHR" and slices[0]["destination"] == "TYO" and cabin == "business" and max_conn == 0
    assert passengers == [{"type": "adult"}, {"type": "infant_without_seat"}]
    assert result["origin"]["given"] == "LHR" and result["origin"]["iata_code"] == "LHR"
    assert result["destination"] == {"iata_code": "TYO", "name": "Tokyo", "type": "city", "city_name": "Tokyo",
                                     "country_code": "JP", "given": "東京"}
    assert result["offer_count"] == 1 and result["offers_available"] == 2
    assert result["cheapest_total"] == {"amount": 218.93, "currency": "USD"}
    assert result["live_mode"] is False and result["notes"][0].startswith("Practice offers")
    assert result["checked_at"].endswith("Z")
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


def test_a_return_trip_sends_two_slices_and_an_unknown_place_is_refused():
    ctx = _Ctx()
    result = _run(T.flights(ctx, {"origin": "LHR", "destination": "JFK", "departure_date": "2026-11-10",
                                  "return_date": "2026-11-17"}))
    assert len(ctx.calls[0][1]) == 2 and ctx.calls[0][1][1] == {"origin": "JFK", "destination": "LHR", "departure_date": "2026-11-17"}
    assert result["return_date"] == "2026-11-17" and ctx.steps == ["offers"]
    with pytest.raises(W.runtime.InvalidRequest):
        _run(T.flights(_Ctx(places=[]), {"origin": "Atlantis", "destination": "JFK", "days_ahead": 3}))


def test_no_offers_is_a_delivered_empty_answer_with_a_note():
    empty = dict(DUFFEL_RESPONSE, offers=[], live_mode=True)
    result = _run(T.flights(_Ctx(offers=empty), {"origin": "LHR", "destination": "JFK", "days_ahead": 3}))
    assert result["offers"] == [] and result["cheapest_total"] is None and result["fastest_duration_seconds"] is None
    assert result["live_mode"] is True and result["notes"] == ["No airline returned an offer for this search."]
    jsonschema.validate(result, W.catalog.contract.OUTPUT_SCHEMAS[WORKER])


# --- fail-closed on practice tokens ------------------------------------------

def test_the_provider_is_unavailable_without_a_live_token_unless_allowed_on_purpose(monkeypatch):
    provider = W.providers.duffel.PROVIDERS[0]
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WORKER_DUFFEL_ALLOW_TEST", raising=False)
    assert provider.available() is False and "not set" in provider.unavailable_reason()
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_abc")
    assert provider.available() is False and "practice" in provider.unavailable_reason()
    monkeypatch.setenv("WORKER_DUFFEL_ALLOW_TEST", "1")
    assert provider.available() is True
    monkeypatch.delenv("WORKER_DUFFEL_ALLOW_TEST")
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_abc")
    assert provider.available() is True and W.catalog.BY_NAME[WORKER].available()


# --- the route, the tool, the contract ---------------------------------------

@pytest.fixture
def app_module(monkeypatch, tmp_path):
    global W
    W = _load_workers()
    monkeypatch.setenv("AUDIT_API_KEY", "test-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://audit.example.test")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", TEST_PAY_TO)
    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_test_only")
    W.ledger.reset_for_tests()
    W.runtime.reset_breakers()
    spec = importlib.util.spec_from_file_location("wcag_audit_main_workers_travel", MAIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if getattr(module, "workers", None) is not None:
        W = module.workers
    monkeypatch.setattr(module.x402_payments, "_facilitator_supports", lambda version, network: True)
    W.router.configure(
        authorize_and_rate_limit=module._authorize_and_rate_limit,
        bill=module._bill, deliver=module._deliver,
        failed_response=module._failed_audit_response,
        node_version=module.SERVICE_VERSION,
        mpp_payment_facts=module.mpp_payments.settlement_for,
        blocked_target_reason=module.audits.blocked_target_reason)
    yield module
    W.ledger.reset_for_tests()


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def _serve_from_fixture(monkeypatch):
    async def skill(ctx, payload):
        return await W.skills.travel.flights(_Ctx(), payload)
    registry = dict(W.router.REGISTRY)
    registry[WORKER] = skill
    monkeypatch.setattr(W.router, "REGISTRY", registry)


def test_the_row_is_priced_on_the_ladder_and_always_current():
    w = W.catalog.BY_NAME[WORKER]
    assert w.path == PATH and w.price_usd == 0.50 and w.tier == "standard" and w.requires == ["duffel"]
    schema = W.catalog.contract.OUTPUT_SCHEMAS[WORKER]
    assert "checked_at" in schema["required"] and "live_mode" in schema["required"]
    jsonschema.validate(W.catalog.example_for(w), w.input_schema)
    assert len(w.description) <= 500 and W.skills.PRECHECKS.get(w.skill) is not None


def test_bad_input_over_http_is_a_free_400_before_the_gate(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the payment gate ran for a request the precheck refuses")
    monkeypatch.setattr(W.router, "_authorize", refuse)
    response = client.post(PATH, headers={"X-API-Key": "test-key"},
                           json={"origin": "LHR", "destination": "JFK", "departure_date": "2020-01-01"})
    assert response.status_code == 400 and response.json()["billed"] is False


def test_a_paid_call_over_http_and_mcp_delivers_the_envelope(client, monkeypatch):
    _serve_from_fixture(monkeypatch)
    body = {"origin": "LHR", "destination": "JFK", "days_ahead": 30, "max_offers": 2}
    response = client.post(PATH, json=body)
    assert response.status_code == 402
    accept = response.json()["accepts"][0]
    assert int(accept.get("maxAmountRequired") or accept.get("amount")) == 500_000
    response = client.post(PATH, headers={"X-API-Key": "test-key"}, json=body)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["worker"] == WORKER and envelope["price_usd"] == 0.50
    jsonschema.validate(envelope, W.catalog.response_schema(W.catalog.BY_NAME[WORKER]))
    assert envelope["result"]["offer_count"] == 2 and envelope["result"]["offers"][0]["total_amount"] == 218.93
    receipt = client.get(envelope["receipt_url"]).json()
    assert receipt["request"]["worker"] == WORKER and receipt["execution"]["status"] == "ok"
    mcp = client.post("/mcp", headers={"X-API-Key": "test-key"}, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"origin": "LHR", "destination": "東京", "days_ahead": 30}}})
    result = mcp.json()["result"]
    assert result["isError"] is False and result["structuredContent"]["result"]["destination"]["iata_code"] == "TYO"


def test_the_tool_is_listed_and_the_static_manifests_match(app_module, client):
    live = next(t for t in app_module._mcp_tools() if t["name"] == TOOL)
    worker = W.catalog.BY_NAME[WORKER]
    assert live["outputSchema"] == W.catalog.response_schema(worker) and "$0.50 per call" in live["description"]
    served = next(t for t in client.get("/mcp.json").json()["tools"] if t["name"] == TOOL)
    assert served["httpEndpoint"] == {"method": "POST", "path": PATH, "price_usd": 0.50}
    static_dir = REPO_ROOT / "wcag-audit-engine" / "app" / "static"
    for name in ("mcp.json",):
        tool = next(t for t in json.loads((static_dir / name).read_text())["tools"] if t["name"] == TOOL)
        assert tool["inputSchema"] == live["inputSchema"] and tool["httpEndpoint"] == served["httpEndpoint"]
    glama = json.loads((REPO_ROOT / "glama.json").read_text())
    assert any(t["name"] == TOOL for t in glama["tools"])
