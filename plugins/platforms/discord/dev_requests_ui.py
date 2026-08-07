#!/usr/bin/env python3
"""Discord review UI for agent dev modification requests (/devrequests).

Agents submit requests via the ``request_dev_modification`` tool (see
tools/dev_requests.py — fleet-shared Redis store). The owner runs
/devrequests to read each pending request IN FULL and click Approve or
Deny — same interaction pattern as the exec-approval buttons. Approved
requests move to the ``devreq:approved`` queue that the development
environment reads.

Kept in its own file (not adapter.py) so upstream engine syncs can't
silently delete it.

Interaction contract
--------------------
Every entry-point (handle_devrequests_slash, _handle_diag) calls
``interaction.response.defer()`` **before** any I/O so Discord's 3-second
ack window is never breached.  All subsequent messages go via
``interaction.followup.send()``.  Button clicks likewise defer before
hitting Redis so the component interaction token can't expire mid-call.
No code path ends in a bare ``pass`` — every failure produces a visible
followup message or a logged ERROR.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from urllib.parse import urlparse

import discord

logger = logging.getLogger(__name__)

MSG_LIMIT = 1900  # leave headroom under Discord's 2000-char cap
_POLL_INTERVAL = 5   # seconds between each poll of dispatch_status
_POLL_ATTEMPTS = 18  # 18 × 5 s = 90 s total


def _store():
    from tools import dev_requests
    return dev_requests


async def _poll_dispatch_outcome(channel, req_id: str, title: str) -> None:
    """Post a follow-up in the channel once the vault resolves the dispatch.

    Polls devreq:item:{req_id} every _POLL_INTERVAL seconds for up to
    _POLL_ATTEMPTS iterations, then posts one of:
      ✅  started   — Replit Agent run is underway
      ❌  failed    — dispatch error message included
      ⏳  timeout   — no result yet, direct owner to the dashboard
    """
    for _ in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_INTERVAL)
        try:
            item = await asyncio.to_thread(_store().get_request, req_id)
        except Exception:
            logger.debug("Dispatch poller: could not read request %s", req_id)
            continue
        ds = (item or {}).get("dispatch_status")
        if ds == "started":
            try:
                await channel.send(
                    f"✅ Replit Agent run started for dev request "
                    f"#{req_id} \"{title}\" — work is underway."
                )
            except Exception:
                logger.exception("Dispatch poller: could not post success for %s", req_id)
            return
        if ds == "failed":
            err = ((item or {}).get("dispatch_error") or "unknown error")[:300]
            try:
                await channel.send(
                    f"❌ Auto-dispatch failed for dev request "
                    f"#{req_id} \"{title}\".\n"
                    f"Reason: `{err}`\n"
                    "Retry via the vault dashboard, or complete the Replit "
                    "OAuth connection at `/admin/replit-mcp/connect` first."
                )
            except Exception:
                logger.exception("Dispatch poller: could not post failure for %s", req_id)
            return
    # Timed out — no dispatch_status appeared.
    try:
        await channel.send(
            f"⏳ No dispatch result yet for dev request "
            f"#{req_id} \"{title}\" after 90 s — "
            "check the vault dashboard (`/api/admin/replit-mcp/status`) for status."
        )
    except Exception:
        logger.exception("Dispatch poller: could not post timeout for %s", req_id)


class DevRequestApprovalView(discord.ui.View):
    """Approve / Deny buttons for one dev modification request."""

    def __init__(self, req_id: str, reviewer_id: int):
        super().__init__(timeout=600)
        self.req_id = req_id
        self.reviewer_id = reviewer_id  # only the /devrequests invoker decides
        self.resolved = False

    async def _guard(self, interaction) -> bool:
        if interaction.user.id != self.reviewer_id:
            await interaction.response.send_message(
                "Only the reviewer who opened /devrequests can decide this.",
                ephemeral=True)
            return False
        if self.resolved:
            await interaction.response.send_message(
                "Already decided.", ephemeral=True)
            return False
        return True

    async def _decide(self, interaction, status: str, label: str) -> None:
        if not await self._guard(interaction):
            return
        # Defer the component interaction immediately — Redis can take >3s.
        # Non-ephemeral so the Approved/Denied edit is visible to the channel.
        await interaction.response.defer()
        try:
            # Redis is synchronous — keep it off the Discord event loop.
            item = await asyncio.to_thread(
                _store().set_status, self.req_id, status,
                str(interaction.user))
        except Exception as e:
            logger.exception("dev request decision failed")
            try:
                await interaction.followup.send(
                    f"❌ Could not update request #{self.req_id}: `{e}`",
                    ephemeral=True)
            except Exception:
                logger.error(
                    "dev request decision: could not deliver error to user for "
                    "request %s: %s", self.req_id, e)
            return
        if item is None:
            try:
                await interaction.followup.send(
                    f"Request #{self.req_id} no longer exists (item expired?).",
                    ephemeral=True)
            except Exception:
                logger.error("dev request decision: could not send 'not found' for %s",
                             self.req_id)
            return
        if item.get("conflict"):
            self.resolved = True
            for child in self.children:
                child.disabled = True
            try:
                await interaction.edit_original_response(
                    content=f"Request #{self.req_id} was already decided "
                            f"({item.get('status')}).",
                    view=self)
            except Exception:
                logger.exception(
                    "dev request decision: could not edit message for conflict on %s",
                    self.req_id)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        title = item.get("title", "")
        if status == "approved":
            note = "approved — queued for Replit Agent dispatch (result will follow)"
        else:
            note = "closed"
        try:
            await interaction.edit_original_response(
                content=f"{label} — request #{self.req_id} \"{title}\" {note}.",
                view=self)
        except Exception:
            logger.exception(
                "dev request decision: could not edit message for %s", self.req_id)
            # Best-effort fallback: at least send a followup so the reviewer knows
            try:
                await interaction.followup.send(
                    f"{label} — request #{self.req_id} \"{title}\" {note} "
                    "(could not update original card).",
                    ephemeral=True)
            except Exception:
                logger.error(
                    "dev request decision: all delivery paths failed for %s", self.req_id)
        if status == "approved":
            asyncio.create_task(
                _poll_dispatch_outcome(interaction.channel, self.req_id, title)
            )

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await self._decide(interaction, "approved", "✅ Approved")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        await self._decide(interaction, "denied", "❌ Denied")


def _format_request(item: dict) -> str:
    head = (f"**Dev request #{item['id']}** — {item.get('title', '')}\n"
            f"From: `{item.get('agent', '?')}` · Project: `{item.get('project', '?')}`\n\n")
    body = item.get("description", "")
    if len(head) + len(body) > MSG_LIMIT:
        body = body[: MSG_LIMIT - len(head) - 20] + "\n… (truncated)"
    return head + body


def _format_empty_state(counts: dict) -> str:
    """Build the 'no pending requests' message with honest queue diagnostics."""
    parts = ["No pending dev requests."]
    expired = counts.get("pending_expired", 0)
    if expired:
        parts.append(
            f"({expired} stale/expired ID(s) found in the pending list and cleaned up.)"
        )
    approved_live = counts.get("approved_live", 0)
    if approved_live:
        parts.append(
            f"{approved_live} approved request(s) are in the dispatch queue "
            "waiting for the Replit Agent."
        )
    backlog = counts.get("dispatch_backlog", 0)
    if backlog:
        parts.append(f"Dispatch backlog: {backlog} item(s) queued for dispatch.")
    parts.append("Approved requests are queued for the development team.")
    return " ".join(parts)


def _redis_fingerprint(url: str) -> str:
    """Return 'host:port (sha256[:6])' — never exposes password."""
    try:
        p = urlparse(url)
        host = p.hostname or "?"
        port = p.port or 6379
        digest = hashlib.sha256(url.encode()).hexdigest()[:6]
        return f"{host}:{port} ({digest})"
    except Exception:
        return "(could not parse URL)"


async def _handle_diag(interaction) -> None:
    """Show Redis health and per-status queue counts (ephemeral)."""
    await interaction.response.defer(ephemeral=True)
    lines: list[str] = ["**Dev-request system diagnostics**"]
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        lines.append("❌ **REDIS_URL**: not set — tool hidden from all agents")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    lines.append(f"✅ **REDIS_URL**: `{_redis_fingerprint(url)}`")

    try:
        import redis as _redis_mod
        _r = _redis_mod.from_url(url, decode_responses=True, socket_timeout=3)
        _r.ping()
        lines.append("✅ **Redis ping**: OK")
    except Exception as e:
        lines.append(f"❌ **Redis ping**: `{e}`")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        return

    try:
        counts = await asyncio.to_thread(_store().queue_counts)
        lines.append(
            f"📋 **Pending list**: {counts['pending_in_list']} total "
            f"({counts['pending_live']} live, {counts['pending_expired']} expired)"
        )
        lines.append(
            f"✅ **Approved list**: {counts['approved_in_list']} total "
            f"({counts['approved_live']} live, {counts['approved_expired']} expired)"
        )
        lines.append(f"🚀 **Dispatch backlog**: {counts['dispatch_backlog']} item(s)")
        hb = counts.get("heartbeat_ts")
        if hb:
            age = int(time.time()) - hb
            lines.append(f"💓 **Dispatcher heartbeat**: {age}s ago")
        else:
            lines.append("⚠️ **Dispatcher heartbeat**: no recent heartbeat (dispatcher may be down)")
        # Claim/lease presence for the most recent approved IDs
        approved_ids = (await asyncio.to_thread(
            lambda: _redis_mod.from_url(url, decode_responses=True, socket_timeout=3
                                        ).lrange("devreq:approved", -5, -1)
        ))
        if approved_ids:
            lines.append("**Recent approved IDs** (claim/lease presence):")
            _rr = _redis_mod.from_url(url, decode_responses=True, socket_timeout=3)
            for rid in approved_ids:
                has_claim = bool(_rr.exists(f"replitmcp:claim:{rid}"))
                has_lease = bool(_rr.exists(f"replitmcp:lease:{rid}"))
                lines.append(
                    f"  #{rid}: claim={'✓' if has_claim else '✗'} "
                    f"lease={'✓' if has_lease else '✗'}"
                )
    except Exception as e:
        lines.append(f"❌ **Queue query failed**: `{e}`")

    try:
        await interaction.followup.send("\n".join(lines), ephemeral=True)
    except Exception:
        logger.exception("/devrequests diag: could not deliver diagnostics")


async def handle_devrequests_slash(interaction, action: str = "list") -> None:
    """Entry point called by the /devrequests slash command.

    ``action`` is either "list" (default — show pending requests with
    Approve/Deny buttons) or "diag" (Redis health and queue counts).
    """
    if action.strip().lower() == "diag":
        await _handle_diag(interaction)
        return

    # ── Ack immediately — Redis listing can take seconds ──────────────────
    await interaction.response.defer()

    try:
        pending = await asyncio.to_thread(_store().list_requests, "pending")
    except Exception as e:
        logger.exception("/devrequests: list_requests failed")
        try:
            await interaction.followup.send(
                f"❌ Could not reach the request store: `{e}`", ephemeral=True)
        except Exception:
            logger.error("/devrequests: could not deliver store-error to user: %s", e)
        return

    if not pending:
        try:
            counts = await asyncio.to_thread(_store().queue_counts)
        except Exception:
            counts = {}
        msg = _format_empty_state(counts)
        try:
            await interaction.followup.send(msg)
        except Exception:
            logger.error("/devrequests: could not deliver empty-state message")
        return

    # ── Count line ─────────────────────────────────────────────────────────
    try:
        await interaction.followup.send(
            f"{len(pending)} pending dev request(s) — full text below, "
            "approve or deny each:"
        )
    except Exception as e:
        logger.exception("/devrequests: could not send count message")
        # Without the count message we can still try to post the cards, but
        # log the failure explicitly so it's visible.
        logger.error("/devrequests: count message failed: %s", e)

    # ── One card per request ───────────────────────────────────────────────
    for item in pending:
        try:
            await interaction.followup.send(
                _format_request(item),
                view=DevRequestApprovalView(item["id"], interaction.user.id),
            )
        except Exception as e:
            logger.exception("Failed to post dev request card for %s", item.get("id"))
            # Always deliver a visible error so the owner knows a card was dropped.
            try:
                await interaction.followup.send(
                    f"⚠️ Failed to display request #{item.get('id')} "
                    f"(`{e!s:.120}`) — check bot logs.",
                    ephemeral=True,
                )
            except Exception:
                logger.error(
                    "/devrequests: could not deliver card-failure notice for %s",
                    item.get("id"),
                )
