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
import time
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
# connection id -> extra per-product tool names (Google Workspace suites)
_registered_suite: Dict[str, List[str]] = {}
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


def _special_call(conn_id: str, service: str, args: dict) -> str:
    """Call one of the vault's structured, credential-sensitive adapters."""
    operation = str(args.get("operation") or "").strip()
    if not operation:
        return json.dumps({"error": "operation is required"})
    payload = {
        "operation": operation,
        "args": args.get("args") if isinstance(args.get("args"), dict) else {},
    }
    try:
        return json.dumps(
            _vault_http("POST", f"/api/vault/{service}/{conn_id}", payload,
                        timeout=125),
            ensure_ascii=False, default=str,
        )
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

    if service == "macincloud":
        return {
            "name": tool_name,
            "description": description + (
                "\nOperations:"
                "\n• screenshot — capture desktop → returns image_b64 (PNG). args: {cursor: bool}"
                "\n• run_command — run a shell cmd. args: {command, allow_shell_ops, timeout}"
                "\n• open_browser — open URL. args: {url, app}"
                "\n• applescript — run AppleScript. args: {script, allow_shell}"
                "\n• list_apps — list installed apps. no args needed"
                "\n• key_combo — send keyboard shortcut. args: {keys} e.g. 'command+c'"
                "\n• type_text — type at cursor. args: {text}"
                "\n• focus_app — bring app to front. args: {app}"
                "\n• click_at — click at screen coords. args: {x, y}"
                "\n• right_click_at — right-click at screen coords. args: {x, y}"
                "\n• double_click_at — double-click at screen coords. args: {x, y}"
                "\n• mouse_move — move cursor without clicking. args: {x, y}"
                "\n• scroll — scroll wheel. args: {x, y, scroll_dy (+up/-down lines), scroll_dx}"
                "\n• get_session_url — get the VNC viewer URL to share with Garrett. no args"
                "\n• request_handoff — ask Garrett to take over the desktop."
                " args: {message: 'Please help with this CAPTCHA'}."
                " Returns viewer_url to include in your message to him."
                "\n• return_control — signal to Garrett that he can hand control back. no args"
                "\nCredentials and SSH keys stay in the vault — agents never see them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "screenshot", "run_command", "open_browser",
                            "applescript", "list_apps", "key_combo",
                            "type_text", "focus_app",
                            "click_at", "right_click_at", "double_click_at",
                            "mouse_move", "scroll",
                            "get_session_url", "request_handoff", "return_control",
                        ],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation-specific arguments. "
                            "click_at/right_click_at/double_click_at/mouse_move: {x, y}. "
                            "scroll: {x, y, scroll_dy, scroll_dx}. "
                            "run_command: {command, allow_shell_ops, timeout}. "
                            "open_browser: {url, app}. applescript: {script, allow_shell}. "
                            "key_combo: {keys}. type_text: {text}. focus_app: {app}. "
                            "request_handoff: {message}. screenshot: {cursor}."
                        ),
                    },
                },
                "required": ["operation"],
            },
        }
    if service == "apple":
        return {
            "name": tool_name,
            "description": description + (
                "\nOperations: calendar_list, calendar_search, calendar_create, "
                "calendar_update, calendar_delete, reminders_list, reminders_create, "
                "reminders_complete, contacts_search, contacts_read. Results are "
                "structured and credentials stay in the vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "calendar_list", "calendar_search", "calendar_create",
                            "calendar_update", "calendar_delete", "reminders_list",
                            "reminders_create", "reminders_complete",
                            "contacts_search", "contacts_read",
                        ],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation arguments. Search/list: limit, start, end, "
                            "calendar_url. Create: summary, description, location, "
                            "start/end or due. Update/delete/complete: href and "
                            "optional etag. Contacts: query or href."
                        ),
                    },
                },
                "required": ["operation"],
            },
        }
    if service == "email":
        return {
            "name": tool_name,
            "description": description + (
                "\nOperations: folders, search, read, send. The Notes folder is "
                "read-only and partial because modern iCloud Notes has no public API."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["folders", "search", "read", "send"],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation arguments. Search/read: folder, query, "
                            "limit. Send: to, subject, body."
                        ),
                    },
                },
                "required": ["operation"],
            },
        }
    if str(conn.get("auth_kind") or "") == "amazon":
        return {
            "name": tool_name,
            "description": description + (
                "\nAmazon Associates affiliate connection. Operations:"
                "\n• build_link — build a tagged affiliate link. args: {asin} "
                "or {url} (an Amazon product URL), optional {marketplace}"
                "\n• search_items — PA-API product search. args: {keywords, "
                "ItemCount, Resources}"
                "\n• get_items — PA-API item lookup. args: {asin} or "
                "{item_ids: [...]}, optional {Resources}"
                "\n• get_variations / get_browse_nodes — PA-API passthrough "
                "(PA-API operations need stored PA-API keys; build_link works "
                "with just the tag). All request signing happens in the vault."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["build_link", "search_items", "get_items",
                                 "get_variations", "get_browse_nodes"],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation arguments. build_link: {asin | url, "
                            "marketplace}. search_items: {keywords, ItemCount}. "
                            "get_items: {asin | item_ids}."
                        ),
                    },
                },
                "required": ["operation"],
            },
        }
    if service == "discord_read":
        return {
            "name": tool_name,
            "description": description + (
                "\nREAD-ONLY Discord access via the fleet reader bot (works in "
                "any server the reader bot has been invited to; the vault "
                "rejects every write-style call).\nOperations:"
                "\n• list_servers — servers the reader bot can see. no args"
                "\n• list_channels — channels in a server. args: {guild_id}"
                "\n• read_messages — recent messages, newest first. args: "
                "{channel_id, limit (max 100), before/after/around (message id)}"
                "\n• extract_links — links found in recent messages. args: "
                "{channel_id, limit}"
                "\n• download_attachment — save an attachment locally. args: "
                "{url (from read_messages), save_dir (optional)}"
                "\nNo sending, reacting, or DMs — this connection cannot write."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["list_servers", "list_channels",
                                 "read_messages", "extract_links",
                                 "download_attachment"],
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation arguments. list_channels: {guild_id}. "
                            "read_messages/extract_links: {channel_id, limit, "
                            "before}. download_attachment: {url, save_dir}."
                        ),
                    },
                },
                "required": ["operation"],
            },
        }
    # Google OAuth connections: return a hub schema so agents call this tool
    # with {product, operation, args} instead of confusing it with a generic
    # HTTP proxy.  The per-product tools (vault_<id>_sheets, etc.) are
    # preferred, but this hub lets agents pick the product dynamically.
    auth_kind_raw = str(conn.get("auth_kind") or "")
    if (service or "").lower() == "google" and auth_kind_raw == "oauth2":
        product_list = ", ".join(GOOGLE_PRODUCTS)
        suite_tools = "\n".join(f"• {tool_name}_{p}" for p in GOOGLE_PRODUCTS)
        return {
            "name": tool_name,
            "description": (
                f"Google Workspace hub for the '{conn.get('label') or conn_id}' account.\n"
                f"Prefer the dedicated per-product tools when you know which product you need:\n"
                f"{suite_tools}\n\n"
                f"Use this hub when you need to specify the product dynamically. "
                f"Pass `product` (one of: {product_list}), `operation`, and `args`.\n"
                "OAuth token is attached server-side — never handle credentials.\n"
                + (description if description not in (
                    f"Call the {service} API"
                    + (f" ({conn.get('label', '')})" if conn.get('label', '').lower() != (service or '').lower() else "")
                    + " through the secure vault proxy. Credentials are attached "
                      "server-side — never handle or ask for API keys."
                    + (f"\nBase URL: {base_url} (give `path` relative to it)." if base_url else ""),
                ) else "")
            ).strip(),
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "enum": GOOGLE_PRODUCTS,
                        "description": (
                            "Which Google product to call. One of: " + product_list + ". "
                            "For the dedicated per-product tools you do not need this field."
                        ),
                    },
                    "operation": {
                        "type": "string",
                        "description": (
                            "Which operation to run. Depends on the product — "
                            "see GOOGLE_WORKSPACE_VAULT_SKILL.md for the full list."
                        ),
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Operation-specific arguments. Put all parameters here. "
                            "Example: {\"spreadsheet_id\": \"...\", \"range\": \"A1:B5\"}."
                        ),
                    },
                },
                "required": ["product", "operation"],
            },
        }

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


def _email_call(conn_id: str, args: dict) -> str:
    """Execute an email (IMAP/SMTP) action through the vault."""
    action_map = {
        "list_folders": "folders",
        "list_messages": "list",
        "search_messages": "search",
        "search": "search",
        "read_message": "read",
        "send": "send",
        "download_attachment": "attachment",
    }
    action = str(args.get("action") or "list_messages")
    payload: Dict[str, Any] = {"action": action_map.get(action, action)}
    for key in ("folder", "limit", "unseen_only", "uid", "to", "cc", "bcc",
                "subject", "body", "reply_to", "in_reply_to", "filename",
                "index", "from", "text", "query", "since", "before",
                "last_days", "unseen", "flagged", "has_attachment",
                "attachment_name", "min_size_kb", "max_size_kb"):
        val = args.get(key)
        if val not in (None, "", []):
            payload[key] = val
    # booleans that are meaningful as False too (e.g. has_attachment: false)
    for key in ("unseen", "has_attachment"):
        if args.get(key) is False:
            payload[key] = False
    try:
        resp = _vault_http("POST", f"/api/vault/email/{conn_id}", payload,
                           timeout=120)
        if payload["action"] == "attachment" and isinstance(resp, dict):
            return _save_email_attachment(resp, args)
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
        return json.dumps({"error": f"Email action failed: {e}"})


def _save_email_attachment(resp: Dict[str, Any], args: dict) -> str:
    """Decode a vault attachment response and write the file locally.

    Returns JSON with the saved path so the agent can open/manipulate it.
    """
    import base64
    import re as _re

    att = resp.get("attachment") or {}
    b64 = att.get("content_b64")
    if not b64:
        return json.dumps(resp, ensure_ascii=False, default=str)

    # Sanitize the filename: basename only, printable chars, no traversal.
    raw_name = str(att.get("filename") or "attachment.bin")
    name = os.path.basename(raw_name.replace("\\", "/"))
    name = _re.sub(r"[^\w.\- ()\[\]]", "_", name).strip(". ") or "attachment.bin"

    save_dir = str(args.get("save_dir") or "").strip()
    if save_dir:
        save_dir = os.path.realpath(os.path.expanduser(save_dir))
        home = os.path.realpath(os.path.expanduser("~"))
        if not (save_dir == home or save_dir.startswith(home + os.sep)
                or save_dir.startswith("/tmp/")):
            save_dir = ""  # outside allowed roots — fall back to Downloads
    if not save_dir:
        save_dir = os.path.expanduser("~/Downloads")
    os.makedirs(save_dir, exist_ok=True)

    data = base64.b64decode(b64)
    # Exclusive-create with a retrying suffix: never clobber an earlier
    # download, even for same-second or concurrent saves.
    stem, ext = os.path.splitext(name)
    path = os.path.join(save_dir, name)
    for attempt in range(1, 1000):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            path = os.path.join(save_dir, f"{stem}_{attempt}{ext}")
    else:
        return json.dumps({"error": "Could not find a free filename to save the attachment."})
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return json.dumps({
        "saved": True,
        "path": path,
        "filename": att.get("filename"),
        "content_type": att.get("content_type"),
        "size": len(data),
        "note": "File saved locally — you can now read, process, or re-send it.",
    }, ensure_ascii=False)


def _discord_read_call(conn_id: str, args: dict) -> str:
    """Execute a read-only Discord operation through the vault.

    download_attachment responses carry base64 file content; decode and save
    locally (same flow as email attachments) so the agent gets a file path.
    """
    operation = str(args.get("operation") or "").strip()
    if not operation:
        return json.dumps({"error": "operation is required"})
    inner = args.get("args") if isinstance(args.get("args"), dict) else {}
    payload = {"operation": operation, "args": inner}
    try:
        resp = _vault_http("POST", f"/api/vault/discord/{conn_id}", payload,
                           timeout=90)
        if operation == "download_attachment" and isinstance(resp, dict) \
                and resp.get("attachment"):
            return _save_email_attachment(resp, inner)
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
        return json.dumps({"error": f"Discord read call failed: {e}"})


def _build_email_schema(conn: dict, tool_name: str) -> dict:
    conn_id = conn["id"]
    label = conn.get("label") or conn_id
    address = conn.get("email_address") or ""
    desc = (
        f"Use the {label} mailbox"
        + (f" ({address})" if address else "")
        + " through the secure vault (classic IMAP/SMTP — the vault logs in "
          "server-side; you never see the password). Actions: "
          "search_messages — powerful search combining any of: from, to, cc, "
          "subject, text keywords, since/before dates or last_days, unseen, "
          "flagged, size, has_attachment, attachment_name (substring or * "
          "wildcard). Use minimal criteria, e.g. {from: 'john', last_days: "
          "20} or {attachment_name: 'contract', has_attachment: true}. "
          "Also: list_messages (newest first), read_message (uid), "
          "download_attachment (uid + filename or index — saves the file to "
          "~/Downloads), send (to, subject, body, cc, bcc), list_folders."
    )
    for field in ("description", "skill_description"):
        text = (conn.get(field) or "").strip()
        if text:
            desc += "\n" + text
    if len(desc) > 2000:
        desc = desc[:2000] + "…"
    return {
        "name": tool_name,
        "description": desc,
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search_messages", "list_messages",
                             "read_message", "send", "list_folders",
                             "download_attachment"],
                },
                "from": {
                    "type": "string",
                    "description": "Sender name or address contains this (search).",
                },
                "to": {
                    "type": "string",
                    "description": "Recipient filter (search) OR recipient "
                                   "address(es), comma-separated (send).",
                },
                "text": {
                    "type": "string",
                    "description": "Keywords to find in the message headers "
                                   "and body (search).",
                },
                "since": {
                    "type": "string",
                    "description": "Only messages on/after this date, "
                                   "YYYY-MM-DD (search).",
                },
                "before": {
                    "type": "string",
                    "description": "Only messages before this date, "
                                   "YYYY-MM-DD (search).",
                },
                "last_days": {
                    "type": "integer",
                    "description": "Shortcut: only messages from the last N "
                                   "days (search).",
                },
                "unseen": {
                    "type": "boolean",
                    "description": "true = unread only, false = read only "
                                   "(search).",
                },
                "flagged": {
                    "type": "boolean",
                    "description": "Only flagged/starred messages (search).",
                },
                "has_attachment": {
                    "type": "boolean",
                    "description": "true = only messages with attachments, "
                                   "false = only without (search).",
                },
                "attachment_name": {
                    "type": "string",
                    "description": "Attachment filename filter — substring "
                                   "or * wildcard, e.g. 'contract' or "
                                   "'*.pdf' (search).",
                },
                "min_size_kb": {
                    "type": "number",
                    "description": "Only messages larger than this many KB "
                                   "(search).",
                },
                "max_size_kb": {
                    "type": "number",
                    "description": "Only messages smaller than this many KB "
                                   "(search).",
                },
                "folder": {
                    "type": "string",
                    "description": "Mailbox folder (default INBOX).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to list (default 10, max 50).",
                },
                "unseen_only": {
                    "type": "boolean",
                    "description": "List only unread messages.",
                },
                "uid": {
                    "type": "string",
                    "description": "Message uid (from list_messages) to read "
                                   "or download an attachment from.",
                },
                "filename": {
                    "type": "string",
                    "description": "Attachment filename (from read_message) "
                                   "to download.",
                },
                "index": {
                    "type": "integer",
                    "description": "0-based attachment index (alternative to "
                                   "filename).",
                },
                "save_dir": {
                    "type": "string",
                    "description": "Directory to save the attachment "
                                   "(default ~/Downloads).",
                },
                "cc": {"type": "string",
                       "description": "CC filter (search) or CC recipients (send)."},
                "bcc": {"type": "string", "description": "BCC recipients (send)."},
                "subject": {"type": "string",
                            "description": "Subject filter (search) or subject line (send)."},
                "body": {"type": "string", "description": "Plain-text body (send)."},
                "reply_to": {
                    "type": "string",
                    "description": "Reply-To address (send, optional).",
                },
            },
            "required": ["action"],
        },
    }


# Products registered as suite sub-tools for the combined "google" connection.
GOOGLE_PRODUCTS = ["gmail", "drive", "sheets", "docs", "slides", "forms",
                   "tasks", "chat", "people", "calendar", "meet", "app_script"]

# Mapping from individual Google service catalog keys → the single product
# they expose.  Each creates one base proxy tool + one product suite tool
# (e.g. google_sheets connection → vault_<id> + vault_<id>_sheets).
GOOGLE_INDIVIDUAL_SERVICES: Dict[str, str] = {
    "google_gmail":      "gmail",
    "google_drive":      "drive",
    "google_sheets":     "sheets",
    "google_docs":       "docs",
    "google_slides":     "slides",
    "google_forms":      "forms",
    "google_calendar":   "calendar",
    "google_tasks":      "tasks",
    "google_people":     "people",
    "google_meet":       "meet",
    "google_app_script": "app_script",
}

# ---------------------------------------------------------------------------
# MCP bearer-token connection tools
# ---------------------------------------------------------------------------

def _mcp_call(conn_id: str, tool_name: str, args: dict) -> str:
    """Call a named tool on an MCP bearer connection through the vault."""
    payload = {"tool": tool_name, "arguments": args or {}}
    try:
        resp = _vault_http("POST", f"/api/vault/mcp/{conn_id}", payload, timeout=125)
        return json.dumps(resp, ensure_ascii=False, default=str)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode() if e.fp else ""
        except Exception:
            pass
        # Structured 401: the vault detected that the MCP bearer token has
        # expired.  Only return the structured result when the body's
        # detail.error == "mcp_token_expired" — any other 401 (including
        # vault agent-auth failures, which use a plain-string detail) must
        # fall through to the generic "Vault MCP error (401): ..." handler.
        if e.code == 401:
            try:
                parsed = json.loads(body)
                detail_obj = parsed.get("detail") if isinstance(parsed, dict) else None
                if isinstance(detail_obj, dict) and detail_obj.get("error") == "mcp_token_expired":
                    return json.dumps(detail_obj)
            except Exception:
                pass
            # Not the structured form — fall through to generic handler below.
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return json.dumps({"error": f"Vault MCP error ({e.code}): {detail}"})
    except Exception as e:
        return json.dumps({"error": f"MCP call failed: {e}"})


def _make_mcp_tool_handler(conn_id: str, remote_tool_name: str):
    def _handler(args: dict, **_kw) -> str:
        return _mcp_call(conn_id, remote_tool_name, args or {})
    return _handler


def _build_mcp_tool_schema(tool_name: str, mcp_tool: dict) -> dict:
    """Build a tool schema from an MCP tools/list entry."""
    input_schema = mcp_tool.get("inputSchema") or {}
    # MCP uses inputSchema; normalise to 'parameters' for the registry.
    params = {
        "type": input_schema.get("type", "object"),
        "properties": input_schema.get("properties", {}),
    }
    if input_schema.get("required"):
        params["required"] = input_schema["required"]
    desc = (mcp_tool.get("description") or "").strip()
    if len(desc) > 1500:
        desc = desc[:1500] + "…"
    return {
        "name": tool_name,
        "description": desc or f"MCP tool: {mcp_tool.get('name', tool_name)}",
        "parameters": params,
    }


def _sync_mcp_tools(conn_id: str, conn: dict, base_tool_name: str,
                    claimed: Dict[str, str]) -> None:
    """Register one native tool per remote MCP tool for *conn_id*.

    Uses the cached tool manifest stored in conn["mcp_tools"] by the vault's
    list endpoint.  If the manifest is empty, deregisters any stale tools.
    Called from _sync_connection_tools while holding _registered_lock.
    """
    mcp_tools: List[dict] = conn.get("mcp_tools") or []
    old_suite = _registered_suite.get(conn_id, [])

    new_suite: List[str] = []
    for mcp_tool in mcp_tools:
        remote_name = str(mcp_tool.get("name") or "").strip()
        if not remote_name:
            continue
        safe = re.sub(r"[^a-z0-9_]+", "_", remote_name.lower()).strip("_")
        pname = f"{base_tool_name}_{safe}"
        # Collision check
        if claimed.get(pname) not in (None, conn_id):
            logger.warning(
                "MCP tool name collision: %r already claimed by %r — "
                "skipping %r for connection %r",
                pname, claimed[pname], remote_name, conn_id,
            )
            continue
        claimed[pname] = conn_id

        pschema = _build_mcp_tool_schema(pname, mcp_tool)
        pexisting = registry.get_entry(pname)
        if pname in old_suite and pexisting is not None and pexisting.schema == pschema:
            new_suite.append(pname)
            continue
        try:
            registry.register(
                name=pname,
                toolset=TOOLSET,
                schema=pschema,
                handler=_make_mcp_tool_handler(conn_id, remote_name),
                description=f"MCP tool {remote_name!r} via vault connection {conn_id}",
                emoji="🔐",
            )
            new_suite.append(pname)
        except Exception:
            logger.exception(
                "Failed to register MCP tool %r for connection %s", pname, conn_id)

    # Deregister stale tools that are no longer in the manifest.
    for stale in old_suite:
        if stale not in new_suite:
            try:
                registry.deregister(stale)
            except Exception:
                pass
    _registered_suite[conn_id] = new_suite

GOOGLE_OPERATIONS: Dict[str, str] = {
    "gmail": "search (q, limit), read (id), send (to, subject, body, cc, bcc), "
             "modify (id, add_labels, remove_labels), labels",
    "drive": "search (q or name_contains, limit), get (file_id), download (file_id), "
             "export (file_id, mime_type), create_folder (name, parent_id), delete (file_id)",
    "meet": "spaces (limit), get_space (name), end_active_conference (name)",
    "app_script": "list (limit), get (script_id), get_content (script_id), "
                  "run (script_id, function_name, parameters, dev_mode)",
    "sheets": "create (title), meta (spreadsheet_id), get (spreadsheet_id, range), "
              "update (spreadsheet_id, range, values), append (spreadsheet_id, range, values), "
              "batch_get (spreadsheet_id, ranges)",
    "docs": "create (title), get (document_id), insert_text (document_id, text, index), "
            "batch_update (document_id, requests)",
    "slides": "create (title), get (presentation_id), batch_update (presentation_id, requests)",
    "forms": "create (title), get (form_id), responses (form_id), batch_update (form_id, requests)",
    "tasks": "lists, list (tasklist, show_completed, limit), create (title, notes, due, tasklist), "
             "complete (task_id, tasklist), delete (task_id, tasklist)",
    "chat": "spaces (limit), messages (space, limit), send (space, text)",
    "people": "contacts (limit), search (query), get (resource_name)",
    "calendar": "calendars, events (calendar_id, time_min, time_max, q, limit), "
                "create_event (summary, start, end, description, location, attendees), "
                "update_event (event_id, patch, calendar_id), delete_event (event_id, calendar_id)",
}


def _google_call(conn_id: str, product: str, args: dict) -> str:
    """Execute a structured Google Workspace operation through the vault.

    Accepts two calling conventions so LLMs that misread the schema still work:
      Flat (preferred):  {"operation": "search", "args": {"q": "..."}}
      Nested (fallback): {"args": {"operation": "search", "q": "..."}}
    """
    a = args or {}
    operation = str(a.get("operation") or "").strip()
    inner = a.get("args") if isinstance(a.get("args"), dict) else {}

    if not operation:
        # LLM may have nested everything (incl. operation) inside "args".
        operation = str(inner.get("operation") or "").strip()
        if operation:
            # Promote: remaining keys in inner become the real args.
            inner = {k: v for k, v in inner.items() if k != "operation"}
        else:
            # As a last resort, treat the whole dict (minus "args" key) as flat args.
            flat = {k: v for k, v in a.items() if k != "args"}
            operation = str(flat.get("operation") or "").strip()
            if operation:
                inner = {k: v for k, v in flat.items() if k != "operation"}

    if not operation:
        return json.dumps({"error": "operation is required",
                           "hint": f"Pass operation as a top-level key. "
                                   f"Supported: {GOOGLE_OPERATIONS.get(product, 'see tool description')}",
                           "example": {"operation": "search", "args": {"q": "..."}}})
    payload = {
        "product": product,
        "operation": operation,
        "args": inner,
    }
    try:
        return json.dumps(
            _vault_http("POST", f"/api/vault/google/{conn_id}", payload, timeout=90),
            ensure_ascii=False, default=str,
        )
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
    except Exception as e:
        return json.dumps({"error": f"Vault call failed: {e}"})


def _build_google_schema(conn: dict, tool_name: str, product: str) -> dict:
    conn_id = conn["id"]
    label = conn.get("label") or conn_id
    ops_str = GOOGLE_OPERATIONS.get(product, "")
    return {
        "name": tool_name,
        "description": (
            f"Google {product.capitalize()} for the '{label}' account via the "
            "secure vault (OAuth token attached server-side — never handle "
            f"credentials).\n"
            f"Supported operations: {ops_str}.\n"
            "Also supports operation='request' with args={{method, path, params, json}} "
            f"for any raw {product.capitalize()} API call not listed above.\n"
            "CALLING CONVENTION: pass `operation` as a TOP-LEVEL key alongside `args`. "
            'Example: {"operation": "search", "args": {"q": "my query"}}. '
            "Do NOT nest `operation` inside `args`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": (
                        f"Which operation to run. One of: {ops_str} — or 'request'. "
                        "Pass this as a top-level key, NOT inside args."
                    ),
                },
                "args": {
                    "type": "object",
                    "description": (
                        "Operation-specific arguments (everything EXCEPT operation itself). "
                        "For example, {\"q\": \"query\"} for search, "
                        "{\"spreadsheet_id\": \"...\", \"range\": \"A1:B5\"} for get."
                    ),
                },
            },
            "required": ["operation"],
        },
    }


def _make_google_handler(conn_id: str, product: str):
    def _handler(args: dict, **_kw) -> str:
        return _google_call(conn_id, product, args or {})
    return _handler


def _make_conn_handler(conn_id: str, auth_kind: str = "", service: str = ""):
    if auth_kind == "email" or service == "email":
        def _email_handler(args: dict, **_kw) -> str:
            return _email_call(conn_id, args or {})
        return _email_handler
    if service == "apple" or auth_kind == "apple":
        def _apple_handler(args: dict, **_kw) -> str:
            return _special_call(conn_id, "apple", args or {})
        return _apple_handler
    if service == "macincloud" or auth_kind == "macincloud":
        def _mac_handler(args: dict, **_kw) -> str:
            return _special_call(conn_id, "mac", args or {})
        return _mac_handler
    if service == "discord_read":
        def _discord_read_handler(args: dict, **_kw) -> str:
            return _discord_read_call(conn_id, args or {})
        return _discord_read_handler
    if auth_kind == "amazon":
        def _amazon_handler(args: dict, **_kw) -> str:
            return _special_call(conn_id, "amazon", args or {})
        return _amazon_handler
    # Google OAuth hub: accept {product, operation, args} and route through
    # the structured Google endpoint rather than the generic proxy.
    if service == "google" and auth_kind == "oauth2":
        def _google_hub_handler(args: dict, **_kw) -> str:
            a = args or {}
            product = str(a.get("product") or "").strip().lower()
            if not product:
                return json.dumps({
                    "error": "product is required for the Google hub tool.",
                    "hint": (
                        "Pass 'product' as one of: " + ", ".join(GOOGLE_PRODUCTS) + ". "
                        "Or use the dedicated per-product tool "
                        f"(e.g. vault_{conn_id.lower()}_sheets) which does not need 'product'."
                    ),
                })
            if product not in GOOGLE_PRODUCTS:
                return json.dumps({
                    "error": f"Unknown Google product '{product}'.",
                    "hint": "product must be one of: " + ", ".join(GOOGLE_PRODUCTS),
                })
            return _google_call(conn_id, product, a)
        return _google_hub_handler

    def _handler(args: dict, **_kw) -> str:
        if service in {"apple", "email"}:
            return _special_call(conn_id, service, args or {})
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
        # Deregister revoked connections (base tool + any Google suite tools)
        for conn_id in list(_registered):
            if conn_id not in seen:
                for name in [_registered[conn_id]] + _registered_suite.get(conn_id, []):
                    try:
                        registry.deregister(name)
                    except Exception:
                        logger.exception("Failed to deregister vault tool %s for %s", name, conn_id)
                del _registered[conn_id]
                _registered_suite.pop(conn_id, None)
                removed += 1
        # Register new / re-register changed. Track tool-name claims within
        # this sync so two connection ids that normalize to the same tool
        # name (e.g. "MY-API" and "MY_API") can't overwrite each other's
        # handler or cause the wrong tool to be deregistered on revoke.
        # One shared claim namespace covers base tools AND google suite
        # tools so e.g. connection "FOO_SHEETS"'s base tool can't collide
        # silently with connection "FOO"'s vault_foo_sheets suite tool.
        claimed = {name: cid for cid, name in _registered.items()
                   if cid in seen}
        for cid, names in _registered_suite.items():
            if cid in seen:
                for name in names:
                    claimed.setdefault(name, cid)
        for conn_id, conn in sorted(seen.items()):
            tool_name = _tool_name_for(conn_id)
            auth_kind = str(conn.get("auth_kind") or "")
            service_lower = (conn.get("service") or "").lower()
            is_google = service_lower == "google" and auth_kind == "oauth2"
            google_product = (GOOGLE_INDIVIDUAL_SERVICES.get(service_lower)
                              if auth_kind == "oauth2" else None)
            is_google_individual = bool(google_product)
            wanted_names = [tool_name]
            if is_google:
                wanted_names += [f"{tool_name}_{p}" for p in GOOGLE_PRODUCTS]
            elif is_google_individual:
                wanted_names += [f"{tool_name}_{google_product}"]
            collision = next(
                (n for n in wanted_names
                 if claimed.get(n) not in (None, conn_id)), None)
            if collision:
                logger.warning(
                    "Vault tool name collision: connections %r and %r both "
                    "claim tool %r — skipping %r. Rename one connection in "
                    "the vault dashboard.",
                    claimed[collision], conn_id, collision, conn_id,
                )
                continue
            for n in wanted_names:
                claimed[n] = conn_id

            is_mcp = (conn.get("service") or "").lower() in {"mcp_bearer", "vowsok"} \
                     or auth_kind == "mcp_bearer"
            if is_mcp:
                # MCP connections: register one tool per remote tool in the
                # cached manifest, not a single generic HTTP proxy tool.
                _sync_mcp_tools(conn_id, conn, tool_name, claimed)
                # Mark as registered using the base name so deregister works.
                _registered[conn_id] = tool_name
                continue
            if auth_kind == "email":
                schema = _build_email_schema(conn, tool_name)
            else:
                schema = _build_conn_schema(conn, tool_name)
            existing = registry.get_entry(tool_name)
            base_ok = True
            if (conn_id in _registered and existing is not None
                    and existing.schema == schema):
                pass  # base tool unchanged
            else:
                if conn_id in _registered and existing is not None:
                    updated += 1
                else:
                    added += 1
                try:
                    registry.register(
                        name=tool_name,
                        toolset=TOOLSET,
                        schema=schema,
                        handler=_make_conn_handler(conn_id, auth_kind, conn.get("service") or ""),
                        description=f"Vault-proxied access to {conn.get('service') or conn_id}",
                        emoji="🔐",
                    )
                    _registered[conn_id] = tool_name
                except Exception:
                    logger.exception("Failed to register vault tool for %s", conn_id)
                    base_ok = False

            # Google suite tools are reconciled on EVERY sync — independent
            # of whether the base tool changed — so failed registrations are
            # retried and auth-kind changes drop stale product tools.
            def _register_google_product(product: str) -> bool:
                """Register one product tool; return True on success."""
                nonlocal added, updated
                pname = f"{tool_name}_{product}"
                pschema = _build_google_schema(conn, pname, product)
                pexisting = registry.get_entry(pname)
                already = pname in _registered_suite.get(conn_id, [])
                if already and pexisting is not None and pexisting.schema == pschema:
                    return True  # unchanged — keep it
                try:
                    registry.register(
                        name=pname,
                        toolset=TOOLSET,
                        schema=pschema,
                        handler=_make_google_handler(conn_id, product),
                        description=f"Google {product} via vault connection {conn_id}",
                        emoji="🔐",
                    )
                    if already:
                        updated += 1
                    else:
                        added += 1
                    return True
                except Exception:
                    logger.exception(
                        "Failed to register google %s tool for %s", product, conn_id)
                    return False

            if is_google and base_ok:
                suite: List[str] = []
                for product in GOOGLE_PRODUCTS:
                    if _register_google_product(product):
                        suite.append(f"{tool_name}_{product}")
                _registered_suite[conn_id] = suite
            elif is_google_individual and base_ok:
                # Individual service connection: register only the one product tool.
                suite_individual: List[str] = []
                if _register_google_product(google_product):
                    suite_individual.append(f"{tool_name}_{google_product}")
                # Deregister any stale tools from a prior service type.
                for stale in _registered_suite.get(conn_id, []):
                    if stale not in suite_individual:
                        try:
                            registry.deregister(stale)
                        except Exception:
                            pass
                        removed += 1
                _registered_suite[conn_id] = suite_individual
            elif not is_google and not is_google_individual \
                    and conn_id in _registered_suite:
                # No longer a google oauth connection — drop stale suite tools.
                for name in _registered_suite.pop(conn_id):
                    try:
                        registry.deregister(name)
                    except Exception:
                        logger.exception("Failed to deregister stale tool %s", name)
                    removed += 1

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
