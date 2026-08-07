"""Structured Mac/SSH operations for MACinCloud vault connections.

Agents call POST /api/vault/mac/{conn_id} with {operation, args}.
All SSH/SFTP is done inside the vault — agents never see credentials.
"""
from __future__ import annotations

import asyncio
import io
import socket
from typing import Any, Dict

try:
    import paramiko
except ImportError:
    paramiko = None  # type: ignore[assignment]


class MacOpsError(Exception):
    """Raised for invalid operations or SSH/SFTP failures."""


# Maximum output returned from shell commands (bytes)
_MAX_OUTPUT = 64 * 1024
# Screenshot temp path on the remote Mac
_SS_PATH = "/tmp/_hermes_screenshot.png"


def _require_paramiko() -> None:
    if paramiko is None:
        raise MacOpsError(
            "paramiko is not installed on the vault service — "
            "add it to services/vault/requirements.txt and redeploy."
        )


def _ssh_connect(secrets: Dict[str, Any]) -> "paramiko.SSHClient":
    """Open an authenticated SSH session using stored secrets."""
    _require_paramiko()
    host = (secrets.get("ssh_host") or "").strip()
    user = (secrets.get("ssh_user") or "").strip()
    password = (secrets.get("ssh_password") or "").strip()
    port = int(secrets.get("ssh_port") or 22)
    if not host or not user or not password:
        raise MacOpsError(
            "MACinCloud connection is missing ssh_host, ssh_user, or ssh_password — "
            "edit the connection in the vault dashboard."
        )
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=port, username=user, password=password,
            timeout=20, banner_timeout=20, auth_timeout=20,
            look_for_keys=False, allow_agent=False,
        )
    except (paramiko.AuthenticationException, paramiko.SSHException, OSError) as exc:
        raise MacOpsError(f"SSH connection failed: {exc}") from exc
    return client


def _run_ssh(client: "paramiko.SSHClient", cmd: str, timeout: float = 30) -> str:
    """Run a command and return its combined stdout+stderr."""
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
        out = stdout.read(_MAX_OUTPUT).decode("utf-8", "replace")
        err = stderr.read(_MAX_OUTPUT).decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        combined = (out + err).strip()
        if rc != 0 and not combined:
            combined = f"(exit code {rc})"
        return combined
    except Exception as exc:
        raise MacOpsError(f"SSH exec failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Individual operation handlers
# ---------------------------------------------------------------------------

async def _screenshot(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Take a screenshot of the Mac desktop and return it as base64 PNG."""
    import base64

    loop = asyncio.get_running_loop()

    def _take() -> bytes:
        client = _ssh_connect(secrets)
        try:
            # -x = no sound; -C = include cursor; send to temp file
            cursor = "-C" if args.get("cursor", True) else ""
            _run_ssh(client, f"screencapture -x {cursor} {_SS_PATH}", timeout=15)
            sftp = client.open_sftp()
            buf = io.BytesIO()
            sftp.getfo(_SS_PATH, buf)
            sftp.close()
            _run_ssh(client, f"rm -f {_SS_PATH}", timeout=5)
            return buf.getvalue()
        finally:
            client.close()

    data = await loop.run_in_executor(None, _take)
    if not data:
        raise MacOpsError("screencapture returned an empty file")
    import base64
    return {
        "format": "png",
        "size_bytes": len(data),
        "image_b64": base64.b64encode(data).decode(),
    }


async def _run_command(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Run an arbitrary shell command and return stdout+stderr."""
    (cmd,) = (_require_arg(args, "command"),)
    if any(c in cmd for c in [";", "&&", "||", "|", "`", "$(", "\n"]):
        # Allow pipelines and chains but require explicit confirmation flag.
        if not args.get("allow_shell_ops"):
            raise MacOpsError(
                "Command contains shell operators (; && || | ` $()). "
                "Set allow_shell_ops=true in args to confirm this is intentional."
            )
    timeout = min(float(args.get("timeout") or 30), 120)
    loop = asyncio.get_running_loop()

    def _exec() -> str:
        client = _ssh_connect(secrets)
        try:
            return _run_ssh(client, cmd, timeout=timeout)
        finally:
            client.close()

    output = await loop.run_in_executor(None, _exec)
    return {"output": output}


async def _open_browser(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Open a URL in the default browser (or a named app)."""
    url = _require_arg(args, "url")
    if not url.startswith(("http://", "https://")):
        raise MacOpsError("url must start with http:// or https://")
    app = (args.get("app") or "").strip()  # e.g. "Google Chrome", "Firefox"
    if app:
        cmd = f'open -a {_quote(app)} {_quote(url)}'
    else:
        cmd = f"open {_quote(url)}"
    loop = asyncio.get_running_loop()

    def _exec() -> str:
        client = _ssh_connect(secrets)
        try:
            return _run_ssh(client, cmd, timeout=15)
        finally:
            client.close()

    output = await loop.run_in_executor(None, _exec)
    return {"url": url, "app": app or "default browser", "output": output}


async def _applescript(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute an AppleScript for GUI automation."""
    script = _require_arg(args, "script")
    # Safety: refuse scripts that use `do shell script` unless opted-in.
    if "do shell script" in script and not args.get("allow_shell"):
        raise MacOpsError(
            "AppleScript contains 'do shell script'. "
            "Set allow_shell=true in args to confirm."
        )
    # Escape single quotes in the script for wrapping in osascript -e '...'
    # Use a heredoc approach instead to avoid escaping nightmares.
    loop = asyncio.get_running_loop()

    def _exec() -> str:
        client = _ssh_connect(secrets)
        try:
            sftp = client.open_sftp()
            remote_path = "/tmp/_hermes_script.applescript"
            with sftp.open(remote_path, "w") as f:
                f.write(script)
            sftp.close()
            result = _run_ssh(client, f"osascript {remote_path}", timeout=60)
            _run_ssh(client, f"rm -f {remote_path}", timeout=5)
            return result
        finally:
            client.close()

    output = await loop.run_in_executor(None, _exec)
    return {"output": output}


async def _list_apps(secrets: Dict[str, Any], _args: Dict[str, Any]) -> Dict[str, Any]:
    """List installed applications on the Mac."""
    loop = asyncio.get_running_loop()

    def _exec() -> str:
        client = _ssh_connect(secrets)
        try:
            return _run_ssh(
                client,
                "ls /Applications/ /Applications/Utilities/ 2>/dev/null | grep '\\.app$' | sed 's/\\.app$//' | sort",
                timeout=15,
            )
        finally:
            client.close()

    output = await loop.run_in_executor(None, _exec)
    apps = [a.strip() for a in output.splitlines() if a.strip()]
    return {"apps": apps, "count": len(apps)}


async def _key_combo(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Send a keyboard shortcut via AppleScript (e.g. cmd+c, cmd+tab)."""
    keys = _require_arg(args, "keys")  # e.g. "command+c" or "command+tab"
    parts = [k.strip().lower() for k in keys.replace("+", " ").split()]
    _KEY_MAP = {
        "cmd": "command", "command": "command", "ctrl": "control",
        "control": "control", "shift": "shift", "opt": "option",
        "option": "option", "alt": "option", "fn": "function",
        "tab": "tab", "return": "return", "enter": "return",
        "space": "space", "escape": "escape", "esc": "escape",
        "up": "up arrow", "down": "down arrow",
        "left": "left arrow", "right": "right arrow",
        "delete": "delete", "backspace": "delete",
        "f1":"f1","f2":"f2","f3":"f3","f4":"f4","f5":"f5","f6":"f6",
        "f7":"f7","f8":"f8","f9":"f9","f10":"f10","f11":"f11","f12":"f12",
    }
    modifiers = []
    key = None
    for p in parts:
        if p in {"command", "control", "shift", "option", "function",
                 "cmd", "ctrl", "opt", "alt"}:
            modifiers.append(_KEY_MAP.get(p, p))
        else:
            key = _KEY_MAP.get(p, p)
    if key is None:
        raise MacOpsError(
            f"Could not parse key combo '{keys}'. "
            "Format: modifier+key e.g. 'command+c', 'command+shift+s'"
        )
    mod_str = (", ".join(f"{m} down" for m in set(modifiers))) if modifiers else ""
    script = (
        f'tell application "System Events"\n'
        f'  key code (key code "{key}"){" using {" + mod_str + "}" if mod_str else ""}\n'
        f"end tell"
    )
    # Use simpler key code approach via keystroke
    if modifiers:
        using = " & ".join(f"{m} down" for m in set(modifiers))
        script = (
            f'tell application "System Events"\n'
            f'  keystroke "{key}" using {{{using}}}\n'
            f"end tell"
        )
    else:
        script = (
            f'tell application "System Events"\n'
            f'  keystroke "{key}"\n'
            f"end tell"
        )
    return await _applescript(secrets, {"script": script})


async def _type_text(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Type text at the current cursor position using AppleScript."""
    text = _require_arg(args, "text")
    escaped = text.replace('"', '\\"')
    script = (
        f'tell application "System Events"\n'
        f'  keystroke "{escaped}"\n'
        f"end tell"
    )
    return await _applescript(secrets, {"script": script})


async def _focus_app(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Bring an application to the foreground."""
    app = _require_arg(args, "app")
    script = f'tell application "{app}" to activate'
    return await _applescript(secrets, {"script": script})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_arg(args: Dict[str, Any], key: str) -> str:
    val = (args.get(key) or "").strip() if isinstance(args.get(key), str) else args.get(key)
    if not val:
        raise MacOpsError(f"'{key}' is required")
    return str(val)


def _quote(s: str) -> str:
    """Single-quote a shell argument, escaping any single quotes inside."""
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def _mouse_event(secrets: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    """Synthesize a mouse event at absolute screen coordinates via CoreGraphics."""
    x = int(args.get("x") or 0)
    y = int(args.get("y") or 0)
    action = str(args.get("action") or "click").lower()   # click | right_click | double_click | move | scroll
    scroll_dx = int(args.get("scroll_dx") or 0)
    scroll_dy = int(args.get("scroll_dy") or 0)

    if action == "scroll":
        # scroll_dy > 0 = scroll up/towards user; < 0 = scroll down
        py_snippet = (
            f"import Quartz, time\n"
            f"Quartz.CGEventPost(Quartz.kCGHIDEventTap,\n"
            f"    Quartz.CGEventCreateScrollWheelEvent(None,\n"
            f"        Quartz.kCGScrollEventUnitLine, 2, {scroll_dy}, {scroll_dx}))\n"
        )
    else:
        if action == "right_click":
            down_t = "Quartz.kCGEventRightMouseDown"
            up_t   = "Quartz.kCGEventRightMouseUp"
            btn    = "Quartz.kCGMouseButtonRight"
        else:
            down_t = "Quartz.kCGEventLeftMouseDown"
            up_t   = "Quartz.kCGEventLeftMouseUp"
            btn    = "Quartz.kCGMouseButtonLeft"
        clicks = 2 if action == "double_click" else 1
        if action == "move":
            py_snippet = (
                f"import Quartz\n"
                f"pt = Quartz.CGPoint({x}, {y})\n"
                f"Quartz.CGEventPost(Quartz.kCGHIDEventTap,\n"
                f"    Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft))\n"
            )
        else:
            py_snippet = (
                f"import Quartz, time\n"
                f"pt = Quartz.CGPoint({x}, {y})\n"
                f"Quartz.CGEventPost(Quartz.kCGHIDEventTap,\n"
                f"    Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft))\n"
                f"time.sleep(0.05)\n"
                f"for _ in range({clicks}):\n"
                f"    Quartz.CGEventPost(Quartz.kCGHIDEventTap,\n"
                f"        Quartz.CGEventCreateMouseEvent(None, {down_t}, pt, {btn}))\n"
                f"    time.sleep(0.05)\n"
                f"    Quartz.CGEventPost(Quartz.kCGHIDEventTap,\n"
                f"        Quartz.CGEventCreateMouseEvent(None, {up_t}, pt, {btn}))\n"
                f"    time.sleep(0.05)\n"
            )

    # Wrap in a Python one-liner suitable for ssh exec_command
    cmd = f"/usr/bin/python3 -c \"{py_snippet.replace(chr(10), '; ')}\""
    loop = asyncio.get_running_loop()

    def _exec() -> str:
        client = _ssh_connect(secrets)
        try:
            return _run_ssh(client, cmd, timeout=15)
        finally:
            client.close()

    output = await loop.run_in_executor(None, _exec)
    result: Dict[str, Any] = {"action": action, "x": x, "y": y}
    if action == "scroll":
        result.update({"scroll_dx": scroll_dx, "scroll_dy": scroll_dy})
    if output:
        result["output"] = output  # surface any Python error
    return result


OPERATIONS = {
    "screenshot": _screenshot,
    "run_command": _run_command,
    "open_browser": _open_browser,
    "applescript": _applescript,
    "list_apps": _list_apps,
    "key_combo": _key_combo,
    "type_text": _type_text,
    "focus_app": _focus_app,
    # Mouse / pointer events — synthesized via CoreGraphics over SSH
    "click_at":        _mouse_event,  # args: x, y
    "right_click_at":  _mouse_event,  # args: x, y
    "double_click_at": _mouse_event,  # args: x, y
    "mouse_move":      _mouse_event,  # args: x, y
    "scroll":          _mouse_event,  # args: x, y, scroll_dy (±lines), scroll_dx
}

OPERATION_HINTS = {
    "screenshot": "Take a screenshot of the Mac desktop → returns image_b64 (PNG). args: cursor (bool, default true)",
    "run_command": "Run a shell command. args: command (required), allow_shell_ops (bool), timeout (seconds, max 120)",
    "open_browser": "Open a URL in the default browser. args: url (required), app ('Safari'/'Google Chrome'/etc)",
    "applescript": "Run an AppleScript for GUI automation. args: script (required), allow_shell (bool)",
    "list_apps": "List installed .app bundles in /Applications. No args needed.",
    "key_combo": "Send a keyboard shortcut. args: keys e.g. 'command+c', 'command+shift+s'",
    "type_text": "Type text at the current cursor position. args: text (required)",
    "focus_app": "Bring an app to the foreground. args: app (app name, e.g. 'Safari')",
    "click_at": "Click at absolute screen coordinates. args: x (int), y (int). Uses CoreGraphics.",
    "right_click_at": "Right-click at absolute screen coordinates. args: x (int), y (int).",
    "double_click_at": "Double-click at absolute screen coordinates. args: x (int), y (int).",
    "mouse_move": "Move the cursor without clicking. args: x (int), y (int).",
    "scroll": "Scroll the mouse wheel. args: x (int), y (int), scroll_dy (+up/-down lines), scroll_dx (horizontal).",
}


async def run_mac_operation(
    secrets: Dict[str, Any], operation: str, args: Dict[str, Any]
) -> Dict[str, Any]:
    """Entry point called by the vault endpoint."""
    op = (operation or "").strip().lower()
    if op not in OPERATIONS:
        raise MacOpsError(
            f"Unknown operation '{op}'. "
            f"Available: {', '.join(sorted(OPERATIONS))}"
        )
    # Mouse-event operations all share _mouse_event; inject the correct action
    # so the handler knows which type of event to synthesize.
    _OP_TO_ACTION = {
        "click_at": "click",
        "right_click_at": "right_click",
        "double_click_at": "double_click",
        "mouse_move": "move",
        "scroll": "scroll",
    }
    effective_args = dict(args or {})
    if op in _OP_TO_ACTION:
        effective_args.setdefault("action", _OP_TO_ACTION[op])
    return await OPERATIONS[op](secrets, effective_args)
