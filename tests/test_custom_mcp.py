"""Unit tests for custom_mcp.py — bearer-token MCP connections."""

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Stub httpx so the vault module can be imported without the package installed
# in the test runner (it's available in the vault service but not globally).
# ---------------------------------------------------------------------------
if "httpx" not in sys.modules:
    mock_httpx = types.ModuleType("httpx")
    mock_httpx.AsyncClient = MagicMock
    mock_httpx.Response = MagicMock
    sys.modules["httpx"] = mock_httpx

sys.path.insert(0, "services/vault")
import custom_mcp  # noqa: E402


def _make_response(json_body=None, sse_lines=None,
                   status_code=200, content_type="application/json",
                   headers=None):
    """Build a fake httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    headers_d = {"content-type": content_type}
    if headers:
        headers_d.update(headers)
    resp.headers = headers_d
    if json_body is not None:
        resp.content = json.dumps(json_body).encode()
        resp.json.return_value = json_body
        resp.text = json.dumps(json_body)
    elif sse_lines is not None:
        resp.text = "\n".join(sse_lines)
        resp.content = resp.text.encode()
        resp.json.side_effect = ValueError("not json")
    else:
        resp.content = b""
        resp.text = ""
    return resp


class TestParseResponse(unittest.TestCase):

    def test_json_response_parsed(self):
        body = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        resp = _make_response(json_body=body)
        result = custom_mcp._parse_mcp_response(resp)
        self.assertEqual(result["result"], {"ok": True})

    def test_sse_last_result_returned(self):
        sse = [
            "data: " + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            "data: ",
            "data: " + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"answer": 42}}),
        ]
        resp = _make_response(sse_lines=sse, content_type="text/event-stream")
        result = custom_mcp._parse_mcp_response(resp)
        self.assertEqual(result["result"]["answer"], 42)

    def test_202_returns_none(self):
        resp = _make_response(status_code=202)
        resp.content = b""
        self.assertIsNone(custom_mcp._parse_mcp_response(resp))

    def test_unknown_content_type_raises(self):
        resp = _make_response(content_type="application/octet-stream")
        resp.content = b"binary"
        resp.text = "binary"
        with self.assertRaises(custom_mcp.CustomMCPError):
            custom_mcp._parse_mcp_response(resp)


class TestValidateServerUrl(unittest.TestCase):

    def test_https_ok(self):
        custom_mcp._validate_server_url("https://app.vowsok.com/api/mcp")  # no raise

    def test_http_rejected(self):
        with self.assertRaises(custom_mcp.CustomMCPError):
            custom_mcp._validate_server_url("http://app.vowsok.com/api/mcp")

    def test_empty_rejected(self):
        with self.assertRaises(custom_mcp.CustomMCPError):
            custom_mcp._validate_server_url("")

    def test_no_host_rejected(self):
        with self.assertRaises(custom_mcp.CustomMCPError):
            custom_mcp._validate_server_url("https:///api/mcp")


class TestListTools(unittest.TestCase):

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_client(self, init_body, tools_body):
        """Build a mock async HTTP client that returns init_body then tools_body."""
        init_resp = _make_response(json_body=init_body, headers={"mcp-session-id": "sess123"})
        notif_resp = _make_response(status_code=202)
        notif_resp.content = b""
        tools_resp = _make_response(json_body=tools_body)

        client = AsyncMock()
        client.post = AsyncMock(side_effect=[init_resp, notif_resp, tools_resp])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    def test_list_tools_returns_tool_list(self):
        init_body = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
        tools_body = {
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "tools": [
                    {"name": "search", "description": "Search for stuff",
                     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                    {"name": "create_task", "description": "Create a task",
                     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}},
                                     "required": ["title"]}},
                ]
            },
        }
        client = self._make_client(init_body, tools_body)
        with patch("httpx.AsyncClient", return_value=client):
            tools = self._run(custom_mcp.list_tools(
                "https://app.vowsok.com/api/mcp", "test-token"))
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["name"], "search")
        self.assertEqual(tools[1]["name"], "create_task")

    def test_list_tools_401_raises(self):
        resp_401 = _make_response(status_code=401)
        resp_401.content = b"Unauthorized"
        client = AsyncMock()
        client.post = AsyncMock(return_value=resp_401)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(custom_mcp.CustomMCPError) as ctx:
                self._run(custom_mcp.list_tools(
                    "https://example.com/api/mcp", "bad-token"))
        self.assertIn("401", str(ctx.exception))

    def test_list_tools_validates_url(self):
        with self.assertRaises(custom_mcp.CustomMCPError):
            self._run(custom_mcp.list_tools("http://insecure.com/mcp", "tok"))

    def test_list_tools_unexpected_shape_raises(self):
        init_body = {"jsonrpc": "2.0", "id": 1, "result": {}}
        bad_tools_body = {"jsonrpc": "2.0", "id": 2, "result": {"tools": "not-a-list"}}
        client = self._make_client(init_body, bad_tools_body)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(custom_mcp.CustomMCPError):
                self._run(custom_mcp.list_tools("https://x.com/mcp", "tok"))


class TestCallTool(unittest.TestCase):

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_client(self, init_body, call_body):
        init_resp = _make_response(json_body=init_body)
        notif_resp = _make_response(status_code=202)
        notif_resp.content = b""
        call_resp = _make_response(json_body=call_body)
        client = AsyncMock()
        client.post = AsyncMock(side_effect=[init_resp, notif_resp, call_resp])
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        return client

    def test_call_tool_returns_result(self):
        init_body = {"jsonrpc": "2.0", "id": 1, "result": {}}
        call_body = {
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "content": [{"type": "text", "text": "Task created"}],
                "isError": False,
            },
        }
        client = self._make_client(init_body, call_body)
        with patch("httpx.AsyncClient", return_value=client):
            result = self._run(custom_mcp.call_tool(
                "https://app.vowsok.com/api/mcp", "tok", "create_task",
                {"title": "Hello"}))
        self.assertFalse(result.get("isError"))
        self.assertEqual(result["content"][0]["text"], "Task created")

    def test_call_tool_isError_raises(self):
        init_body = {"jsonrpc": "2.0", "id": 1, "result": {}}
        call_body = {
            "jsonrpc": "2.0", "id": 2,
            "result": {
                "content": [{"type": "text", "text": "Permission denied"}],
                "isError": True,
            },
        }
        client = self._make_client(init_body, call_body)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(custom_mcp.CustomMCPError) as ctx:
                self._run(custom_mcp.call_tool(
                    "https://app.vowsok.com/api/mcp", "tok", "create_task",
                    {"title": "Oops"}))
        self.assertIn("Permission denied", str(ctx.exception))

    def test_call_tool_mcp_error_raises(self):
        init_body = {"jsonrpc": "2.0", "id": 1, "result": {}}
        call_body = {
            "jsonrpc": "2.0", "id": 2,
            "error": {"code": -32600, "message": "Invalid request"},
        }
        client = self._make_client(init_body, call_body)
        with patch("httpx.AsyncClient", return_value=client):
            with self.assertRaises(custom_mcp.CustomMCPError) as ctx:
                self._run(custom_mcp.call_tool(
                    "https://app.vowsok.com/api/mcp", "tok", "bad_tool", {}))
        self.assertIn("Invalid request", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
