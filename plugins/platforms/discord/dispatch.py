"""Discord-side machinery for inter-agent task dispatch.

Companion to ``tools/agent_dispatch.py`` (the Redis store + agent-facing
tool). This module runs inside each agent's Discord adapter and provides:

  * roster publishing   — announce this agent (name, Discord user id, role,
                          tool names) to ``dispatch:roster:<agent>``.
  * outbox drainer      — perform Discord actions queued by the tool
                          (open chain threads, post protocol messages,
                          swap status reactions).
  * intake watcher      — pop ``dispatch:inbox:<agent>`` events, ack orders
                          with a 👀 reaction (one-ack dedup via SETNX), and
                          inject each event as an internal MessageEvent turn
                          so the agent starts working without any chat text.
  * Harmony PM watcher  — only when AGENT_NAME == harmony: nudge stalled
                          multi-agent chains, escalate to the owner, and
                          keep a pinned capabilities roster message fresh.

Loop-safety design: agents NEVER trigger each other through Discord messages.
All agent-to-agent signalling flows through Redis; the Discord thread is the
human-auditable mirror. The adapter's ``_handle_message`` gate (see
``dispatch_gate_allows``) makes dispatch threads reaction/tool-only for
agents while keeping the owner's messages authoritative.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

POLL_SECONDS = 3.0
PM_POLL_SECONDS = 60.0
STALL_SECONDS = int(os.getenv("DISPATCH_STALL_SECONDS", "900"))
ACK_TTL = 7 * 24 * 3600


def dispatch_channel_id() -> str:
    return (os.getenv("DISPATCH_CHANNEL_ID") or "").strip()


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _owner_id() -> str:
    """Owner Discord user id — same fail-closed semantics as the vault gate."""
    try:
        try:
            from vault_ui import _resolve_vault_owner_id
        except ImportError:
            from .vault_ui import _resolve_vault_owner_id
        return _resolve_vault_owner_id()
    except Exception:
        return (os.getenv("DISCORD_OWNER_ID") or "").strip()


def _store():
    """Import the shared Redis store module lazily (it needs tools.registry)."""
    from tools import agent_dispatch as store
    return store


class DispatchManager:
    """Per-adapter dispatch runtime. Constructed once, started post-connect."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.agent = _agent_name()
        self.channel_id = dispatch_channel_id()
        self._tasks: list[asyncio.Task] = []
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self.channel_id) and bool(os.getenv("REDIS_URL", "").strip())

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._tasks.append(asyncio.create_task(self._safe(self._publish_roster())))
        self._tasks.append(asyncio.create_task(self._safe(self._outbox_loop())))
        self._tasks.append(asyncio.create_task(self._safe(self._intake_loop())))
        if self.agent == "harmony":
            self._tasks.append(asyncio.create_task(self._safe(self._pm_loop())))
        logger.info("[%s] dispatch: started (channel=%s, pm=%s)",
                    self.agent, self.channel_id, self.agent == "harmony")

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        self._started = False

    async def _safe(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] dispatch background task died", self.agent)

    def _redis(self):
        return _store()._redis()

    # ------------------------------------------------------------------
    # roster
    # ------------------------------------------------------------------
    async def _publish_roster(self) -> None:
        """Publish this agent's roster entry, then refresh periodically so it
        never TTLs out while the agent is alive."""
        boot_republishes = [180, 240]  # extra publishes at ~3min and ~7min
        while True:
            try:
                await self._publish_roster_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] dispatch roster publish failed", self.agent)
            if boot_republishes:
                # Re-publish a few times shortly after boot: the first
                # publish races the vault grant sync (default refresh 300s),
                # so vault_* tools are usually still missing from the
                # registry at that point.
                await asyncio.sleep(boot_republishes.pop(0))
                continue
            await asyncio.sleep(6 * 3600)

    # Shared core dispatch/communication tools every agent has, published
    # alongside the agent's own granted vault_* tools. The full registry
    # snapshot is deliberately NOT published — it listed identical local
    # tooling for every agent and made Harmony route work by noise.
    CORE_ROSTER_TOOLS = (
        "agent_dispatch",
        "vault",
        "ask_owner",
        "request_dev_modification",
    )

    def _roster_tools(self) -> list[str]:
        """This agent's granted ``vault_*`` tools plus the shared core
        dispatch tools — not the entire local tool registry."""
        names: set[str] = set()
        try:
            from tools.registry import registry as _reg
            entries = None
            if hasattr(_reg, "snapshot"):
                entries, _checks = _reg.snapshot()
            if entries is not None:
                names = {e.name for e in entries}
            else:
                names = set(getattr(_reg, "_tools", {}).keys())
        except Exception:
            names = set()
        tools = sorted(n for n in names if n.startswith("vault_"))
        # Core tools are shared by every agent; include them unconditionally —
        # at boot the roster can publish before tools.agent_dispatch/vault
        # have registered, and an empty tool list misleads Harmony.
        tools += list(self.CORE_ROSTER_TOOLS)
        return tools

    async def _publish_roster_once(self) -> None:
        client = getattr(self.adapter, "_client", None)
        user = getattr(client, "user", None) if client else None
        tools = self._roster_tools()
        entry = {
            "agent": self.agent,
            "_v": 2,
            "discord_user_id": str(getattr(user, "id", "") or ""),
            "role": (os.getenv("AGENT_ROLE") or "").strip(),
            "tools": tools[:120],
            "updated_at": int(time.time()),
        }
        store = _store()
        r = await asyncio.to_thread(self._redis)
        _payload = json.dumps(entry, ensure_ascii=False)
        await asyncio.to_thread(
            lambda: r.set(f"dispatch:roster:{self.agent}", _payload, ex=store.CHAIN_TTL)
        )
        logger.info("[%s] dispatch: roster published (%d tools)", self.agent, len(tools))

    # ------------------------------------------------------------------
    # outbox — Discord actions queued by the tool
    # ------------------------------------------------------------------
    async def _outbox_loop(self) -> None:
        key = f"dispatch:outbox:{self.agent}"
        while True:
            action = None
            r = None
            try:
                r = await asyncio.to_thread(self._redis)
                raw = await asyncio.to_thread(r.lpop, key)
                if not raw:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                try:
                    action = json.loads(raw)
                except ValueError:
                    continue
                await self._perform_action(r, action)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] dispatch outbox action failed", self.agent)
                # Requeue with a bounded retry count so a transient Discord
                # failure doesn't permanently lose the action.
                if r is not None and isinstance(action, dict):
                    retries = int(action.get("retries", 0)) + 1
                    if retries <= 5:
                        action["retries"] = retries
                        try:
                            await asyncio.to_thread(
                                r.rpush, key, json.dumps(action, ensure_ascii=False))
                        except Exception:
                            logger.exception("[%s] dispatch outbox requeue failed",
                                             self.agent)
                    else:
                        logger.error("[%s] dispatch: dropping outbox action after "
                                     "%d retries: %s", self.agent, retries, action)
                await asyncio.sleep(POLL_SECONDS)

    async def _perform_action(self, r, action: Dict[str, Any]) -> None:
        store = _store()
        kind = action.get("kind")
        chain_id = str(action.get("chain_id") or "")
        chain = store.get_chain(r, chain_id) if chain_id else None
        if not chain:
            logger.warning("[%s] dispatch: outbox action for missing chain %s",
                           self.agent, chain_id)
            return
        if kind == "open_chain":
            await self._open_chain(r, store, chain)
        elif kind == "post":
            await self._post_in_thread(r, store, chain, action)
        elif kind == "react":
            await self._swap_reaction(chain, action.get("add"), action.get("remove"))

    def _mention_for(self, r, store, who: str) -> str:
        who = (who or "").lower()
        if who in ("owner", "garrett"):
            oid = _owner_id()
            return f"<@{oid}>" if oid else "@owner"
        entry = store.roster_entry(r, who)
        uid = (entry or {}).get("discord_user_id")
        return f"<@{uid}>" if uid else f"**{who}**"

    async def _get_channel(self, channel_id: str):
        client = self.adapter._client
        ch = client.get_channel(int(channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(channel_id))
        return ch

    async def _open_chain(self, r, store, chain: Dict[str, Any]) -> None:
        """Create the chain thread in the dispatch channel and post the order.

        Idempotent: a retry after a partial failure reuses the existing
        thread instead of opening a duplicate; the assignee-side SETNX ack
        claim dedups a re-pushed order event.
        """
        if chain.get("thread_id"):
            store.push_inbox(r, chain["to"], {"kind": "order", "chain_id": chain["id"]})
            return
        channel = await self._get_channel(self.channel_id)
        title = f"#{chain['id']} {chain['from']}→{chain['to']}: {chain['task'][:60]}"
        thread = await channel.create_thread(
            name=title[:100],
            type=self._public_thread_type(),
            auto_archive_duration=1440,
        )
        mention = self._mention_for(r, store, chain["to"])
        parent_note = f" (sub-task of #{chain['parent_id']})" if chain.get("parent_id") else ""
        order_text = (
            f"📨 **Dispatch order #{chain['id']}**{parent_note}\n"
            f"{mention} — from **{chain['from']}**:\n\n"
            f"{chain['task']}\n\n"
            f"-# Protocol: ack by reaction only · work silently · post text "
            f"only for a question or the final result · status: 👀 received, "
            f"🔧 working, ❓ question, ✅ done, ❌ failed"
        )
        msg = await thread.send(order_text)
        chain["thread_id"] = str(thread.id)
        chain["order_message_id"] = str(msg.id)
        store.save_chain(r, chain)
        await asyncio.to_thread(
            lambda: r.set(f"dispatch:thread:{thread.id}", chain["id"], ex=store.CHAIN_TTL))
        # Hand the order to the assignee's intake watcher.
        store.push_inbox(r, chain["to"], {"kind": "order", "chain_id": chain["id"]})
        logger.info("[%s] dispatch: opened chain %s thread %s → %s",
                    self.agent, chain["id"], thread.id, chain["to"])

    @staticmethod
    def _public_thread_type():
        try:
            import discord
            return discord.ChannelType.public_thread
        except Exception:
            return None

    async def _post_in_thread(self, r, store, chain: Dict[str, Any],
                              action: Dict[str, Any]) -> None:
        if not chain.get("thread_id"):
            logger.warning("[%s] dispatch: chain %s has no thread yet; requeueing post",
                           self.agent, chain["id"])
            store.push_outbox(r, self.agent, action)
            await asyncio.sleep(POLL_SECONDS)
            return
        thread = await self._get_channel(chain["thread_id"])
        mention = self._mention_for(r, store, action.get("mention") or "")
        text = action.get("text") or ""
        await thread.send(f"{mention} {text}"[:1990])
        add, remove = action.get("react_add"), action.get("react_remove")
        if add or remove:
            await self._swap_reaction(chain, add, remove)

    async def _swap_reaction(self, chain: Dict[str, Any],
                             add: Optional[str], remove: Optional[str]) -> None:
        if not chain.get("thread_id") or not chain.get("order_message_id"):
            return
        try:
            thread = await self._get_channel(chain["thread_id"])
            msg = await thread.fetch_message(int(chain["order_message_id"]))
        except Exception as e:
            logger.warning("[%s] dispatch: cannot fetch order message for chain %s: %s",
                           self.agent, chain["id"], e)
            return
        if remove:
            await self.adapter._remove_reaction(msg, remove)
        if add:
            await self.adapter._add_reaction(msg, add)

    # ------------------------------------------------------------------
    # intake — events addressed to this agent
    # ------------------------------------------------------------------
    async def _intake_loop(self) -> None:
        key = f"dispatch:inbox:{self.agent}"
        while True:
            event = None
            r = None
            try:
                r = await asyncio.to_thread(self._redis)
                raw = await asyncio.to_thread(r.lpop, key)
                if not raw:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                await self._handle_inbox_event(r, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] dispatch intake failed", self.agent)
                if r is not None and isinstance(event, dict):
                    retries = int(event.get("retries", 0)) + 1
                    if retries <= 5:
                        event["retries"] = retries
                        try:
                            await asyncio.to_thread(
                                r.rpush, key, json.dumps(event, ensure_ascii=False))
                        except Exception:
                            logger.exception("[%s] dispatch intake requeue failed",
                                             self.agent)
                    else:
                        logger.error("[%s] dispatch: dropping inbox event after "
                                     "%d retries: %s", self.agent, retries, event)
                await asyncio.sleep(POLL_SECONDS)

    async def _handle_inbox_event(self, r, event: Dict[str, Any]) -> None:
        store = _store()
        kind = event.get("kind")
        chain = store.get_chain(r, str(event.get("chain_id") or ""))
        if not chain:
            return
        if kind == "order":
            # One-ack-per-order dedup: SETNX claim.
            claimed = await asyncio.to_thread(
                lambda: r.set(f"dispatch:ack:{chain['id']}", self.agent,
                              nx=True, ex=ACK_TTL))
            if not claimed:
                logger.info("[%s] dispatch: order %s already acked; skipping",
                            self.agent, chain["id"])
                return
            await self._swap_reaction(chain, "👀", None)
            chain["status"] = "acked"
            try:
                # Guarded: only a still-pending order becomes acked. If the
                # dispatcher cancelled (or anything else raced us), skip the
                # injection instead of resurrecting a terminal chain.
                await asyncio.to_thread(
                    store.save_chain_guarded, r, chain, {"pending"})
            except RuntimeError as e:
                logger.info("[%s] dispatch: not injecting order %s: %s",
                            self.agent, chain["id"], e)
                return
            text = (
                f"[Dispatch order #{chain['id']} from {chain['from']}]\n"
                f"{chain['task']}\n\n"
                f"[Protocol — follow strictly: you have already acked this "
                f"order with a 👀 reaction. Do NOT post any chat message in "
                f"the dispatch thread. Work on the task now, silently. Use "
                f"the agent_dispatch tool for ALL communication: "
                f"action='working' (chain_id={chain['id']}) when you start, "
                f"action='question' if you are blocked, and "
                f"action='complete' with the result text when done. If your "
                f"final response text would normally go to the user, put it "
                f"in the complete action's text instead and end your turn "
                f"with no chat output.]"
            )
        elif kind == "question":
            text = (
                f"[Dispatch chain #{chain['id']}: {chain.get('asked_by') or chain['to']} "
                f"asked YOU a question]\n{chain.get('question') or ''}\n\n"
                f"[Answer via the agent_dispatch tool: action='answer', "
                f"chain_id={chain['id']}, text=<your answer>. Do not post in "
                f"the dispatch thread directly.]"
            )
        elif kind == "answer":
            text = (
                f"[Dispatch chain #{chain['id']}: {event.get('from')} answered "
                f"your question]\n{event.get('answer') or ''}\n\n"
                f"[Resume the paused task now. Continue using the "
                f"agent_dispatch tool for status/completion; no chat posts "
                f"in the dispatch thread.]"
            )
        elif kind == "completed":
            outcome = "succeeded ✅" if event.get("success", True) else "FAILED ❌"
            text = (
                f"[Dispatch chain #{chain['id']} to {event.get('from')} {outcome}]\n"
                f"Result: {event.get('result') or '(none)'}\n\n"
                f"[This was a task you dispatched. Integrate the result and "
                f"continue your own work. If YOUR work was itself a dispatch "
                f"order, report via agent_dispatch action='complete' on your "
                f"own chain. Only post chat text if the owner is waiting on "
                f"you in a normal conversation.]"
            )
        elif kind == "cancelled":
            text = (
                f"[Dispatch chain #{chain['id']} was CANCELLED by "
                f"{event.get('by')}: {event.get('reason') or 'no reason given'}]\n"
                f"[Stop work on it. Do not post in the dispatch thread.]"
            )
        else:
            return
        await self._inject_turn(chain, text)

    async def _inject_turn(self, chain: Dict[str, Any], text: str) -> None:
        """Inject an internal MessageEvent so the agent takes a turn.

        The session is keyed to the chain's Discord thread so that any
        accidental conversational output at least lands in the right thread.
        """
        from gateway.platforms.base import MessageEvent, MessageType
        thread_id = chain.get("thread_id") or self.channel_id
        source = self.adapter.build_source(
            chat_id=str(thread_id),
            chat_name=f"dispatch #{chain['id']}",
            chat_type="thread",
            user_id="dispatch",
            user_name="dispatch",
            thread_id=str(thread_id) if chain.get("thread_id") else None,
            parent_chat_id=self.channel_id,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await self.adapter.handle_message(event)

    # ------------------------------------------------------------------
    # Harmony PM watcher — observer on multi-agent chains only
    # ------------------------------------------------------------------
    async def _pm_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(PM_POLL_SECONDS)
                r = await asyncio.to_thread(self._redis)
                await self._pm_tick(r)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] dispatch PM tick failed", self.agent)

    async def _pm_tick(self, r) -> None:
        store = _store()
        await self._refresh_roster_pin(r, store)
        now = int(time.time())
        for cid in await asyncio.to_thread(r.smembers, "dispatch:active"):
            chain = store.get_chain(r, cid)
            if not chain:
                await asyncio.to_thread(r.srem, "dispatch:active", cid)
                continue
            # Multi-agent = has children or >2 participants on the root.
            root = store.get_chain(r, chain.get("root_id") or cid) or chain
            has_children = bool(await asyncio.to_thread(
                r.llen, f"dispatch:children:{cid}"))
            multi = has_children or len(root.get("agents", [])) > 2 or bool(chain.get("parent_id"))
            if not multi:
                continue
            if self.agent in (chain.get("from"), chain.get("to")):
                continue  # Harmony is a participant, not an observer, here
            age = now - int(chain.get("updated_at") or chain.get("created_at") or now)
            if chain.get("status") in ("acked", "working") and age > STALL_SECONDS:
                # First stall → nudge the assignee; second → escalate to owner.
                nudged = await asyncio.to_thread(
                    lambda: r.set(f"dispatch:nudged:{cid}", "1",
                                  nx=True, ex=STALL_SECONDS))
                try:
                    if nudged:
                        mention = self._mention_for(r, store, chain["to"])
                        await self._pm_post(chain,
                            f"🕰️ {mention} — chain #{cid} has been quiet for "
                            f"{age // 60} min. Still on it? Update status via "
                            f"the agent_dispatch tool (working/question/complete).")
                    else:
                        escalated = await asyncio.to_thread(
                            lambda: r.set(f"dispatch:escalated:{cid}", "1",
                                          nx=True, ex=4 * STALL_SECONDS))
                        if escalated:
                            owner = self._mention_for(r, store, "owner")
                            await self._pm_post(chain,
                                f"🚨 {owner} — chain #{cid} ({chain['from']}→"
                                f"{chain['to']}) looks stalled ({age // 60} min "
                                f"without progress after a nudge).")
                except Exception:
                    logger.exception("[%s] dispatch PM nudge failed for %s",
                                     self.agent, cid)

    async def _pm_post(self, chain: Dict[str, Any], text: str) -> None:
        if not chain.get("thread_id"):
            return
        thread = await self._get_channel(chain["thread_id"])
        await thread.send(text[:1990])

    async def _refresh_roster_pin(self, r, store) -> None:
        """Keep a pinned capabilities roster message fresh (hourly)."""
        fresh = await asyncio.to_thread(
            lambda: r.set("dispatch:roster_pin_lock", "1", nx=True, ex=3600))
        if not fresh:
            return
        roster = store.get_roster(r)
        if not roster:
            return
        lines = ["📋 **Fleet dispatch roster** (auto-generated)", ""]
        for e in sorted(roster, key=lambda x: x.get("agent") or ""):
            role = f" — {e['role']}" if e.get("role") else ""
            tools = e.get("tools") or []
            vault_tools = [t for t in tools if t.startswith("vault_")]
            summary = ", ".join(vault_tools[:8]) or f"{len(tools)} tools"
            lines.append(f"• **{e.get('agent')}**{role} · {summary}")
        lines.append("")
        lines.append("-# Dispatch with the agent_dispatch tool. "
                     "Protocol: reaction ack · silent work · text only for "
                     "questions and results.")
        content = "\n".join(lines)[:1990]
        channel = await self._get_channel(self.channel_id)
        msg_id = await asyncio.to_thread(r.get, "dispatch:roster_msg")
        msg = None
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
            except Exception:
                msg = None
        if msg is not None:
            if getattr(msg, "content", None) != content:
                await msg.edit(content=content)
        else:
            msg = await channel.send(content)
            try:
                await msg.pin()
            except Exception:
                logger.info("[%s] dispatch: could not pin roster message", self.agent)
            await asyncio.to_thread(
                lambda: r.set("dispatch:roster_msg", str(msg.id)))

    def owner_steering_bypass(self, author_id: str, channel_ids) -> bool:
        """Fail-closed allowlist exception for owner steering.

        True only when: dispatch is enabled, the message's channel-id set
        (channel + thread parent) contains the dispatch channel, an owner id
        is configured, and the author IS that owner.
        """
        if not self.enabled or not channel_ids:
            return False
        if self.channel_id not in {str(c) for c in channel_ids}:
            return False
        oid = _owner_id()
        return bool(oid) and bool(author_id) and str(author_id) == oid

    # ------------------------------------------------------------------
    # owner steering / gate helper (called from adapter._handle_message)
    # ------------------------------------------------------------------
    async def gate(self, message: Any, parent_channel_id: Optional[str],
                   is_thread: bool) -> Optional[str]:
        """Decide what to do with a Discord message in dispatch territory.

        Returns:
          None       — not dispatch territory; process normally.
          "drop"     — dispatch territory, this agent must stay silent.
          "steer"    — owner steering addressed to this agent: process the
                       message (the caller proceeds with normal handling).
        """
        if not self.enabled:
            return None
        chan_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        in_dispatch = (
            chan_id == self.channel_id
            or (is_thread and parent_channel_id == self.channel_id)
        )
        if not in_dispatch:
            return None
        author = getattr(message, "author", None)
        author_id = str(getattr(author, "id", "") or "")
        is_owner = bool(author_id) and author_id == _owner_id()
        if not is_owner:
            # Agents and other users never converse in dispatch territory.
            return "drop"
        if not is_thread:
            # Owner talking in the dispatch channel root: normal handling
            # (mention rules apply) — only threads carry chain semantics.
            return None
        # Owner message inside a chain thread → route to the current assignee.
        # All Redis I/O off the event loop (a Redis timeout must not stall
        # Discord message handling).
        try:
            store = _store()

            def _lookup():
                r = store._redis()
                chain_id = r.get(f"dispatch:thread:{chan_id}")
                return r, (store.get_chain(r, chain_id) if chain_id else None)

            r, chain = await asyncio.to_thread(_lookup)
        except Exception:
            logger.exception("[%s] dispatch gate: Redis lookup failed", self.agent)
            return "drop"
        if not chain:
            return "drop"
        if chain.get("status") == "waiting" and chain.get("waiting_on") == "owner":
            # Owner answered a question — the ASKER resumes.
            asker = chain.get("asked_by") or chain.get("to")
            if asker != self.agent:
                return "drop"
            chain["status"] = "working" if asker == chain.get("to") else "acked"
            chain["waiting_on"] = ""
            try:
                await asyncio.to_thread(
                    store.save_chain_guarded, r, chain, {"waiting"})
            except RuntimeError:
                # Raced with the tool-side answer path — the chain already
                # moved on; still steer so the owner's text reaches the agent.
                pass
            return "steer"
        # General steering (redirect / cancel / extra instructions) →
        # exactly one agent responds: the assignee.
        if chain.get("to") != self.agent:
            return "drop"
        return "steer"
