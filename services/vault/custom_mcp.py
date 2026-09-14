"""Custom MCP bearer-token connections for the Open Manus Key Vault.

Handles MCP servers that accept static bearer tokens (e.g. Vowsok at
https://app.vowsok.com/api/mcp).  Uses the same JSON-RPC / Streamable-HTTP
MCP protocol sequence as replit_mcp.py but authenticates with a static
bearer token stored encrypted in the vault rather than OAuth.

Vowsok's remote MCP handler maps the advertised ``name`` field to its HTTP
backend's ``fullName`` field; the vault must not perform that mapping.

SSRF protection: _validate_server_url rejects non-HTTPS schemes, then
resolves the hostname and rejects loopback, private, link-local, reserved,
and multicast IP addresses so the vault cannot be weaponised to reach
internal services.

Redis key for the cached tool manifest (written by sync_tools):
  vault:conn:{conn_id}:mcp_tools   JSON list of MCP tool dicts
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("vault.custom_mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"
# Only HTTPS MCP endpoints are permitted.
_ALLOWED_SCHEMES = {"https"}
_DIAGNOSTICS_ENV = "VAULT_MCP_DIAGNOSTICS"
_DIAGNOSTIC_MAX_BYTES = 2048
_CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Only known Vowsok create_client fields are described.  Values and unknown
# field names are never logged; ``notes`` is included because it is an
# advertised optional create_client field.
_SHAPE_FIELDS = ("name", "fullName", "email", "phone", "status", "notes")


class CustomMCPError(Exception):
    pass


class MCPTokenExpiredError(CustomMCPError):
    """Raised when the upstream MCP server rejects the bearer token with a 401.

    Distinct from the base CustomMCPError so callers (app.py) can return a
    structured, agent-readable 401 rather than a generic 502.
    """
    pass


def _is_ip_safe(addr: str) -> bool:
    """Return False if *addr* is a loopback/private/link-local/reserved IP."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False  # can't parse → treat as unsafe
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_server_url(url: str) -> None:
    """Raise CustomMCPError if the URL is not a safe public HTTPS endpoint.

    Checks performed in order:
      1. Non-empty
      2. HTTPS scheme only
      3. Has a hostname
      4. DNS-resolved IPs are all public (no loopback / private / link-local /
         reserved / multicast addresses)
    """
    if not url:
        raise CustomMCPError("MCP server URL is not configured")
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise CustomMCPError(
            f"MCP server URL must use HTTPS (got scheme {parts.scheme!r})")
    hostname = parts.hostname
    if not hostname:
        raise CustomMCPError("MCP server URL has no host")
    # Resolve the hostname and verify every returned IP is public.
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise CustomMCPError(
            f"MCP server hostname {hostname!r} could not be resolved: {exc}"
        )
    if not results:
        raise CustomMCPError(
            f"MCP server hostname {hostname!r} returned no addresses")
    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        if not _is_ip_safe(ip_str):
            raise CustomMCPError(
                f"MCP server URL resolves to a non-public IP ({ip_str}) — "
                "only public internet endpoints are permitted"
            )


def _mcp_headers(bearer_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }


def _parse_mcp_response(resp: httpx.Response) -> Optional[Dict[str, Any]]:
    """Parse a Streamable HTTP response (JSON or SSE) into the JSON-RPC msg."""
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
    if resp.status_code == 202 or not resp.content:
        return None
    if ctype == "application/json":
        return resp.json()
    if ctype == "text/event-stream":
        last = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    last = msg
        return last
    raise CustomMCPError(
        f"Unexpected MCP response type {ctype!r} "
        f"(HTTP {resp.status_code}): {resp.text[:200]}"
    )


def _trace_number(value: Any) -> Optional[int]:
    """Keep only bounded integer protocol/status codes in an internal trace."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if abs(value) <= 1_000_000_000 else None


def _trace_update(trace: Optional[Dict[str, Any]], stage: str,
                  response: Optional[httpx.Response] = None,
                  rpc_code: Any = None) -> None:
    if trace is None:
        return
    trace["stage"] = stage
    status = _trace_number(getattr(response, "status_code", None))
    if status is not None and 100 <= status <= 599:
        trace["http_status"] = status
    code = _trace_number(rpc_code)
    if code is not None:
        trace["rpc_code"] = code


async def _mcp_session(server_url: str, bearer_token: str,
                       rpc_id: int, method: str,
                       params: Optional[Dict[str, Any]],
                       timeout: float = 60.0,
                       trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run initialize, initialized notification, and one MCP request."""
    headers = _mcp_headers(bearer_token)
    async with httpx.AsyncClient(timeout=timeout) as client:
        _trace_update(trace, "mcp_initialize")
        init_resp = await client.post(server_url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "open-manus-vault", "version": "1.0"},
            },
        })
        _trace_update(trace, "mcp_initialize", init_resp)
        if init_resp.status_code == 401:
            raise MCPTokenExpiredError(
                "MCP server rejected the bearer token (401). "
                "Update the token in the vault dashboard."
            )
        if init_resp.status_code >= 400:
            raise CustomMCPError(
                f"MCP initialize failed (HTTP {init_resp.status_code}): "
                f"{init_resp.text[:200]}"
            )
        init_msg = _parse_mcp_response(init_resp)
        if init_msg and init_msg.get("error"):
            _trace_update(
                trace, "mcp_initialize",
                rpc_code=(init_msg["error"].get("code")
                          if isinstance(init_msg["error"], dict) else None),
            )
            raise CustomMCPError(
                f"MCP initialize error: {json.dumps(init_msg['error'])[:200]}"
            )
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        _trace_update(trace, "mcp_notification")
        await client.post(server_url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })

        body: Dict[str, Any] = {
            "jsonrpc": "2.0", "id": rpc_id, "method": method,
        }
        if params is not None:
            body["params"] = params
        _trace_update(trace, "mcp_call")
        resp = await client.post(server_url, headers=headers, json=body)

    _trace_update(trace, "mcp_call", resp)
    if resp.status_code == 401:
        raise MCPTokenExpiredError(
            "MCP server rejected the bearer token on the tool call (401). "
            "Update the token in the vault dashboard."
        )
    msg = _parse_mcp_response(resp)
    if not msg:
        raise CustomMCPError(f"Empty MCP response (HTTP {resp.status_code})")
    if msg.get("error"):
        err = msg["error"]
        _trace_update(
            trace, "mcp_call",
            rpc_code=(err.get("code") if isinstance(err, dict) else None),
        )
        raise CustomMCPError(
            f"MCP error ({err.get('code', '?')}): "
            f"{err.get('message', json.dumps(err)[:200])}"
        )
    return msg


def _is_vowsok_create_client(server_url: str, tool_name: str) -> bool:
    try:
        host = (urlsplit(server_url).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return False
    return host == "app.vowsok.com" and tool_name == "create_client"


def _correlation_id(value: Optional[str] = None) -> str:
    value = str(value or "")
    return value if _CID_RE.fullmatch(value) else uuid.uuid4().hex


def _shape(arguments: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Describe allowlisted field presence/types without values or keys."""
    def _type(value: Any) -> str:
        return {
            type(None): "null", bool: "boolean", str: "string", int: "integer",
            float: "number", list: "array", dict: "object",
        }.get(type(value), "other")

    return {
        field: {"present": field in arguments, **(
            {"type": _type(arguments[field])} if field in arguments else {}
        )}
        for field in _SHAPE_FIELDS
    }


def _diagnostic(
    *, cid: str, stage: str, status: str, code: str,
    started: float, arguments: Optional[Dict[str, Any]] = None,
    mapping_decision: Optional[str] = None,
    trace: Optional[Dict[str, Any]] = None,
) -> None:
    """Log only bounded Vowsok create_client diagnostics when opted in."""
    if os.getenv(_DIAGNOSTICS_ENV, "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return
    event: Dict[str, Any] = {
        "event": "mcp_diagnostic",
        "correlation_id": cid,
        "stage": stage,
        "status": status,
        "code": code,
        "duration_ms": max(
            0, min(3_600_000, int((time.monotonic() - started) * 1000))
        ),
    }
    if mapping_decision in {"preserve_name", "reject_fullName"}:
        event["mapping_decision"] = mapping_decision
    if trace:
        for key in ("http_status", "rpc_code"):
            value = _trace_number(trace.get(key))
            if value is not None and (
                key == "rpc_code" or 100 <= value <= 599
            ):
                event[key] = value
    if arguments is not None:
        event["fields"] = _shape(arguments)
    encoded = json.dumps(event, separators=(",", ":"), sort_keys=True)
    # Current fields are bounded; retain a hard ceiling if this shape grows.
    if len(encoded.encode()) > _DIAGNOSTIC_MAX_BYTES:
        keep = ("event", "correlation_id", "stage", "status", "code",
                "duration_ms")
        encoded = json.dumps(
            {key: event[key] for key in keep}, separators=(",", ":"))
    logger.info("%s", encoded[:_DIAGNOSTIC_MAX_BYTES])


def _vowsok_error(
    message: str, *, cid: str, stage: str, code: str,
) -> CustomMCPError:
    """Create a fixed agent-facing error and attach diagnostic metadata."""
    error = CustomMCPError(
        f"{message} (stage={stage}, code={code}, correlation_id={cid}); "
        "the downstream MCP-to-API bridge is not observable from the vault"
    )
    # Keep CustomMCPError's public API unchanged while exposing metadata.
    error.correlation_id = cid
    error.stage = stage
    error.code = code
    return error


def _vowsok_failure(message: str, *, cid: str, started: float,
                    trace: Dict[str, Any], code: str,
                    stage: Optional[str] = None) -> CustomMCPError:
    stage = stage or trace.get("stage", "mcp_call")
    _diagnostic(
        cid=cid, stage=stage, status="error", code=code,
        started=started, trace=trace,
    )
    return _vowsok_error(message, cid=cid, stage=stage, code=code)


def _is_timeout(exc: BaseException) -> bool:
    timeout_type = getattr(httpx, "TimeoutException", ())
    return isinstance(
        exc,
        tuple(
            kind for kind in (asyncio.TimeoutError, timeout_type)
            if isinstance(kind, type)
        ),
    )


async def list_tools(server_url: str, bearer_token: str) -> List[Dict[str, Any]]:
    """Call tools/list against *server_url* using *bearer_token*."""
    _validate_server_url(server_url)
    msg = await _mcp_session(
        server_url, bearer_token,
        rpc_id=2, method="tools/list", params=None,
        timeout=30.0,
    )
    result = msg.get("result") or {}
    tools = result.get("tools") or []
    if not isinstance(tools, list):
        raise CustomMCPError(
            f"tools/list returned unexpected shape: {json.dumps(result)[:200]}"
        )
    return tools


async def call_tool(server_url: str, bearer_token: str,
                    tool_name: str, arguments: Dict[str, Any],
                    timeout: float = 120.0,
                    correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """Call *tool_name* with *arguments* via MCP tools/call."""
    vowsok = _is_vowsok_create_client(server_url, tool_name)
    cid = _correlation_id(correlation_id) if vowsok else ""
    trace: Optional[Dict[str, Any]] = {} if vowsok else None
    started = time.monotonic()
    try:
        _validate_server_url(server_url)
    except Exception:
        if vowsok:
            raise _vowsok_failure(
                "Vowsok create_client URL validation failed", cid=cid,
                started=started, trace=trace, code="invalid_url",
                stage="validate_url",
            ) from None
        raise

    if vowsok and not isinstance(arguments, dict):
        raise _vowsok_failure(
            "Vowsok create_client arguments must be a JSON object",
            cid=cid, started=started, trace=trace, code="invalid_arguments",
            stage="validate_arguments",
        ) from None
    if vowsok and "fullName" in arguments:
        _diagnostic(
            cid=cid, stage="validate_arguments", status="error",
            code="alias_rejected", started=started, arguments=arguments,
            mapping_decision="reject_fullName", trace=trace,
        )
        raise _vowsok_error(
            "create_client fullName is not advertised; use name only",
            cid=cid, stage="validate_arguments", code="alias_rejected",
        )

    wire_arguments = dict(arguments) if vowsok else arguments
    if vowsok:
        _diagnostic(
            cid=cid, stage="validate_arguments", status="started",
            code="request_started", started=started, arguments=arguments,
            mapping_decision="preserve_name", trace=trace,
        )
    session_kwargs: Dict[str, Any] = {
        "rpc_id": 2, "method": "tools/call",
        "params": {"name": tool_name, "arguments": wire_arguments},
        "timeout": timeout,
    }
    if trace is not None:
        session_kwargs["trace"] = trace
    try:
        msg = await _mcp_session(
            server_url, bearer_token, **session_kwargs,
        )
    except MCPTokenExpiredError:
        if not vowsok:
            raise
        stage = trace.get("stage", "mcp_call")
        _diagnostic(
            cid=cid, stage=stage, status="error", code="token_expired",
            started=started, trace=trace,
        )
        raise MCPTokenExpiredError(
            f"Vowsok MCP call rejected the bearer token (401) "
            f"(stage={stage}, "
            f"code=token_expired, correlation_id={cid})"
        ) from None
    except CustomMCPError:
        if not vowsok:
            raise
        code = (
            "mcp_invalid_request"
            if trace.get("rpc_code") == -32602
            else "mcp_error"
        )
        raise _vowsok_failure(
            "Vowsok create_client MCP request failed", cid=cid,
            started=started, trace=trace, code=code,
        ) from None
    except Exception as exc:
        if not vowsok:
            raise
        code = "upstream_timeout" if _is_timeout(exc) else "transport_error"
        message = (
            "Vowsok create_client request timed out without a retry"
            if code == "upstream_timeout"
            else "Vowsok create_client transport request failed"
        )
        raise _vowsok_failure(
            message, cid=cid, started=started, trace=trace, code=code,
        ) from None

    raw_result = msg.get("result")
    if raw_result is not None and not isinstance(raw_result, dict):
        if vowsok:
            raise _vowsok_failure(
                "Vowsok create_client returned an invalid MCP result",
                cid=cid, started=started, trace=trace,
                code="invalid_result", stage="result",
            )
        raise CustomMCPError("MCP tool returned an unexpected result shape")
    result = raw_result or {}
    if result.get("isError"):
        if vowsok:
            raise _vowsok_failure(
                "Vowsok create_client tool reported an error",
                cid=cid, started=started, trace=trace,
                code="mcp_tool_error", stage="result",
            )
        texts = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict)
        ]
        raise CustomMCPError(f"MCP tool error: {' '.join(texts)[:300]}")
    if vowsok:
        _diagnostic(
            cid=cid, stage="result", status="success",
            code="ok", started=started, trace=trace,
        )
    return result