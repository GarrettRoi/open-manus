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
"""

from __future__ import annotations

import asyncio
import logging

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
        try:
            # Redis is synchronous — keep it off the Discord event loop.
            item = await asyncio.to_thread(
                _store().set_status, self.req_id, status,
                str(interaction.user))
        except Exception as e:
            logger.exception("dev request decision failed")
            await interaction.response.send_message(
                f"Could not update request #{self.req_id}: {e}", ephemeral=True)
            return
        if item is None:
            await interaction.response.send_message(
                f"Request #{self.req_id} no longer exists.", ephemeral=True)
            return
        if item.get("conflict"):
            self.resolved = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"Request #{self.req_id} was already decided "
                        f"({item.get('status')}).",
                view=self)
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        title = item.get("title", "")
        if status == "approved":
            note = "approved — queued for Replit Agent dispatch (result will follow)"
        else:
            note = "closed"
        await interaction.response.edit_message(
            content=f"{label} — request #{self.req_id} \"{title}\" {note}.",
            view=self)
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


async def handle_devrequests_slash(interaction) -> None:
    """List pending dev requests, each with its own Approve/Deny buttons."""
    try:
        pending = await asyncio.to_thread(_store().list_requests, "pending")
    except Exception as e:
        await interaction.response.send_message(
            f"Could not reach the request store: {e}", ephemeral=True)
        return
    if not pending:
        await interaction.response.send_message(
            "No pending dev requests. (Approved ones are queued for the "
            "development team.)", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{len(pending)} pending dev request(s) — full text below, "
        "approve or deny each:")
    for item in pending:
        try:
            await interaction.followup.send(
                _format_request(item),
                view=DevRequestApprovalView(item["id"], interaction.user.id),
            )
        except Exception:
            logger.exception("Failed to post dev request %s", item.get("id"))
