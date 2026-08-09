"""Per-agent task-board threads in the shared TASK_BOARD_CHANNEL_ID channel.

Each agent maintains ONE thread in the fleet's task-board channel — a
public, mobile-friendly view of everything on that agent's plate:

  * a pinned/first "board" message, edited in place, grouped kanban-style
    (🎯 goal, 📋 pending, 🔧 active, ⏸ blocked/review, ✅ recently done,
    📨 dispatch chains assigned to it), and
  * a rolling feed of update posts below it (task created/moved/completed,
    goal set/paused/done, dispatch chain assigned/finished).

Data sources (all read-only, polled):
  * kanban:   ``hermes_cli.kanban_db`` (this service's SQLite board);
              items assigned to this agent.
  * goal:     ``state_meta`` rows ``goal:*`` in this service's state.db
              (read-only SQLite connection; each service holds only its
              own agent's sessions).
  * dispatch: active chains in the shared Redis dispatch store where this
              agent is the assignee.

Redis is used for the thread/message ids and the last-posted snapshot so
updates survive restarts and are diffed, not re-spammed. Gated on
TASK_BOARD_CHANNEL_ID + REDIS_URL — with either missing this module is a
no-op. Loop-safety: this manager only WRITES to its own thread; agents
never react to task-board posts (bot-authored posts are filtered by the
normal adapter rules).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

POLL_SECONDS = float(os.getenv("TASK_BOARD_POLL_SECONDS", "60"))
DONE_WINDOW_SECONDS = 48 * 3600  # "recently done" horizon
MAX_LIST = 12                    # per-section cap in the board message

_STATUS_PENDING = {"triage", "todo", "scheduled", "ready"}
_STATUS_ACTIVE = {"running"}
_STATUS_HELD = {"blocked", "review"}


def task_board_channel_id() -> str:
    return (os.getenv("TASK_BOARD_CHANNEL_ID") or "").strip()


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _redis():
    from tools import agent_dispatch as store
    return store._redis()


# ----------------------------------------------------------------------
# snapshot collection (sync helpers, always called via asyncio.to_thread)
# ----------------------------------------------------------------------

def _collect_kanban(agent: str) -> List[Dict[str, Any]]:
    """Kanban items assigned to this agent (best-effort; [] on any failure)."""
    try:
        from hermes_cli import kanban_db
    except Exception:
        return []
    try:
        conn = kanban_db.connect()
    except Exception:
        logger.debug("taskboard: kanban connect failed", exc_info=True)
        return []
    try:
        out: List[Dict[str, Any]] = []
        now = time.time()
        for t in kanban_db.list_tasks(conn, assignee=agent):
            status = (t.status or "").lower()
            if status == "done":
                done_at = _parse_ts(getattr(t, "completed_at", None))
                if done_at and now - done_at > DONE_WINDOW_SECONDS:
                    continue
            out.append({
                "kind": "kanban",
                "id": str(t.id),
                "title": (t.title or "")[:120],
                "status": status,
            })
        return out
    except Exception:
        logger.debug("taskboard: kanban list failed", exc_info=True)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _parse_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def _collect_goals() -> List[Dict[str, Any]]:
    """Active/paused goals from this service's state.db (read-only)."""
    try:
        from hermes_state import DEFAULT_DB_PATH
        db_path = os.getenv("HERMES_STATE_DB") or str(DEFAULT_DB_PATH)
    except Exception:
        return []
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT key, value FROM state_meta WHERE key LIKE 'goal:%'"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        logger.debug("taskboard: goal read failed", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for key, raw in rows:
        try:
            g = json.loads(raw)
        except Exception:
            continue
        status = (g.get("status") or "").lower()
        if status not in ("active", "paused"):
            continue
        out.append({
            "kind": "goal",
            "id": str(key),
            "title": (g.get("goal") or "")[:160],
            "status": status,
        })
    return out


_LEGACY_STATUS_MAP = {
    "pending": "todo", "in_progress": "running",
    "blocked": "blocked", "completed": "done", "done": "done",
    "cancelled": "done",
}


def _collect_legacy_board(agent: str) -> List[Dict[str, Any]]:
    """Tasks assigned to this agent on the fleet's Redis task board
    (``skills/task_board`` — the board agents' SOUL prompts use today)."""
    try:
        r = _redis()
        out: List[Dict[str, Any]] = []
        now = time.time()
        for tid in r.zrevrange("taskboard:index", 0, 200):
            raw = r.get(f"taskboard:task:{tid}")
            if not raw:
                continue
            try:
                t = json.loads(raw)
            except ValueError:
                continue
            if (t.get("assignee") or "").lower() != agent:
                continue
            status = _LEGACY_STATUS_MAP.get(
                (t.get("status") or "").lower(), (t.get("status") or "").lower())
            if status == "done":
                done_at = _parse_ts(t.get("updated_at"))
                if done_at and now - done_at > DONE_WINDOW_SECONDS:
                    continue
            out.append({
                "kind": "kanban",  # rendered/grouped identically
                "id": str(t.get("id") or tid),
                "title": (t.get("title") or "")[:120],
                "status": status,
            })
        return out
    except Exception:
        logger.debug("taskboard: legacy board read failed", exc_info=True)
        return []


def _collect_dispatch(agent: str) -> List[Dict[str, Any]]:
    """Active dispatch chains where this agent is the assignee."""
    try:
        from tools import agent_dispatch as store
        r = store._redis()
        out: List[Dict[str, Any]] = []
        for cid in r.smembers("dispatch:active"):
            chain = store.get_chain(r, cid)
            if not chain or (chain.get("to") or "").lower() != agent:
                continue
            out.append({
                "kind": "dispatch",
                "id": str(chain["id"]),
                "title": (chain.get("task") or "")[:120],
                "status": (chain.get("status") or "").lower(),
                "from": (chain.get("from") or "").lower(),
                "thread_id": str(chain.get("thread_id") or ""),
            })
        return out
    except Exception:
        logger.debug("taskboard: dispatch read failed", exc_info=True)
        return []


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------

_KANBAN_EMOJI = {
    "triage": "🟡", "todo": "🟡", "scheduled": "🗓️", "ready": "🟢",
    "running": "🔧", "blocked": "⛔", "review": "🔎", "done": "✅",
}
_DISPATCH_EMOJI = {
    "pending": "📨", "acked": "👀", "working": "🔧",
    "waiting": "❓", "done": "✅", "failed": "❌", "cancelled": "🚫",
}


def _line(item: Dict[str, Any]) -> str:
    if item["kind"] == "goal":
        icon = "🎯" if item["status"] == "active" else "⏸"
        return f"{icon} {item['title']}"
    if item["kind"] == "dispatch":
        icon = _DISPATCH_EMOJI.get(item["status"], "📨")
        link = f" → <#{item['thread_id']}>" if item.get("thread_id") else ""
        return f"{icon} chain #{item['id']} from **{item.get('from', '?')}**: {item['title']}{link}"
    icon = _KANBAN_EMOJI.get(item["status"], "•")
    return f"{icon} `{item['id']}` {item['title']}"


def render_board(agent: str, snapshot: Dict[str, Dict[str, Any]]) -> str:
    """Compact kanban-style board message (edited in place)."""
    items = list(snapshot.values())
    goals = [i for i in items if i["kind"] == "goal"]
    disp = [i for i in items if i["kind"] == "dispatch"]
    kb = [i for i in items if i["kind"] == "kanban"]
    pend = [i for i in kb if i["status"] in _STATUS_PENDING]
    act = [i for i in kb if i["status"] in _STATUS_ACTIVE]
    held = [i for i in kb if i["status"] in _STATUS_HELD]
    done = [i for i in kb if i["status"] == "done"]

    lines = [f"📌 **{agent.capitalize()} — task board**"]
    if goals:
        lines.append("")
        lines.append("**🎯 Goal**")
        lines += [_line(g) for g in goals[:3]]
    if disp:
        lines.append("")
        lines.append("**📨 Dispatch chains**")
        lines += [_line(d) for d in disp[:MAX_LIST]]

    def section(name: str, rows: List[Dict[str, Any]]) -> None:
        lines.append("")
        lines.append(f"**{name}**")
        if rows:
            lines.extend(_line(x) for x in rows[:MAX_LIST])
            if len(rows) > MAX_LIST:
                lines.append(f"-# …and {len(rows) - MAX_LIST} more")
        else:
            lines.append("-# none")

    section("📋 Pending", pend)
    section("🔧 Active", act)
    if held:
        section("⏸ Blocked / review", held)
    section("✅ Done (48h)", done)
    lines.append("")
    lines.append(f"-# auto-updated · <t:{int(time.time())}:R>")
    return "\n".join(lines)[:3900]


def diff_updates(old: Dict[str, Dict[str, Any]],
                 new: Dict[str, Dict[str, Any]]) -> List[str]:
    """Rolling-feed lines for what changed between two snapshots."""
    updates: List[str] = []
    for key, item in new.items():
        prev = old.get(key)
        if prev is None:
            if item["kind"] == "goal":
                updates.append(f"🎯 new goal: {item['title']}")
            elif item["kind"] == "dispatch":
                updates.append("📨 new dispatch " + _line(item)[2:].strip())
            elif item["status"] != "done":
                updates.append(f"🆕 `{item['id']}` {item['title']} → **{item['status']}**")
            continue
        if prev.get("status") != item["status"]:
            icon = "✅" if item["status"] == "done" else \
                _KANBAN_EMOJI.get(item["status"]) or \
                _DISPATCH_EMOJI.get(item["status"]) or "🔀"
            label = item["title"] if item["kind"] != "kanban" else \
                f"`{item['id']}` {item['title']}"
            updates.append(f"{icon} {label}: **{prev['status']}** → **{item['status']}**")
    for key, item in old.items():
        if key in new:
            continue
        if item["kind"] == "goal":
            updates.append(f"🏁 goal finished: {item['title']}")
        elif item["kind"] == "dispatch":
            updates.append(f"🏁 chain #{item['id']} closed: {item['title']}")
        # kanban done-items simply age out of the 48h window — not an event
    return updates


def snapshot_key(item: Dict[str, Any]) -> str:
    return f"{item['kind']}:{item['id']}"


# ----------------------------------------------------------------------
# manager
# ----------------------------------------------------------------------

class TaskBoardManager:
    """Maintains this agent's thread in the shared task-board channel."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.agent = _agent_name()
        self.channel_id = task_board_channel_id()
        self._task: Optional[asyncio.Task] = None
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(self.channel_id) and bool(os.getenv("REDIS_URL", "").strip())

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[%s] taskboard: started (channel=%s)", self.agent, self.channel_id)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._started = False

    async def _loop(self) -> None:
        await asyncio.sleep(20)  # let the adapter settle after connect
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] taskboard tick failed", self.agent)
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self) -> None:
        agent = self.agent
        kb, legacy, goals, disp = await asyncio.gather(
            asyncio.to_thread(_collect_kanban, agent),
            asyncio.to_thread(_collect_legacy_board, agent),
            asyncio.to_thread(_collect_goals),
            asyncio.to_thread(_collect_dispatch, agent),
        )
        snapshot = {snapshot_key(i): i for i in (goals + disp + kb + legacy)}

        r = await asyncio.to_thread(_redis)
        raw_old = await asyncio.to_thread(r.get, f"taskboard:snapshot:{agent}")
        first_run = raw_old is None
        try:
            old = json.loads(raw_old) if raw_old else {}
        except ValueError:
            old = {}

        thread = await self._ensure_thread(r)
        if thread is None:
            return

        # Rolling updates (skip on very first run — the board message is
        # enough; a restart with an existing snapshot diffs normally).
        if not first_run:
            updates = diff_updates(old, snapshot)
            for i in range(0, len(updates), 8):
                await thread.send("\n".join(updates[i:i + 8])[:1990])

        # Board message: edit in place; only touch Discord when it changed.
        board = render_board(agent, snapshot)
        prev_board = await asyncio.to_thread(r.get, f"taskboard:board_render:{agent}")
        # strip the volatile timestamp line before comparing
        strip = lambda s: "\n".join((s or "").splitlines()[:-1])
        if strip(prev_board) != strip(board) or first_run:
            await self._upsert_board_message(r, thread, board)
            await asyncio.to_thread(r.set, f"taskboard:board_render:{agent}", board)

        await asyncio.to_thread(
            r.set, f"taskboard:snapshot:{agent}",
            json.dumps(snapshot, ensure_ascii=False))

    # ------------------------------------------------------------------
    async def _ensure_thread(self, r):
        """Fetch (or create) this agent's thread in the board channel."""
        tid = await asyncio.to_thread(r.get, f"taskboard:thread:{self.agent}")
        if tid:
            try:
                return await self._get_channel(tid)
            except Exception:
                logger.info("[%s] taskboard: stored thread %s unusable; recreating",
                            self.agent, tid)
        try:
            channel = await self._get_channel(self.channel_id)
        except Exception:
            logger.warning("[%s] taskboard: board channel %s unreachable",
                           self.agent, self.channel_id)
            return None
        thread = await channel.create_thread(
            name=f"📌 {self.agent.capitalize()} — tasks"[:100],
            type=self._public_thread_type(),
            auto_archive_duration=10080,
        )
        await asyncio.to_thread(r.set, f"taskboard:thread:{self.agent}", str(thread.id))
        # thread changed → the old board message id is useless
        await asyncio.to_thread(r.delete, f"taskboard:board_msg:{self.agent}")
        return thread

    async def _upsert_board_message(self, r, thread, board: str) -> None:
        msg_id = await asyncio.to_thread(r.get, f"taskboard:board_msg:{self.agent}")
        if msg_id:
            try:
                msg = await thread.fetch_message(int(msg_id))
                await msg.edit(content=board)
                return
            except Exception:
                logger.info("[%s] taskboard: board message %s gone; reposting",
                            self.agent, msg_id)
        msg = await thread.send(board)
        try:
            await msg.pin()
        except Exception:
            logger.debug("[%s] taskboard: could not pin board message", self.agent)
        await asyncio.to_thread(r.set, f"taskboard:board_msg:{self.agent}", str(msg.id))

    async def _get_channel(self, channel_id: str):
        client = self.adapter._client
        ch = client.get_channel(int(channel_id))
        if ch is None:
            ch = await client.fetch_channel(int(channel_id))
        if ch is None:
            raise RuntimeError(f"channel {channel_id} not found")
        return ch

    def _public_thread_type(self):
        import discord
        return discord.ChannelType.public_thread
