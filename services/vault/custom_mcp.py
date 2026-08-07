"""Custom MCP bearer-token connections for the Open Manus Key Vault.

Handles MCP servers that accept static bearer tokens (e.g. Vowsok at
https://app.vowsok.com/api/mcp).  Uses the same JSON-RPC / Streamable-HTTP
MCP protocol sequence as replit_mcp.py but authenticates with a static
bearer token stored encrypted in the vault rather than OAuth.

SSRF protection: _validate_server_url rejects non-HTTPS schemes, then
resolves the hostname and rejects loopback, private, link-local, reserved,
and multicast IP addresses so the vault cannot be weaponised to reach
internal services.

Redis key for the cached tool manifest (written by sync_tools):
  vault:conn:{conn_id}:mcp_tools   JSON list of MCP tool dicts
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("vault.custom_mcp")

MCP_PROTOCOL_VERSION = "2025-03-26"

# Only HTTPS MCP endpoints are permitted.
_ALLOWED_SCHEMES = {"https"}


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


async def _mcp_session(server_url: str, bearer_token: str,
                       rpc_id: int, method: str,
                       params: Optional[Dict[str, Any]],
                       timeout: float = 60.0) -> Dict[str, Any]:
    """
    Run the MCP session handshake (initialize → notifications/initialized)
    and then call *method* with *params*, returning the JSON-RPC message.
    """
    headers = _mcp_headers(bearer_token)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # 1. initialize
        init_resp = await client.post(server_url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "open-manus-vault", "version": "1.0"},
            },
        })
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
            raise CustomMCPError(
                f"MCP initialize error: {json.dumps(init_msg['error'])[:200]}"
            )
        # Carry the session id if the server issued one.
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # 2. notifications/initialized (fire-and-forget, ignore response)
        await client.post(server_url, headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"
        })

        # 3. The actual call.
        body: Dict[str, Any] = {
            "jsonrpc": "2.0", "id": rpc_id, "method": method,
        }
        if params is not None:
            body["params"] = params
        resp = await client.post(server_url, headers=headers, json=body)

    # Guard: a 401 on the actual call means the token was accepted during
    # initialize but rejected later (e.g. mid-session expiry or per-tool ACL).
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
        raise CustomMCPError(
            f"MCP error ({err.get('code', '?')}): "
            f"{err.get('message', json.dumps(err)[:200])}"
        )
    return msg


async def list_tools(server_url: str, bearer_token: str) -> List[Dict[str, Any]]:
    """
    Call tools/list against *server_url* using *bearer_token*.

    Returns a list of MCP tool dicts, each with at least:
      {name, description, inputSchema}
    """
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
                    timeout: float = 120.0) -> Dict[str, Any]:
    """
    Call *tool_name* with *arguments* via MCP tools/call.

    Returns the MCP result dict (content list, isError flag, etc.).
    """
    _validate_server_url(server_url)
    msg = await _mcp_session(
        server_url, bearer_token,
        rpc_id=2, method="tools/call",
        params={"name": tool_name, "arguments": arguments},
        timeout=timeout,
    )
    result = msg.get("result") or {}
    if result.get("isError"):
        texts = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict)
        ]
        raise CustomMCPError(f"MCP tool error: {' '.join(texts)[:300]}")
    return result
