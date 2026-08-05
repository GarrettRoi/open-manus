"""Vault-backed native tools.

Turns every vault connection the agent has been granted into a native
tool in the registry (one tool per connection, e.g. ``vault_openai``),
so the model can pick external APIs like any built-in tool instead of
having to remember the vault_client skill.

Architecture:
    - A static ``vault`` meta-tool (registered at module level so
      discover_builtin_tools() picks this file up) exposes ``list`` /
      ``request_access`` / ``refresh`` actions.
    - At import time we fetch ``/api/vault/list`` from the vault and
      register one ``vault_<connection>`` tool per granted connection
      in the ``vault`` toolset. Each tool is a thin generic HTTP caller:
      the model supplies method/path/params/json/headers and the vault
      proxy attaches the credential server-side. Credentials never enter
      this process.
    - A daemon thread re-syncs grants every VAULT_TOOLS_REFRESH_SECONDS
      (default 300s): new grants appear and revoked grants disappear
      without a redeploy (the registry generation counter invalidates
      the model_tools schema cache automatically).
    - Everything degrades gracefully: no VAULT_TOKEN, or vault
      unreachable, means no per-connection tools and a single log line —
      never a crash. The vault_client skill remains as a fallback path.
"""

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools.registry import registry

logger = logging.getLogger(__name__)

VAULT_URL = os.getenv("VAULT_URL", "http://vault.railway.internal:8080").rstrip("/")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")

TOOLSET = "vault"
TOOL_PREFIX = "vault_"

def _parse_refresh_seconds() -> int:
    raw = os.getenv("VAULT_TOOLS_REFRESH_SECONDS", "300")
    try:
        return max(60, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid VAULT_TOOLS_REFRESH_SECONDS=%r — using default 300s.", raw)
        return 300


_REFRESH_SECONDS = _parse_refresh_seconds()

# connection id (vault-side, e.g. "OPENAI") -> registered tool name
_registered: Dict[str, str] = {}
_registered_lock = threading.Lock()
_refresh_thread: Optional[threading.Thread] = None
_warned_unreachable = False


# ---------------------------------------------------------------------------
# Vault HTTP helpers (kept self-contained; mirrors skills/vault_client)
# ---------------------------------------------------------------------------

def _vault_http(method: str, path: str, payload: Optional[dict] = None,
                timeout: float = 15,
                extra_headers: Optional[dict] = None) -> Any:
    """Raw authenticated request to the vault itself (not the upstream API)."""
    if not VAULT_TOKEN:
        raise RuntimeError("VAULT_TOKEN not set")
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(
        f"{VAULT_URL}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {VAULT_TOKEN}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
            **(extra_headers or {}),
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_connections(background: bool = True) -> Optional[List[dict]]:
    """Return granted connections, or None when the vault is unavailable."""
    global _warned_unreachable
    if not VAULT_TOKEN:
        return None
    try:
        # Background (routine sync) polls are kept out of the vault audit log;
        # agent-initiated lists (background=False) still get audited.
        hdrs = {"X-Vault-Background": "1"} if background else None
        data = _vault_http("GET", "/api/vault/list", extra_headers=hdrs)
        _warned_unreachable = False
        conns = data.get("available_connections") or data.get("available_keys") or []
        return [c for c in conns if isinstance(c, dict) and c.get("id")]
    except Exception as e:
        if not _warned_unreachable:
            _warned_unreachable = True
            logger.warning(
                "Vault unreachable at %s (%s) — per-connection vault tools "
                "unavailable until it comes back; will keep retrying every %ss.",
                VAULT_URL, e, _REFRESH_SECONDS,
            )
        return None


def _proxy_call(conn_id: str, args: dict) -> str:
    """Execute an upstream API call through the vault proxy."""
    method = str(args.get("method") or "GET").upper()
    path = str(args.get("path") or "/")
    if not path.startswith("/"):
        path = "/" + path
    payload: Dict[str, Any] = {
        "method": method,
        "path": path,
        "timeout": min(float(args.get("timeout") or 30), 120),
    }
    for key in ("params", "headers", "json"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            try:
                val = json.loads(val)
            except (ValueError, TypeError):
                return json.dumps({
                    "error": f"'{key}' must be a JSON object; got unparseable string.",
                })
        if val:
            payload[key] = val
    data = args.get("data")
    if data:
        payload["data"] = data if isinstance(data, str) else json.dumps(data)

    try:
        resp = _vault_http(
            "POST", f"/api/vault/proxy/{conn_id}", payload,
            timeout=payload["timeout"] + 15,
        )
        return json.dumps(resp, ensure_ascii=False, default=str)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode() if e.fp else ""
        except Exception:
            pass
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return json.dumps({"error": f"Vault error ({e.code}): {detail}"})
    except URLError as e:
        return json.dumps({
            "error": f"Cannot reach vault at {VAULT_URL}: {e.reason}. "
                     "The vault service may be restarting — try again shortly.",
        })
    except Exception as e:
        return json.dumps({"error": f"Vault call failed: {e}"})


# ---------------------------------------------------------------------------
# Per-connection tool registration
# ---------------------------------------------------------------------------

def _tool_name_for(conn_id: str) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", conn_id.strip().lower()).strip("_")
    return f"{TOOL_PREFIX}{safe or 'unknown'}"


def _build_conn_schema(conn: dict, tool_name: str) -> dict:
    conn_id = conn["id"]
    service = conn.get("service") or conn_id
    label = conn.get("label") or ""
    base_url = conn.get("base_url") or ""
    desc_bits = [
        f"Call the {service} API"
        + (f" ({label})" if label and label.lower() != service.lower() else "")
        + " through the secure vault proxy. Credentials are attached "
          "server-side — never handle or ask for API keys.",
    ]
    if base_url:
        desc_bits.append(f"Base URL: {base_url} (give `path` relative to it).")
    for field in ("description", "skill_description"):
        text = (conn.get(field) or "").strip()
        if text:
            desc_bits.append(text)
    example = (conn.get("example_call") or "").strip()
    if example:
        desc_bits.append(f"Example: {example}")
    description = "\n".join(desc_bits)
    # Keep schema descriptions bounded — some connections carry long skill notes.
    if len(description) > 2000:
        description = description[:2000] + "…"

    return {
        "name": tool_name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
                    "description": "HTTP method for the upstream API.",
                },
                "path": {
                    "type": "string",
                    "description": "Upstream API path relative to the base URL, e.g. '/v1/voices'.",
                },
                "params": {
                    "type": "object",
                    "description": "Query-string parameters (optional).",
                },
                "json": {
                    "type": "object",
                    "description": "JSON request body (optional).",
                },
                "headers": {
                    "type": "object",
                    "description": "Extra request headers (optional; auth headers are set by the vault and cannot be overridden).",
                },
                "data": {
                    "type": "string",
                    "description": "Raw request body for non-JSON payloads (optional).",
                },
                "timeout": {
                    "type": "number",
                    "description": "Upstream timeout in seconds (default 30, max 120).",
                },
            },
            "required": ["method", "path"],
        },
    }


def _make_conn_handler(conn_id: str):
    def _handler(args: dict, **_kw) -> str:
        return _proxy_call(conn_id, args or {})
    return _handler


def _sync_connection_tools() -> Optional[Dict[str, int]]:
    """Fetch grants and reconcile registry entries. Returns change counts."""
    conns = _fetch_connections()
    if conns is None:
        return None  # vault unavailable — keep whatever we have registered

    added = removed = updated = 0
    with _registered_lock:
        seen: Dict[str, dict] = {}
        for conn in conns:
            seen[conn["id"]] = conn
        # Deregister revoked connections
        for conn_id in list(_registered):
            if conn_id not in seen:
                try:
                    registry.deregister(_registered[conn_id])
                except Exception:
                    logger.exception("Failed to deregister vault tool for %s", conn_id)
                del _registered[conn_id]
                removed += 1
        # Register new / re-register changed. Track tool-name claims within
        # this sync so two connection ids that normalize to the same tool
        # name (e.g. "MY-API" and "MY_API") can't overwrite each other's
        # handler or cause the wrong tool to be deregistered on revoke.
        claimed = {name: cid for cid, name in _registered.items()
                   if cid in seen}
        for conn_id, conn in sorted(seen.items()):
            tool_name = _tool_name_for(conn_id)
            owner = claimed.get(tool_name)
            if owner is not None and owner != conn_id:
                logger.warning(
                    "Vault tool name collision: connections %r and %r both "
                    "normalize to tool %r — skipping %r. Rename one "
                    "connection in the vault dashboard.",
                    owner, conn_id, tool_name, conn_id,
                )
                continue
            claimed[tool_name] = conn_id
            schema = _build_conn_schema(conn, tool_name)
            existing = registry.get_entry(tool_name)
            if conn_id in _registered and existing is not None:
                if existing.schema == schema:
                    continue
                updated += 1
            else:
                added += 1
            try:
                registry.register(
                    name=tool_name,
                    toolset=TOOLSET,
                    schema=schema,
                    handler=_make_conn_handler(conn_id),
                    description=f"Vault-proxied access to {conn.get('service') or conn_id}",
                    emoji="🔐",
                )
                _registered[conn_id] = tool_name
            except Exception:
                logger.exception("Failed to register vault tool for %s", conn_id)

    if added or removed or updated:
        logger.info(
            "Vault tools synced: %d added, %d removed, %d updated (%d total).",
            added, removed, updated, len(_registered),
        )
    return {"added": added, "removed": removed, "updated": updated,
            "total": len(_registered)}


def _refresh_loop():
    while True:
        try:
            _sync_connection_tools()
        except Exception:
            logger.exception("Vault tool refresh failed")
        threading.Event().wait(_REFRESH_SECONDS)


def _ensure_refresh_thread():
    global _refresh_thread
    if _refresh_thread is None or not _refresh_thread.is_alive():
        _refresh_thread = threading.Thread(
            target=_refresh_loop, name="vault-tools-refresh", daemon=True,
        )
        _refresh_thread.start()


# ---------------------------------------------------------------------------
# Static `vault` meta-tool
# ---------------------------------------------------------------------------

def check_vault_requirements() -> bool:
    return bool(VAULT_TOKEN)


def vault_meta_handler(args: dict, **_kw) -> str:
    action = (args or {}).get("action") or "list"
    if action == "list":
        conns = _fetch_connections(background=False)
        if conns is None:
            return json.dumps({
                "error": "Vault is unreachable right now. Try again shortly, "
                         "or fall back to the vault_client skill.",
            })
        return json.dumps({
            "connections": [
                {
                    "id": c["id"],
                    "service": c.get("service", ""),
                    "label": c.get("label", ""),
                    "base_url": c.get("base_url", ""),
                    "tool": _tool_name_for(c["id"]),
                    "description": c.get("description", ""),
                }
                for c in conns
            ],
            "note": "Each connection is also available as a native tool "
                    "(see 'tool'). If a tool is missing from your schema, "
                    "call vault(action='refresh') to re-sync grants.",
        }, ensure_ascii=False)
    if action == "request_access":
        service = (args.get("service") or "").strip()
        if not service:
            return json.dumps({"error": "'service' is required for request_access."})
        try:
            resp = _vault_http("POST", "/api/vault/request", {
                "service": service,
                "name": args.get("name") or service,
                "reason": args.get("reason") or "",
            })
            return json.dumps(resp, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"request_access failed: {e}"})
    if action == "refresh":
        result = _sync_connection_tools()
        if result is None:
            return json.dumps({"error": "Vault unreachable — could not refresh."})
        return json.dumps({"refreshed": True, **result})
    return json.dumps({"error": f"Unknown action '{action}'. "
                                "Use list, request_access, or refresh."})


registry.register(
    name="vault",
    toolset=TOOLSET,
    schema={
        "name": "vault",
        "description": (
            "Manage your secure credential vault. Every vault connection you "
            "have been granted is ALSO exposed as its own native vault_<name> "
            "tool — prefer those for actual API calls. Use this tool to: "
            "list your connections (action='list'), ask the owner for access "
            "to a new service (action='request_access'), or re-sync your "
            "grants into native tools without a restart (action='refresh'). "
            "Credentials never leave the vault; you cannot read raw keys."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "request_access", "refresh"],
                },
                "service": {
                    "type": "string",
                    "description": "Service to request access to (request_access).",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable connection name (request_access, optional).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you need it (request_access, optional).",
                },
            },
            "required": ["action"],
        },
    },
    handler=vault_meta_handler,
    check_fn=check_vault_requirements,
    requires_env=["VAULT_TOKEN"],
    description="Vault connection management (list / request access / refresh)",
    emoji="🔐",
)


# Initial sync + background refresh — only when the vault is configured.
if VAULT_TOKEN:
    try:
        _sync_connection_tools()
    except Exception:
        logger.exception("Initial vault tool sync failed")
    _ensure_refresh_thread()
