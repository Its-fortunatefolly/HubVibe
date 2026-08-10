import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER_PATH = REPO_ROOT / "wcag-audit-engine" / "integrations" / "mcp_server.py"

pytest.importorskip("mcp", reason="mcp SDK is a standalone-script dependency, not in root requirements.txt")

spec = importlib.util.spec_from_file_location("hubvibe_mcp_server", MCP_SERVER_PATH)
mcp_server = importlib.util.module_from_spec(spec)
sys.modules["hubvibe_mcp_server"] = spec.loader.exec_module(mcp_server) or mcp_server


def test_all_five_tools_registered():
    tools = asyncio.run(mcp_server.server.list_tools())
    names = {t.name for t in tools}
    assert names == {"audit_wcag", "audit_seo", "audit_security", "audit_performance", "audit_bundle"}


def test_tool_call_hits_correct_endpoint_with_api_key(monkeypatch):
    monkeypatch.setenv("HUBVIBE_API_KEY", "test-key")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"status": "ok", "pass": True}

    with patch("httpx.post", return_value=fake_resp) as mock_post:
        result = asyncio.run(mcp_server.server.call_tool("audit_seo", {"url": "https://example.com"}))

    assert result.is_error is False
    call_kwargs = mock_post.call_args
    assert call_kwargs.args[0].endswith("/audit/seo")
    assert call_kwargs.kwargs["json"] == {"url": "https://example.com"}
    assert call_kwargs.kwargs["headers"] == {"X-API-Key": "test-key"}


def test_tool_call_without_api_key_raises(monkeypatch):
    monkeypatch.delenv("HUBVIBE_API_KEY", raising=False)
    with pytest.raises(Exception):
        asyncio.run(mcp_server.server.call_tool("audit_wcag", {"url": "https://example.com"}))


def test_payment_required_response_raises_not_silently_succeeds(monkeypatch):
    # A 402 must never be treated as a completed (empty) audit result.
    monkeypatch.setenv("HUBVIBE_API_KEY", "test-key")
    fake_resp = MagicMock()
    fake_resp.status_code = 402
    fake_resp.json.return_value = {"price": "$0.03"}

    with patch("httpx.post", return_value=fake_resp):
        with pytest.raises(Exception):
            asyncio.run(mcp_server.server.call_tool("audit_wcag", {"url": "https://example.com"}))
