"""Charter manager — arms and resumes the /goal engine from the agent's
persistent goal charter, and runs the owner-question loop.

Responsibilities (all polled, best-effort):

  * Boot resume: if the agent has an active charter in Redis and the home
    channel session has no active goal (or the charter changed), (re)set
    the goal from the charter and inject a kickoff turn. This is what makes
    a standing goal survive a redeploy — the local /goal state may be gone,
    the charter is not.
  * Owner questions: surface newly filed ``ask_owner`` questions in the
    agent's home channel; while any are pending, PARK the goal loop with a
    wait barrier (refreshed each tick) so the judge neither burns turns nor
    terminates the goal; when answers arrive, clear the barrier and inject
    the answers as a turn so the agent continues.

Safety rails:
  * Never clobbers an owner-typed /goal — the charter only (re)sets a goal
    when there is no active goal, or when the active goal carries the
    charter tag.
  * Gated on REDIS_URL + DISCORD_HOME_CHANNEL + gateway runner; a no-op
    otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

POLL_SECONDS = float(os.getenv("CHARTER_POLL_SECONDS", "60"))
WAIT_HORIZON_SECONDS = 6 * 3600   # park window; refreshed while questions pend
WAIT_REASON_PREFIX = "awaiting owner answers (ask_owner)"


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _home_channel_id() -> str:
    return (os.getenv("DISCORD_HOME_CHANNEL") or "").strip()


def _redis():
    from tools import goal_charter as store
    return store._redis()


class CharterManager:
    """Arms /goal from the persistent charter and runs the question loop."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.agent = _agent_name()
        self._task: Optional[asyncio.Task] = None
        self._started = False

    @property
    def enabled(self) -> bool:
        return bool(os.getenv("REDIS_URL", "").strip()) and bool(_home_channel_id())

    def start(self) -> None:
        if self._started or not self.enabled:
            return
        self._started = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[%s] charter: started (home=%s)", self.agent, _home_channel_id())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._started = False

    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        await asyncio.sleep(25)  # let the adapter/gateway settle
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] charter tick failed", self.agent)
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self) -> None:
        from tools import goal_charter as store

        runner = getattr(self.adapter, "gateway_runner", None)
        if runner is None:
            return
        r = await asyncio.to_thread(_redis)
        charter = await asyncio.to_thread(store.load_charter, r, self.agent)

        mgr = self._goal_manager(runner)
        if mgr is None:
            return

        # 1) arm / re-arm the goal from the charter
        if charter is not None and charter.get("status") == "active":
            await self._ensure_goal_armed(store, r, mgr, charter)
        elif charter is not None and charter.get("status") == "paused":
            # Owner paused the charter — pause a charter-armed goal too.
            state = mgr.state
            if (state is not None and state.status == "active"
                    and store.is_charter_goal(state.goal)):
                mgr.pause(reason="charter paused by owner")

        # 2) owner-question loop (runs even when charter is paused so
        #    already-filed questions still surface and round-trip)
        await self._question_tick(store, r, mgr)

    # ------------------------------------------------------------------
    def _goal_manager(self, runner):
        """GoalManager bound to the home-channel session (or None)."""
        try:
            from gateway.platforms.base import MessageEvent, MessageType
            source = self.adapter.build_source(
                chat_id=_home_channel_id(),
                chat_name="home",
                chat_type="channel",
                user_id="charter",
                user_name="charter",
            )
            event = MessageEvent(
                text="", message_type=MessageType.TEXT, source=source, internal=True,
            )
            mgr, _entry = runner._get_goal_manager_for_event(event)
            return mgr
        except Exception:
            logger.exception("[%s] charter: goal manager lookup failed", self.agent)
            return None

    async def _ensure_goal_armed(self, store, r, mgr, charter: Dict[str, Any]) -> None:
        rendered = store.render_goal_text(self.agent, charter)
        applied_key = store._k("applied", self.agent)
        applied_rev = await asyncio.to_thread(r.get, applied_key)
        rev = str(charter.get("rev", 0))

        state = mgr.state
        goal_active = state is not None and state.status in ("active", "paused")
        # Never clobber an owner-typed goal.
        if goal_active and not store.is_charter_goal(state.goal):
            return
        # Nothing to do when this revision is already armed and alive.
        if goal_active and applied_rev == rev:
            # Resume a goal the engine auto-paused (budget) so the standing
            # mission keeps going; owner pauses go through charter status.
            if (state.status == "paused"
                    and (state.paused_reason or "").startswith("turn budget")):
                mgr.resume()
                await self._inject_turn(
                    "[Charter] Turn budget refreshed — continue your standing "
                    "mission. Review your goal and take the next concrete step."
                )
            return

        try:
            mgr.set(rendered)
        except ValueError as exc:
            logger.warning("[%s] charter: goal set rejected: %s", self.agent, exc)
            return
        # Inject the kickoff turn BEFORE finalizing the applied marker: if
        # delivery fails (adapter/home channel hiccup) the next tick re-arms
        # and retries instead of leaving a goal that never takes a turn.
        # mgr.set() is safe to repeat — it just resets the same goal text.
        await self._inject_turn(
            "[Charter] Your standing mission was (re)armed after a restart or "
            "charter update:\n\n" + rendered +
            "\n\n[Pick up where you left off. Check your notes, task board, "
            "and dispatch chains for in-flight work before starting anything "
            "new. If you are in the discovery phase, file information gaps "
            "with the ask_owner tool.]"
        )
        await asyncio.to_thread(r.set, applied_key, rev)
        logger.info("[%s] charter: goal armed (rev %s)", self.agent, rev)

    # ------------------------------------------------------------------
    async def _question_tick(self, store, r, mgr) -> None:
        questions = await asyncio.to_thread(store.list_questions, r, self.agent)
        pending = [q for q in questions if q["status"] == "pending"]
        fresh_answers = [q for q in questions
                         if q["status"] == "answered" and not q.get("consumed")]

        # Surface new questions in the home channel.
        for q in pending:
            if q.get("posted"):
                continue
            try:
                await self.adapter.send(
                    _home_channel_id(),
                    f"❓ **Question for Garrett** (#{q['id']}):\n{q['question']}\n"
                    f"-# answer with `/charter answer {q['id']} <text>`",
                )
                q["posted"] = True
                await asyncio.to_thread(store.save_question, r, self.agent, q)
            except Exception:
                logger.warning("[%s] charter: question post failed", self.agent,
                               exc_info=True)

        state = mgr.state
        goal_active = state is not None and state.status == "active"

        if pending and goal_active:
            # (Re)park the goal while questions are outstanding. Refresh the
            # horizon each tick; never overwrite someone else's barrier.
            reason = f"{WAIT_REASON_PREFIX}: #" + ", #".join(str(q["id"]) for q in pending)
            if not mgr.is_waiting() or (state.waiting_reason or "").startswith(WAIT_REASON_PREFIX):
                try:
                    mgr.wait_for_seconds(WAIT_HORIZON_SECONDS, reason=reason)
                except Exception:
                    logger.debug("[%s] charter: wait refresh failed", self.agent,
                                 exc_info=True)

        if fresh_answers:
            # Clear OUR barrier (only ours) and hand the answers to the agent.
            if goal_active and mgr.is_waiting() and \
                    (state.waiting_reason or "").startswith(WAIT_REASON_PREFIX):
                if not pending:
                    mgr.stop_waiting()
            lines = ["[Charter] Garrett answered your question(s):", ""]
            for q in fresh_answers:
                lines.append(f"Q#{q['id']}: {q['question']}")
                lines.append(f"A: {q['answer']}")
                lines.append("")
            lines.append(
                "[Fold these answers into your understanding of the business "
                "(update your notes/memory), then continue your standing "
                "mission. If everything you needed for the discovery phase is "
                "now answered, move on to execution.]"
            )
            # Deliver FIRST, then mark consumed — a failed injection must be
            # retried on the next tick, never silently swallow an answer.
            await self._inject_turn("\n".join(lines))
            for q in fresh_answers:
                q["consumed"] = True
                await asyncio.to_thread(store.save_question, r, self.agent, q)

    # ------------------------------------------------------------------
    async def _inject_turn(self, text: str) -> None:
        """Inject an internal MessageEvent keyed to the home channel."""
        from gateway.platforms.base import MessageEvent, MessageType
        source = self.adapter.build_source(
            chat_id=_home_channel_id(),
            chat_name="home",
            chat_type="channel",
            user_id="charter",
            user_name="charter",
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await self.adapter.handle_message(event)
