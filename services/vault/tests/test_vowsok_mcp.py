"""Transport-level contract tests for Vowsok's create_client MCP tool.

The Vowsok MCP server advertises ``name`` (required) and
``additionalProperties: false``.  Its remote handler, not this vault, maps
``name`` to the HTTP backend's ``fullName`` field.  These tests model that
observable transport contract and deliberately do not claim to instrument the
remote MCP-to-API bridge.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import custom_mcp  # noqa: E402


VOWSOK_URL = "https://app.vowsok.com/api/mcp"
CREATE_CLIENT_FIELDS = {"name", "email", "phone", "status", "notes"}


class FakeResponse:
    def __init__(
        self,
        body: Optional[Dict[str, Any]] = None,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
    ):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if body is None:
            self.content = b""
            self.text = ""
        elif content_type == "application/json":
            self.content = json.dumps(body).encode()
            self.text = json.dumps(body)
        else:
            self.text = str(body)
            self.content = self.text.encode()
        self._body = body

    def json(self):
        return self._body


class VowsokTransport:
    """Small in-memory MCP transport with the advertised schema contract."""

    def __init__(
        self,
        *,
        call_result: Optional[Dict[str, Any]] = None,
        call_error: Optional[BaseException] = None,
        init_status: int = 200,
        call_rpc_code: Optional[int] = None,
    ):
        self.calls = []
        self.backend_payload = None
        self.call_result = call_result or {
            "content": [{"type": "text", "text": "created"}],
            "isError": False,
        }
        self.call_error = call_error
        self.init_status = init_status
        self.call_rpc_code = call_rpc_code

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        method = json.get("method")
        if method == "initialize":
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"protocolVersion": custom_mcp.MCP_PROTOCOL_VERSION},
                },
                status_code=self.init_status,
            )
        if method == "notifications/initialized":
            return FakeResponse(status_code=202)
        if method != "tools/call":
            raise AssertionError(f"unexpected MCP method: {method}")
        if self.call_error is not None:
            raise self.call_error
        if self.call_rpc_code is not None:
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {
                        "code": self.call_rpc_code,
                        "message": "Required at name",
                    },
                },
            )

        params = json["params"]
        if params["name"] != "create_client":
            return FakeResponse(
                {"jsonrpc": "2.0", "id": 2, "result": self.call_result},
            )
        arguments = params["arguments"]

        # This is the live contract: name is required and unknown properties
        # (including fullName) are rejected by the advertised schema.
        if (
            "name" not in arguments
            or not set(arguments).issubset(CREATE_CLIENT_FIELDS)
        ):
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {
                        "code": -32602,
                        "message": "Required at name",
                    },
                },
            )

        # The remote Vowsok handler owns this mapping.  Keeping it here makes
        # the regression test verify the final backend payload without
        # pretending the vault can observe or perform that bridge.
        self.backend_payload = {
            **arguments,
            "fullName": arguments["name"],
        }
        return FakeResponse(
            {"jsonrpc": "2.0", "id": 2, "result": self.call_result},
        )


class AsyncClientFactory:
    def __init__(self, transport: VowsokTransport):
        self.transport = transport
        self.timeout = None

    def __call__(self, *, timeout):
        self.timeout = timeout
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, *, headers, json):
        return await self.transport.post(url, headers=headers, json=json)


def _run(coro):
    return asyncio.run(coro)


def _install_transport(monkeypatch, transport):
    monkeypatch.setattr(custom_mcp, "_validate_server_url", lambda *args: None)
    monkeypatch.setattr(
        custom_mcp.httpx,
        "AsyncClient",
        AsyncClientFactory(transport),
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "Transport Test"},
        {"name": "Transport Test", "phone": "5550100"},
        {"name": "Transport Test", "phone": "5550100", "status": "lead"},
    ],
)
def test_create_client_name_contract_reaches_backend(monkeypatch, arguments):
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)
    original = dict(arguments)

    result = _run(
        custom_mcp.call_tool(
            VOWSOK_URL,
            "test-token",
            "create_client",
            arguments,
            correlation_id="contract-name",
        )
    )

    assert result["isError"] is False
    call = next(
        entry for entry in transport.calls
        if entry["json"].get("method") == "tools/call"
    )
    assert call["json"]["params"]["arguments"] == original
    assert "name" in call["json"]["params"]["arguments"]
    assert "fullName" not in call["json"]["params"]["arguments"]
    assert transport.backend_payload == {
        **original,
        "fullName": original["name"],
    }
    assert arguments == original


@pytest.mark.parametrize(
    "arguments",
    [
        {"fullName": "Unadvertised Alias"},
        {"name": "Name", "fullName": "Conflicting Alias"},
        {"name": "Name", "fullName": "Name"},
    ],
)
def test_full_name_alias_is_rejected_before_any_transport_write(
    monkeypatch, arguments
):
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)
    original = dict(arguments)

    with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
        _run(
            custom_mcp.call_tool(
                VOWSOK_URL,
                "test-token",
                "create_client",
                arguments,
                correlation_id="reject-alias",
            )
        )

    assert "not advertised" in str(exc_info.value)
    assert exc_info.value.stage == "validate_arguments"
    assert exc_info.value.correlation_id == "reject-alias"
    assert transport.calls == []
    assert arguments == original


def test_transport_rejects_missing_name_using_advertised_contract(monkeypatch):
    """A name-less call reaches schema validation, not a local alias rewrite."""
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)

    with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
        _run(
            custom_mcp.call_tool(
                VOWSOK_URL,
                "test-token",
                "create_client",
                {"phone": "5550100"},
            )
        )

    assert exc_info.value.code == "mcp_invalid_request"
    assert "Required at name" not in str(exc_info.value)
    call = next(
        entry for entry in transport.calls
        if entry["json"].get("method") == "tools/call"
    )
    assert call["json"]["params"]["arguments"] == {"phone": "5550100"}


def test_other_tools_and_hosts_are_not_rewritten(monkeypatch):
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)
    # The transport fixture is intentionally strict only for create_client;
    # use a generic MCP result for an unrelated tool.
    transport.call_result = {"content": [], "isError": False}
    arguments = {"name": "Keep As-Is", "fullName": "Also Keep"}
    _run(
        custom_mcp.call_tool(
            "https://other.example/api/mcp",
            "test-token",
            "update_client",
            arguments,
        )
    )
    call = next(
        entry for entry in transport.calls
        if entry["json"].get("method") == "tools/call"
    )
    assert call["json"]["params"]["arguments"] == arguments
    assert arguments == {"name": "Keep As-Is", "fullName": "Also Keep"}


def test_timeout_is_sanitized_and_never_retried(monkeypatch):
    transport = VowsokTransport(
        call_error=asyncio.TimeoutError("upstream secret client value")
    )
    _install_transport(monkeypatch, transport)

    with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
        _run(
            custom_mcp.call_tool(
                VOWSOK_URL,
                "test-token",
                "create_client",
                {"name": "Timeout Test"},
                correlation_id="timeout-case",
            )
        )

    assert exc_info.value.code == "upstream_timeout"
    assert exc_info.value.correlation_id == "timeout-case"
    assert "upstream secret client value" not in str(exc_info.value)
    assert sum(
        entry["json"].get("method") == "tools/call"
        for entry in transport.calls
    ) == 1


def test_upstream_tool_error_is_sanitized(monkeypatch):
    transport = VowsokTransport(
        call_result={
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "private client name and backend stack trace",
                }
            ],
        }
    )
    _install_transport(monkeypatch, transport)

    with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
        _run(
            custom_mcp.call_tool(
                VOWSOK_URL,
                "test-token",
                "create_client",
                {"name": "Sanitize Test"},
                correlation_id="sanitize-case",
            )
        )

    assert exc_info.value.code == "mcp_tool_error"
    assert "private client name" not in str(exc_info.value)
    assert "backend stack trace" not in str(exc_info.value)
    assert "downstream MCP-to-API bridge is not observable" in str(
        exc_info.value
    )


def test_opt_in_diagnostics_are_bounded_and_redacted(monkeypatch, caplog):
    monkeypatch.setenv(custom_mcp._DIAGNOSTICS_ENV, "1")
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)
    arguments = {
        "name": "Sensitive Client Name",
        "email": "sensitive@example.test",
        "phone": "5550100",
        "secret_field": "must-not-appear",
    }

    with caplog.at_level(logging.INFO, logger=custom_mcp.logger.name):
        with pytest.raises(custom_mcp.CustomMCPError):
            _run(
                custom_mcp.call_tool(
                    VOWSOK_URL,
                    "test-token",
                    "create_client",
                    arguments,
                    correlation_id="diag-case",
                )
            )

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == custom_mcp.logger.name
    ]
    assert events
    allowed_event_keys = {
        "event",
        "correlation_id",
        "stage",
        "status",
        "code",
        "duration_ms",
        "http_status",
        "fields",
        "mapping_decision",
        "rpc_code",
    }
    for event in events:
        assert set(event).issubset(allowed_event_keys)
        assert event["correlation_id"] == "diag-case"
        assert len(json.dumps(event).encode()) <= custom_mcp._DIAGNOSTIC_MAX_BYTES
        assert "Sensitive Client Name" not in json.dumps(event)
        assert "sensitive@example.test" not in json.dumps(event)
        assert "secret_field" not in json.dumps(event)
    argument_event = next(event for event in events if "fields" in event)
    assert any(event.get("mapping_decision") == "preserve_name"
               for event in events)
    assert argument_event["fields"]["name"] == {
        "present": True,
        "type": "string",
    }
    assert argument_event["fields"]["phone"]["type"] == "string"


def test_diagnostic_mapping_decision_reject_full_name(monkeypatch, caplog):
    monkeypatch.setenv(custom_mcp._DIAGNOSTICS_ENV, "1")
    transport = VowsokTransport()
    _install_transport(monkeypatch, transport)

    with caplog.at_level(logging.INFO, logger=custom_mcp.logger.name):
        with pytest.raises(custom_mcp.CustomMCPError):
            _run(
                custom_mcp.call_tool(
                    VOWSOK_URL,
                    "test-token",
                    "create_client",
                    {"name": "Name", "fullName": "Alias"},
                    correlation_id="mapping-case",
                )
            )

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == custom_mcp.logger.name
    ]
    assert any(event.get("mapping_decision") == "reject_fullName"
               for event in events)


def test_init_diagnostic_keeps_init_stage_and_http_status(monkeypatch, caplog):
    monkeypatch.setenv(custom_mcp._DIAGNOSTICS_ENV, "1")
    transport = VowsokTransport(init_status=503)
    _install_transport(monkeypatch, transport)

    with caplog.at_level(logging.INFO, logger=custom_mcp.logger.name):
        with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
            _run(
                custom_mcp.call_tool(
                    VOWSOK_URL,
                    "test-token",
                    "create_client",
                    {"name": "Init Failure"},
                    correlation_id="init-case",
                )
            )

    assert exc_info.value.stage == "mcp_initialize"
    assert "code=mcp_error" in str(exc_info.value)
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == custom_mcp.logger.name
    ]
    failure = next(event for event in events if event["status"] == "error")
    assert failure["stage"] == "mcp_initialize"
    assert failure["http_status"] == 503
    assert "rpc_code" not in failure


def test_call_diagnostic_keeps_call_stage_and_rpc_code(monkeypatch, caplog):
    monkeypatch.setenv(custom_mcp._DIAGNOSTICS_ENV, "1")
    transport = VowsokTransport(call_rpc_code=-32602)
    _install_transport(monkeypatch, transport)

    with caplog.at_level(logging.INFO, logger=custom_mcp.logger.name):
        with pytest.raises(custom_mcp.CustomMCPError) as exc_info:
            _run(
                custom_mcp.call_tool(
                    VOWSOK_URL,
                    "test-token",
                    "create_client",
                    {"name": "Call Failure"},
                    correlation_id="call-case",
                )
            )

    assert exc_info.value.stage == "mcp_call"
    assert "code=mcp_invalid_request" in str(exc_info.value)
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == custom_mcp.logger.name
    ]
    failure = next(event for event in events if event["status"] == "error")
    assert failure["stage"] == "mcp_call"
    assert failure["http_status"] == 200
    assert failure["rpc_code"] == -32602