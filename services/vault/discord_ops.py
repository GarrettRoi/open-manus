"""Read-only Discord operations for the Open Manus Key Vault.

Two jobs:

1. ``readonly_allowed`` — the proxy-level guard for ``discord_read``
   connections: only GET/HEAD on an explicit allowlist of read paths is
   permitted. Any write-style Discord call is rejected before it ever
   reaches the network.

2. ``run_discord_operation`` — structured read operations executed
   server-side with the reader bot token (list servers, list channels,
   read messages, download attachments, extract links). The token never
   leaves the vault; agents get parsed JSON (or base64 file content).
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

DISCORD_API = "https://discord.com/api/v10"

# Hosts Discord serves attachment files from (GET-only pass-through).
ATTACHMENT_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}

# Read-only path allowlist for the discord.com API host.  Matched against
# the URL path with an optional /api or /api/v{N} prefix stripped.
_ID = r"\d+"
READONLY_PATH_PATTERNS = [
    re.compile(p) for p in (
        rf"^/users/@me$",
        rf"^/users/@me/guilds$",
        rf"^/oauth2/applications/@me$",
        rf"^/guilds/{_ID}$",
        rf"^/guilds/{_ID}/channels$",
        rf"^/guilds/{_ID}/threads/active$",
        rf"^/channels/{_ID}$",
        rf"^/channels/{_ID}/messages$",
        rf"^/channels/{_ID}/messages/{_ID}$",
        rf"^/channels/{_ID}/pins$",
    )
]

_API_PREFIX_RE = re.compile(r"^/api(/v\d+)?")

ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024  # 8MB cap on streamed files

# View Channels (1024) + Read Message History (65536)
READONLY_PERMISSIONS = 1024 + 65536  # 66560


class DiscordOpsError(Exception):
    pass


def readonly_allowed(method: str, host: str, path: str) -> bool:
    """True when (method, host, path) is a permitted read-only Discord call."""
    method = (method or "").upper()
    if method not in {"GET", "HEAD"}:
        return False
    host = (host or "").lower()
    if host in ATTACHMENT_HOSTS:
        return True  # CDN file fetches are inherently read-only
    if host != "discord.com":
        return False
    norm = _API_PREFIX_RE.sub("", path or "") or "/"
    return any(p.match(norm) for p in READONLY_PATH_PATTERNS)


def invite_url(application_id: str, guild_id: str = "") -> str:
    """Read-only bot invite link (View Channels + Read Message History)."""
    url = (f"https://discord.com/oauth2/authorize?client_id={application_id}"
           f"&scope=bot&permissions={READONLY_PERMISSIONS}")
    if guild_id:
        url += f"&guild_id={guild_id}&disable_guild_select=true"
    return url


# ---------------------------------------------------------------------------
# Structured read operations (executed with the bot token, server-side)
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


def _require_snowflake(args: Dict[str, Any], key: str) -> str:
    val = str(args.get(key) or "").strip()
    if not val.isdigit():
        raise DiscordOpsError(f"'{key}' is required and must be a numeric Discord ID")
    return val


async def _bot_get(client: httpx.AsyncClient, bot_token: str, path: str,
                   params: Optional[dict] = None) -> Any:
    resp = await client.get(
        f"{DISCORD_API}{path}", params=params,
        headers={"Authorization": f"Bot {bot_token}"})
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message", "")
        except ValueError:
            msg = resp.text[:200]
        raise DiscordOpsError(f"Discord API error {resp.status_code}: {msg}")
    return resp.json()


def _simplify_message(m: dict) -> dict:
    author = m.get("author") or {}
    return {
        "id": m.get("id"),
        "channel_id": m.get("channel_id"),
        "author": {
            "id": author.get("id"),
            "username": author.get("username"),
            "global_name": author.get("global_name"),
            "bot": bool(author.get("bot")),
        },
        "timestamp": m.get("timestamp"),
        "content": m.get("content", ""),
        "attachments": [
            {
                "id": a.get("id"),
                "filename": a.get("filename"),
                "content_type": a.get("content_type"),
                "size": a.get("size"),
                "url": a.get("url"),
            }
            for a in (m.get("attachments") or [])
        ],
        "embeds": [
            {"title": e.get("title"), "url": e.get("url"),
             "description": (e.get("description") or "")[:300]}
            for e in (m.get("embeds") or [])
        ],
    }


def extract_links_from_messages(messages: List[dict]) -> List[dict]:
    links: List[dict] = []
    for m in messages:
        found = list(_LINK_RE.findall(m.get("content") or ""))
        for e in m.get("embeds") or []:
            if e.get("url"):
                found.append(e["url"])
        for a in m.get("attachments") or []:
            if a.get("url"):
                found.append(a["url"])
        seen = set()
        for url in found:
            if url in seen:
                continue
            seen.add(url)
            links.append({
                "url": url,
                "message_id": m.get("id"),
                "channel_id": m.get("channel_id"),
                "author": (m.get("author") or {}).get("username"),
                "timestamp": m.get("timestamp"),
            })
    return links


async def run_discord_operation(bot_token: str, operation: str,
                                args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a read-only Discord operation. Raises DiscordOpsError on failure."""
    args = args or {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        if operation == "list_servers":
            guilds = await _bot_get(client, bot_token, "/users/@me/guilds")
            return {"servers": [
                {"id": g.get("id"), "name": g.get("name")} for g in guilds
            ]}

        if operation == "list_channels":
            guild_id = _require_snowflake(args, "guild_id")
            channels = await _bot_get(client, bot_token, f"/guilds/{guild_id}/channels")
            return {"channels": [
                {
                    "id": c.get("id"), "name": c.get("name"),
                    "type": c.get("type"), "topic": c.get("topic"),
                    "parent_id": c.get("parent_id"),
                    "position": c.get("position"),
                }
                for c in channels
            ]}

        if operation in ("read_messages", "extract_links"):
            channel_id = _require_snowflake(args, "channel_id")
            params: Dict[str, Any] = {
                "limit": max(1, min(int(args.get("limit") or 25), 100)),
            }
            for cursor in ("before", "after", "around"):
                val = str(args.get(cursor) or "").strip()
                if val:
                    if not val.isdigit():
                        raise DiscordOpsError(f"'{cursor}' must be a message ID")
                    params[cursor] = val
            raw = await _bot_get(client, bot_token,
                                 f"/channels/{channel_id}/messages", params)
            messages = [_simplify_message(m) for m in raw]
            if operation == "extract_links":
                return {"links": extract_links_from_messages(messages),
                        "message_count": len(messages)}
            return {"messages": messages}

        if operation == "download_attachment":
            url = str(args.get("url") or "").strip()
            if not url:
                raise DiscordOpsError(
                    "'url' is required — use the attachment url from read_messages")
            parts = urlsplit(url)
            if parts.scheme != "https" or (parts.hostname or "").lower() not in ATTACHMENT_HOSTS:
                raise DiscordOpsError(
                    "Attachment URL must be an https URL on "
                    + " or ".join(sorted(ATTACHMENT_HOSTS)))
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code >= 400:
                raise DiscordOpsError(
                    f"Attachment fetch failed ({resp.status_code}) — Discord "
                    "attachment URLs expire; re-read the message to get a fresh one")
            data = resp.content[:ATTACHMENT_MAX_BYTES]
            filename = parts.path.rsplit("/", 1)[-1] or "attachment.bin"
            return {"attachment": {
                "filename": filename,
                "content_type": resp.headers.get("content-type", ""),
                "size": len(data),
                "truncated": len(resp.content) > ATTACHMENT_MAX_BYTES,
                "content_b64": base64.b64encode(data).decode(),
            }}

        raise DiscordOpsError(
            f"Unknown operation '{operation}'. Supported: list_servers, "
            "list_channels, read_messages, extract_links, download_attachment")
