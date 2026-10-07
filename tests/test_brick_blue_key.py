"""The brick.blue ownership file: the owner's public key, nothing else.

brick.blue proves a domain by reading /.well-known/brick-blue.json. A wrong
shape fails the claim; the route leaking into the OpenAPI document (which
feeds the x402 catalogs and every listing) would change what buyers see.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
APP = REPO_ROOT / "wcag-audit-engine" / "app"


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


def test_brick_blue_document_names_the_owner_key():
    main = _load("wcag_audit_main_brick_blue", APP / "main.py")
    r = TestClient(main.app).get("/.well-known/brick-blue.json")
    assert r.status_code == 200
    key = main._BRICK_BLUE_KEY
    assert r.json() == {"key": key, "owner": f"key:{key}"}
    assert 43 <= len(key) <= 44


def test_brick_blue_route_stays_out_of_the_catalog():
    main = _load("wcag_audit_main_brick_blue_schema", APP / "main.py")
    assert "/.well-known/brick-blue.json" not in main.app.openapi().get("paths", {})
