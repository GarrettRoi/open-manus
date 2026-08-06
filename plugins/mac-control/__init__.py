"""/mac — control a MACinCloud VM stored in the vault.

Commands (all purely programmatic — no agent/LLM in the loop):
  /mac screenshot [conn]       — take desktop screenshot, post as image
  /mac open <url> [conn]       — open URL in browser on the Mac
  /mac run <cmd> [conn]        — run a shell command
  /mac vnc [conn]              — get the interactive desktop viewer URL
  /mac apps [conn]             — list installed applications
  /mac help                    — show this message

'conn' is the vault connection ID (e.g. MY_MAC).  If you only have one
MACinCloud connection it is selected automatically.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from typing import Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

VAULT_URL = os.getenv("VAULT_URL", "http://vault.railway.internal:8080").rstrip("/")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")
# The vault's public URL is needed to generate the VNC viewer link.
# Falls back to the internal URL when running locally.
VAULT_PUBLIC_URL = os.getenv("VAULT_PUBLIC_URL", VAULT_URL).rstrip("/")

_HELP = """\
**`/mac` — MACinCloud desktop control**
```
/mac screenshot [conn]    Take a screenshot and post it here
/mac open <url> [conn]    Open a URL in the Mac browser
/mac run <cmd> [conn]     Run a shell command on the Mac
/mac vnc [conn]           Get the live interactive desktop viewer link
/mac apps [conn]          List installed apps
/mac help                 This message
```
`conn` = vault connection ID (optional if only one Mac connection exists).
"""


# ---------------------------------------------------------------------------
# Vault HTTP helpers
# ---------------------------------------------------------------------------

def _vault(method: str, path: str, payload: Optional[dict] = None,
           timeout: float = 90) -> dict:
    if not VAULT_TOKEN:
        raise RuntimeError("VAULT_TOKEN not set — vault tools unavailable")
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(
        f"{VAULT_URL}{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {VAULT_TOKEN}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _list_mac_connections() -> list[dict]:
    try:
        data = _vault("GET", "/api/vault/list")
        conns = data.get("available_connections") or []
        return [c for c in conns if c.get("service") == "macincloud"]
    except Exception:
        return []


def _resolve_conn(argv: list[str]) -> Tuple[str, list[str]]:
    """Return (conn_id, remaining_argv).  Raises if ambiguous or not found."""
    mac_conns = _list_mac_connections()
    if not mac_conns:
        raise RuntimeError(
            "No MACinCloud connections found in the vault. "
            "Add one via the vault dashboard (/services)."
        )
    # If last arg looks like a connection ID (all caps/underscores, no spaces)
    if argv and re.match(r'^[A-Z0-9_\-]+$', argv[-1]):
        cid = argv[-1].upper()
        if any(c["id"] == cid for c in mac_conns):
            return cid, argv[:-1]
    if len(mac_conns) == 1:
        return mac_conns[0]["id"], argv
    ids = ", ".join(c["id"] for c in mac_conns)
    raise RuntimeError(
        f"Multiple Mac connections found ({ids}). "
        "Append the connection ID to your command, e.g. `/mac screenshot MY_MAC`."
    )


def _mac_op(conn_id: str, operation: str, args: dict) -> dict:
    return _vault("POST", f"/api/vault/mac/{conn_id}",
                  {"operation": operation, "args": args}, timeout=90)


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def _cmd_screenshot(argv: list[str]) -> str:
    conn_id, _ = _resolve_conn(argv)
    try:
        result = _mac_op(conn_id, "screenshot", {"cursor": True})
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return f"Screenshot failed: {detail}"
    except Exception as e:
        return f"Screenshot failed: {e}"

    b64 = (result.get("result") or result).get("image_b64", "")
    if not b64:
        return "Screenshot returned no image data."

    # Save to a temp file so the Discord gateway can post it as an attachment.
    # The gateway looks for a path in the format: [file://<path>]
    raw = base64.b64decode(b64)
    tmp = tempfile.NamedTemporaryFile(suffix=".png", prefix="mac_ss_", delete=False)
    tmp.write(raw)
    tmp.flush()
    tmp.close()
    size_kb = len(raw) // 1024
    # Return a special marker the Discord adapter turns into a file attachment.
    return f"[file://{tmp.name}]\n📸 Screenshot from **{conn_id}** ({size_kb} KB)"


def _cmd_open(argv: list[str]) -> str:
    if not argv:
        return "Usage: `/mac open <url> [conn]`"
    conn_id, rest = _resolve_conn(argv)
    url = rest[0] if rest else ""
    if not url:
        return "Usage: `/mac open <url> [conn]`"
    try:
        result = _mac_op(conn_id, "open_browser", {"url": url})
        inner = result.get("result") or result
        output = inner.get("output") or ""
        app = inner.get("app") or "browser"
        return f"✅ Opened **{url}** in {app} on `{conn_id}`" + (f"\n```\n{output}\n```" if output else "")
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return f"open_browser failed: {detail}"
    except Exception as e:
        return f"open_browser failed: {e}"


def _cmd_run(argv: list[str]) -> str:
    if not argv:
        return "Usage: `/mac run <command> [conn]`"
    conn_id, rest = _resolve_conn(argv)
    cmd = " ".join(rest) if rest else ""
    if not cmd:
        return "Usage: `/mac run <command> [conn]`"
    try:
        result = _mac_op(conn_id, "run_command",
                         {"command": cmd, "allow_shell_ops": True})
        output = (result.get("result") or result).get("output") or "(no output)"
        output = output[:1800]  # Discord 2000-char message limit buffer
        return f"```\n{output}\n```"
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        return f"Command failed: {detail}"
    except Exception as e:
        return f"Command failed: {e}"


def _cmd_vnc(argv: list[str]) -> str:
    conn_id, _ = _resolve_conn(argv)
    url = f"{VAULT_PUBLIC_URL}/vnc/{conn_id}"
    return (
        f"🖥️ **Live desktop viewer** for `{conn_id}`:\n"
        f"{url}\n\n"
        "Open in your browser for full mouse + keyboard interaction.\n"
        "*(Requires your vault admin session — log in at the vault dashboard first.)*"
    )


def _cmd_apps(argv: list[str]) -> str:
    conn_id, _ = _resolve_conn(argv)
    try:
        result = _mac_op(conn_id, "list_apps", {})
        inner = result.get("result") or result
        apps = inner.get("apps") or []
        count = inner.get("count", len(apps))
        if not apps:
            return f"No apps found on `{conn_id}`."
        listing = "\n".join(f"• {a}" for a in apps[:50])
        suffix = f"\n…and {count - 50} more" if count > 50 else ""
        return f"**{count} apps on `{conn_id}`:**\n{listing}{suffix}"
    except Exception as e:
        return f"list_apps failed: {e}"


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def _handle_slash(raw_args: str) -> Optional[str]:
    if not VAULT_TOKEN:
        return "VAULT_TOKEN is not set — vault tools are unavailable."
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP
    sub, rest = argv[0].lower(), argv[1:]
    try:
        if sub == "screenshot":
            return _cmd_screenshot(rest)
        if sub == "open":
            return _cmd_open(rest)
        if sub == "run":
            return _cmd_run(rest)
        if sub == "vnc":
            return _cmd_vnc(rest)
        if sub == "apps":
            return _cmd_apps(rest)
    except RuntimeError as e:
        return f"⚠️ {e}"
    return f"Unknown subcommand `{sub}`.\n\n{_HELP}"


def register(ctx) -> None:
    ctx.register_command(
        "mac",
        handler=_handle_slash,
        description="Control a MACinCloud VM: screenshot, run commands, open browser, live desktop viewer.",
        args_hint="screenshot | open <url> | run <cmd> | vnc | apps | help",
    )
