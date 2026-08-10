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
# Minimum spacing between charter-initiated goal turns (kickoffs, answer
# deliveries). Redis-backed so it survives restarts — this is what stops a
# crash/redeploy loop from re-kicking the goal on every boot.
KICK_COOLDOWN_SECONDS = float(os.getenv("CHARTER_KICK_COOLDOWN_SECONDS", "600"))
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
    def _charter_source(self):
        """The ONE source identity for everything the charter does.

        Session contract: the standing charter runs in a dedicated session
        keyed by the home channel + the synthetic user ``charter`` (channel
        sessions are per-user by default). The goal manager lookup and every
        injected turn use THIS source, so the goal that gets armed and the
        turns that drive it live in the exact same session — while Garrett's
        own messages in the channel (his user_id) map to a different session,
        which is why a charter can never collide with an owner-typed /goal
        mid-conversation.
        """
        return self.adapter.build_source(
            chat_id=_home_channel_id(),
            chat_name="home",
            chat_type="channel",
            user_id="charter",
            user_name="charter",
        )

    def _goal_manager(self, runner):
        """GoalManager bound to the charter session (or None)."""
        try:
            from gateway.platforms.base import MessageEvent, MessageType
            event = MessageEvent(
                text="", message_type=MessageType.TEXT,
                source=self._charter_source(), internal=True,
            )
            mgr, _entry = runner._get_goal_manager_for_event(event)
            return mgr
        except Exception:
            logger.exception("[%s] charter: goal manager lookup failed", self.agent)
            return None

    async def _kick_allowed(self, r) -> bool:
        """True when the per-agent injection cooldown has elapsed.

        The timestamp lives in Redis (``goalcharter:v1:lastkick:<agent>``),
        NOT in process memory: a restart loop (crash → redeploy → boot) must
        not reset the clock, or every boot re-kicks the goal and feeds the
        loop. When cooling down we simply skip the injection this tick; the
        durable state (applied marker / consumed flag) is not advanced, so
        the next tick past the cooldown retries.
        """
        from tools import goal_charter as store
        key = store._k("lastkick", self.agent)
        last = await asyncio.to_thread(r.get, key)
        try:
            last_ts = float(last) if last else 0.0
        except (TypeError, ValueError):
            last_ts = 0.0
        return (time.time() - last_ts) >= KICK_COOLDOWN_SECONDS

    async def _record_kick(self, r) -> None:
        from tools import goal_charter as store
        key = store._k("lastkick", self.agent)
        await asyncio.to_thread(r.set, key, str(time.time()))

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
                if not await self._kick_allowed(r):
                    return
                mgr.resume()
                await self._inject_turn(
                    "[Charter] Turn budget refreshed — continue your standing "
                    "mission. Review your goal and take the next concrete step."
                )
                await self._record_kick(r)
            return

        # Throttle: at most one charter kickoff per cooldown window, tracked
        # in Redis so restart loops can't re-kick on every boot. Skipping
        # here leaves the applied marker unset, so a later tick retries.
        if not await self._kick_allowed(r):
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
        await self._record_kick(r)
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

        # Owner answers are delivered promptly, NOT gated on the kick
        # cooldown: they are owner-initiated (rate-limited by Garrett
        # himself) and cannot feed a restart loop. The cooldown exists to
        # stop boot/re-arm kickoffs from re-firing every crash cycle.
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
            await self._record_kick(r)

    # ------------------------------------------------------------------
    async def _inject_turn(self, text: str) -> None:
        """Inject an internal MessageEvent and wait for the turn to finish.

        ``adapter.handle_message()`` is fire-and-forget — it spawns the turn
        as a background task and returns immediately. Returning here without
        an acknowledgement would let callers persist their durable markers
        (applied rev / consumed flag) for a turn that never actually ran.
        The charter uses a synthetic user ("charter"), so its session key is
        private: the task registered under that key after handle_message()
        is OUR injected turn. Await it (bounded) as the delivery ack; any
        failure raises so the caller's deliver-then-mark ordering retries on
        a later tick.
        """
        from gateway.platforms.base import MessageEvent, MessageType, build_session_key
        source = self._charter_source()
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        cfg_extra = getattr(getattr(self.adapter, "config", None), "extra", {}) or {}
        session_key = build_session_key(
            source,
            group_sessions_per_user=cfg_extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=cfg_extra.get("thread_sessions_per_user", False),
        )
        session_tasks = getattr(self.adapter, "_session_tasks", {})
        # If a previous turn on the charter session is still running,
        # handle_message would QUEUE this event instead of scheduling it —
        # and completion of the old task is not delivery of ours. Refuse up
        # front; durable markers stay unset and the next tick retries.
        prev = session_tasks.get(session_key)
        if prev is not None and not prev.done():
            raise RuntimeError(
                f"charter session {session_key} busy; deferring injection"
            )
        await self.adapter.handle_message(event)
        task = session_tasks.get(session_key)
        if task is None or task is prev:
            # No NEW task appeared for our event: it was queued, merged, or
            # dropped — treat as undelivered so the caller retries.
            raise RuntimeError(
                f"charter injection for {session_key} was not scheduled"
            )
        # Bounded wait: a wedged turn must not freeze the tick loop forever.
        # shield() keeps the turn itself alive if we time out.
        await asyncio.wait_for(asyncio.shield(task), timeout=15 * 60)
        exc = task.exception() if task.done() else None
        if exc is not None:
            raise exc
