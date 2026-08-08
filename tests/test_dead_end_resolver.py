import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_main():
    # The service directory's name carries a leading zero-width space, so
    # it's matched by glob rather than hardcoded.
    candidates = list(REPO_ROOT.glob("*dead-end-resolver/app/main.py"))
    assert candidates, "dead-end-resolver app/main.py not found"
    spec = importlib.util.spec_from_file_location("dead_end_resolver_main", candidates[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_check():
    from fastapi.testclient import TestClient

    module = _load_main()
    client = TestClient(module.app)
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "dead-end-resolver"}


def test_agent_manifest():
    from fastapi.testclient import TestClient

    module = _load_main()
    client = TestClient(module.app)
    response = client.get("/.well-known/agent.json")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Dead-End Resolver"
    assert body["endpoints"][0]["payment_required"] is True
