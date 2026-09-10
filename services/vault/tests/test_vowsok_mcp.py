"""Vowsok's advertised name field must reach its backend as fullName."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import custom_mcp  # noqa: E402


@pytest.mark.parametrize("arguments,expected", [
    ({"name": "Test Lead", "email": "lead@example.test"},
     {"fullName": "Test Lead", "email": "lead@example.test"}),
    ({"fullName": "Test Lead"}, {"fullName": "Test Lead"}),
    ({"name": "Test Lead", "fullName": "Test Lead"}, {"fullName": "Test Lead"}),
    ({}, {}),
])
def test_vowsok_create_client_wire_arguments(monkeypatch, arguments, expected):
    original = dict(arguments)
    session = AsyncMock(return_value={"result": {"content": []}})
    monkeypatch.setattr(custom_mcp, "_validate_server_url", lambda url: None)
    monkeypatch.setattr(custom_mcp, "_mcp_session", session)
    asyncio.run(custom_mcp.call_tool(
        "https://app.vowsok.com/api/mcp", "test-token", "create_client", arguments,
    ))
    assert session.call_args.kwargs["params"] == {
        "name": "create_client", "arguments": expected,
    }
    assert arguments == original


@pytest.mark.parametrize("url,tool", [
    ("https://other.example/api/mcp", "create_client"),
    ("https://app.vowsok.com.other.example/api/mcp", "create_client"),
    ("https://app.vowsok.com/api/mcp", "update_client"),
])
def test_other_providers_and_tools_unchanged(monkeypatch, url, tool):
    session = AsyncMock(return_value={"result": {"content": []}})
    monkeypatch.setattr(custom_mcp, "_validate_server_url", lambda url: None)
    monkeypatch.setattr(custom_mcp, "_mcp_session", session)
    asyncio.run(custom_mcp.call_tool(url, "test-token", tool, {"name": "Test Lead"}))
    assert session.call_args.kwargs["params"]["arguments"] == {"name": "Test Lead"}


def test_conflicting_aliases_fail_before_upstream_write(monkeypatch):
    session = AsyncMock()
    monkeypatch.setattr(custom_mcp, "_validate_server_url", lambda url: None)
    monkeypatch.setattr(custom_mcp, "_mcp_session", session)
    with pytest.raises(custom_mcp.CustomMCPError, match="must match"):
        asyncio.run(custom_mcp.call_tool(
            "https://app.vowsok.com/api/mcp", "test-token", "create_client",
            {"name": "First", "fullName": "Second"},
        ))
    session.assert_not_called()