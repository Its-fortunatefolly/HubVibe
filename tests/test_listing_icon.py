"""The service icon: one HV monogram PNG, shown by every listing.

Agentic.Market listed HubVibe with no icon (enriched=false, iconUrl "")
because no 402 carried `resource.iconUrl`. These pin that every surface a
directory reads -- the x402 v2 challenge, the A2A card, the MCP manifests --
points at the same served PNG on our own origin.
"""

import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"
STATIC = APP / "static"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop_sibling_cache():
    for name in [n for n in sys.modules
                 if n.startswith("wcag_audit_engine_") and n != "wcag_audit_engine_workers"]:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _isolated():
    _drop_sibling_cache()
    yield
    _drop_sibling_cache()


def test_the_icon_is_a_square_png():
    data = (STATIC / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height == 512
    assert len(data) < 200_000, "keep it light: directories fetch it on every crawl"


@pytest.mark.parametrize("url, expected", [
    ("https://hubvibe-io.com/work/market/quote", "https://hubvibe-io.com/icon.png"),
    ("http://127.0.0.1:8080/audit/wcag", "http://127.0.0.1:8080/icon.png"),
    ("", None), (None, None), ("/audit/wcag", None), ("ftp://x/y", None),
])
def test_the_icon_lives_on_the_routes_own_origin(url, expected):
    x402 = _load("wcag_audit_engine_x402_icon", APP / "x402_payments.py")
    assert x402.service_icon_url(url) == expected


def test_the_v2_challenge_names_the_icon(monkeypatch):
    monkeypatch.setenv("X402_PAY_TO_ADDRESS", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd")
    monkeypatch.setenv("X402_FACILITATOR_URL", "https://facilitator.example")
    x402 = _load("wcag_audit_engine_x402_icon2", APP / "x402_payments.py")
    monkeypatch.setattr(x402, "_facilitator_supports", lambda version, network: True)
    challenge = x402.payment_required_v2_dict(
        price="$0.02", resource_url="https://hubvibe-io.com/work/market/quote",
        description="Crypto spot quote")
    assert challenge is not None, "this test environment must be able to build a v2 challenge"
    resource = challenge["resource"]
    assert resource["iconUrl"] == "https://hubvibe-io.com/icon.png"
    assert resource["serviceName"] == "HubVibe"


def test_the_served_icon_and_every_manifest_agree(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("WORKER_LEDGER_PATH", str(tmp_path / "workers.db"))
    monkeypatch.setenv("SANCTIONS_PREFETCH", "0")
    monkeypatch.setenv("A2A_TASKS_PATH", "")
    main = _load("wcag_audit_main_listing_icon", APP / "main.py")
    client = TestClient(main.app)
    base = main.PUBLIC_BASE_URL.rstrip("/")

    response = client.get("/icon.png")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content == (STATIC / "icon.png").read_bytes()

    mcp = client.get("/mcp.json").json()
    assert mcp["icons"][0] == {"src": f"{base}/icon.png", "mimeType": "image/png", "sizes": ["512x512"]}
    assert mcp["icons"][1]["src"] == f"{base}/favicon.svg"

    card = client.get("/.well-known/agent-card.json").json()
    assert card["iconUrl"] == f"{base}/icon.png"

    registry = json.loads((REPO_ROOT / "server.json").read_text())
    assert registry["icons"][0]["src"] == "https://hubvibe-io.com/icon.png"
