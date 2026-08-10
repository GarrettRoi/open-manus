"""Tests for the persistent goal charter store + owner-question queue."""

import json
import sys
import types
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

sys.modules.setdefault("discord", MagicMock())

from tools import goal_charter as store


@pytest.fixture
def r():
    return fakeredis.FakeRedis(decode_responses=True)


# ----------------------------------------------------------------------
# charter records
# ----------------------------------------------------------------------

def test_charter_roundtrip_and_rev_bump(r):
    assert store.load_charter(r, "jade") is None
    saved = store.save_charter(r, "jade", {
        "objectives": ["Run Vows & Vinyl end to end"],
        "phase": "discovery", "status": "active",
    })
    assert saved["rev"] == 1
    loaded = store.load_charter(r, "jade")
    assert loaded["objectives"] == ["Run Vows & Vinyl end to end"]
    assert loaded["mandate"]  # default mandate filled in
    saved2 = store.save_charter(r, "jade", loaded)
    assert saved2["rev"] == 2


def test_load_charter_rejects_empty_objectives(r):
    r.set("goalcharter:v1:charter:jade", json.dumps({"objectives": []}))
    assert store.load_charter(r, "jade") is None


def test_charter_namespace_is_exclusive(r):
    store.save_charter(r, "jade", {"objectives": ["x"]})
    keys = r.keys("*")
    assert all(k.startswith("goalcharter:v1:") for k in keys)


# ----------------------------------------------------------------------
# goal text render
# ----------------------------------------------------------------------

def test_render_goal_text_tagged_and_phased(r):
    charter = store.save_charter(r, "jade", {
        "objectives": ["Fast client responses"], "phase": "discovery",
    })
    text = store.render_goal_text("jade", charter)
    assert store.is_charter_goal(text)
    assert "Fast client responses" in text
    assert "ask_owner" in text
    assert "discovery" in text
    charter["phase"] = "execution"
    text2 = store.render_goal_text("jade", charter)
    assert "execution" in text2 and "Phase 'execution'" in text2


def test_is_charter_goal_rejects_manual_goal():
    assert not store.is_charter_goal("build a rocket")
    assert not store.is_charter_goal("")


# ----------------------------------------------------------------------
# question queue
# ----------------------------------------------------------------------

def test_question_lifecycle(r):
    q = store.file_question(r, "jade", "Which DJ packages do we sell?")
    assert q["id"] == 1 and q["status"] == "pending"
    assert not q["posted"] and not q["consumed"]

    pending = store.list_questions(r, "jade", status="pending")
    assert [p["id"] for p in pending] == [1]

    ans = store.answer_question(r, "jade", 1, "Three tiers: basic/plus/premium")
    assert ans["status"] == "answered"
    assert store.list_questions(r, "jade", status="pending") == []
    assert store.list_questions(r, "jade", status="answered")[0]["answer"].startswith("Three tiers")

    with pytest.raises(RuntimeError):
        store.answer_question(r, "jade", 1, "again")
    with pytest.raises(KeyError):
        store.answer_question(r, "jade", 99, "nope")


def test_file_question_caps_pending(r):
    for i in range(store.QUESTIONS_OPEN_MAX):
        store.file_question(r, "jade", f"q{i}")
    with pytest.raises(RuntimeError):
        store.file_question(r, "jade", "one too many")
    # answering one frees a slot
    store.answer_question(r, "jade", 1, "a")
    store.file_question(r, "jade", "now it fits")


def test_file_question_requires_text(r):
    with pytest.raises(ValueError):
        store.file_question(r, "jade", "   ")


def test_ask_owner_tool_files_question(r, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "jade")
    with patch.object(store, "_redis", return_value=r):
        out = json.loads(store.ask_owner_tool({"question": "Who is our venue contact?"}))
    assert out["ok"] and out["question_id"] == 1
    assert store.list_questions(r, "jade", status="pending")


# ----------------------------------------------------------------------
# CharterManager pure logic
# ----------------------------------------------------------------------

def test_charter_manager_never_clobbers_manual_goal(r, monkeypatch):
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")
    monkeypatch.setenv("REDIS_URL", "redis://fake")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})

    mgr = MagicMock()
    state = MagicMock()
    state.status = "active"
    state.goal = "manual goal typed by Garrett"
    mgr.state = state

    manager = cm.CharterManager(adapter=MagicMock())
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.set.assert_not_called()


def test_charter_manager_arms_when_no_goal(r, monkeypatch):
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})

    mgr = MagicMock()
    mgr.state = None

    manager = cm.CharterManager(adapter=MagicMock())
    injected = []

    async def fake_inject(text):
        injected.append(text)

    manager._inject_turn = fake_inject
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.set.assert_called_once()
    assert store.is_charter_goal(mgr.set.call_args[0][0])
    assert injected and "standing mission" in injected[0]
    # applied marker written → second tick is a no-op
    mgr.reset_mock()
    state = MagicMock()
    state.status = "active"
    state.goal = store.render_goal_text("jade", charter)
    state.paused_reason = None
    mgr.state = state
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.set.assert_not_called()


def test_charter_manager_rearms_on_rev_change(r, monkeypatch):
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})
    manager = cm.CharterManager(adapter=MagicMock())

    async def fake_inject(text):
        pass

    manager._inject_turn = fake_inject

    mgr = MagicMock()
    mgr.state = None
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.set.assert_called_once()

    # owner edits the charter → rev bumps → re-arm even though goal active
    # (clear the kick cooldown so this test exercises rev logic, not throttle)
    r.set("goalcharter:v1:lastkick:jade", "0")
    charter2 = store.save_charter(r, "jade", charter)
    state = MagicMock()
    state.status = "active"
    state.goal = store.render_goal_text("jade", charter)
    mgr.reset_mock()
    mgr.state = state
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter2))
    mgr.set.assert_called_once()


def test_failed_kickoff_injection_retries_next_tick(r, monkeypatch):
    """Applied marker must not persist when the kickoff turn fails."""
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})
    manager = cm.CharterManager(adapter=MagicMock())

    async def failing_inject(text):
        raise RuntimeError("home channel down")

    manager._inject_turn = failing_inject
    mgr = MagicMock()
    mgr.state = None
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.get_event_loop().run_until_complete(
            manager._ensure_goal_armed(store, r, mgr, charter))
    assert r.get("goalcharter:v1:applied:jade") is None  # marker not written

    # next tick: injection recovers → marker written
    injected = []

    async def ok_inject(text):
        injected.append(text)

    manager._inject_turn = ok_inject
    asyncio.get_event_loop().run_until_complete(
        manager._ensure_goal_armed(store, r, mgr, charter))
    assert injected and r.get("goalcharter:v1:applied:jade") == "1"


def test_failed_answer_injection_not_consumed(r, monkeypatch):
    """Answers must stay unconsumed (retryable) when injection fails."""
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    store.file_question(r, "jade", "which venues?")
    store.answer_question(r, "jade", 1, "the usual three")

    manager = cm.CharterManager(adapter=MagicMock())

    async def failing_inject(text):
        raise RuntimeError("adapter down")

    manager._inject_turn = failing_inject
    mgr = MagicMock()
    mgr.state = None
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.get_event_loop().run_until_complete(
            manager._question_tick(store, r, mgr))
    assert not store.get_question(r, "jade", 1)["consumed"]

    # recovery tick: delivered exactly once, then consumed
    injected = []

    async def ok_inject(text):
        injected.append(text)

    manager._inject_turn = ok_inject
    asyncio.get_event_loop().run_until_complete(
        manager._question_tick(store, r, mgr))
    assert len(injected) == 1 and "the usual three" in injected[0]
    assert store.get_question(r, "jade", 1)["consumed"]
    # a further tick injects nothing new
    asyncio.get_event_loop().run_until_complete(
        manager._question_tick(store, r, mgr))
    assert len(injected) == 1


def test_charter_manager_stop_start_lifecycle(monkeypatch):
    """Disconnect/reconnect: stop cancels the task and start() re-arms."""
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    import asyncio

    async def scenario():
        manager = cm.CharterManager(adapter=MagicMock())
        assert manager.enabled
        manager.start()
        task1 = manager._task
        assert manager._started and task1 is not None
        # idempotent while running
        manager.start()
        assert manager._task is task1

        manager.stop()  # disconnect path
        assert not manager._started and manager._task is None
        await asyncio.sleep(0)
        assert task1.cancelled() or task1.done()

        manager.start()  # reconnect path
        task2 = manager._task
        assert manager._started and task2 is not None and task2 is not task1
        manager.stop()
        await asyncio.sleep(0)

    asyncio.get_event_loop().run_until_complete(scenario())


def test_adapter_disconnect_stops_charter_manager():
    """disconnect() must stop the charter manager (source-level guard)."""
    import inspect
    from plugins.platforms.discord import adapter as ad
    src = inspect.getsource(ad.DiscordAdapter.disconnect)
    assert "_charter_manager" in src and ".stop()" in src


def test_kick_cooldown_throttles_rearm(r, monkeypatch):
    """A restart within the cooldown window must NOT re-kick the goal."""
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})
    manager = cm.CharterManager(adapter=MagicMock())
    injected = []

    async def ok_inject(text):
        injected.append(text)

    manager._inject_turn = ok_inject
    mgr = MagicMock()
    mgr.state = None
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter))
    assert len(injected) == 1
    assert r.get("goalcharter:v1:lastkick:jade") is not None

    # simulate a crash-loop restart: applied marker wiped locally is
    # irrelevant (it's in Redis), but even a rev bump within the cooldown
    # must not inject again
    charter2 = store.save_charter(r, "jade", charter)
    mgr.reset_mock()
    mgr.state = None
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter2))
    mgr.set.assert_not_called()
    assert len(injected) == 1

    # cooldown elapsed → re-arm proceeds
    r.set("goalcharter:v1:lastkick:jade", "0")
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter2))
    mgr.set.assert_called_once()
    assert len(injected) == 2


def test_answer_delivery_ignores_kick_cooldown(r, monkeypatch):
    """An owner answer filed right after a kickoff resumes on the NEXT tick —
    it must not wait out the 10-minute kick cooldown."""
    from plugins.platforms.discord import charter as cm

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    store.file_question(r, "jade", "q?")
    store.answer_question(r, "jade", 1, "a")
    import time as _time
    r.set("goalcharter:v1:lastkick:jade", str(_time.time()))  # just kicked

    manager = cm.CharterManager(adapter=MagicMock())
    injected = []

    async def ok_inject(text):
        injected.append(text)

    manager._inject_turn = ok_inject
    mgr = MagicMock()
    mgr.state = None
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(manager._question_tick(store, r, mgr))
    assert len(injected) == 1 and "a" in injected[0]
    assert store.get_question(r, "jade", 1)["consumed"]
    # exactly once
    loop.run_until_complete(manager._question_tick(store, r, mgr))
    assert len(injected) == 1


# ----------------------------------------------------------------------
# _inject_turn delivery acknowledgement (fire-and-forget handle_message)
# ----------------------------------------------------------------------

class _FakeAdapter:
    """Mimics BasePlatformAdapter's fire-and-forget handle_message: spawns
    the turn as a background task in _session_tasks and returns at once."""

    def __init__(self, turn_coro_factory):
        self._turn_coro_factory = turn_coro_factory
        self._session_tasks = {}
        self.config = types.SimpleNamespace(extra={})
        self.turns_completed = 0

    def build_source(self, **kw):
        from gateway.platforms.base import SessionSource
        from gateway.session import Platform
        return SessionSource(platform=Platform.DISCORD, chat_id=kw["chat_id"],
                             chat_type=kw.get("chat_type", "channel"),
                             chat_name=kw.get("chat_name"),
                             user_id=kw.get("user_id"),
                             user_name=kw.get("user_name"))

    async def handle_message(self, event):
        import asyncio
        from gateway.platforms.base import build_session_key
        key = build_session_key(event.source, group_sessions_per_user=True,
                                thread_sessions_per_user=False)
        self._session_tasks[key] = asyncio.create_task(
            self._turn_coro_factory(self))
        # returns immediately — turn still running


def test_inject_turn_awaits_real_turn_completion(monkeypatch):
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    async def turn(adapter):
        await asyncio.sleep(0.05)
        adapter.turns_completed += 1

    adapter = _FakeAdapter(turn)
    manager = cm.CharterManager(adapter=adapter)

    async def scenario():
        await manager._inject_turn("hello")
        # ack means the turn actually finished before _inject_turn returned
        assert adapter.turns_completed == 1

    asyncio.get_event_loop().run_until_complete(scenario())


def test_inject_turn_raises_when_turn_fails(monkeypatch):
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    async def failing_turn(adapter):
        raise RuntimeError("session conflict")

    adapter = _FakeAdapter(failing_turn)
    manager = cm.CharterManager(adapter=adapter)

    async def scenario():
        with pytest.raises(RuntimeError):
            await manager._inject_turn("hello")

    asyncio.get_event_loop().run_until_complete(scenario())


def test_inject_turn_raises_when_not_scheduled(monkeypatch):
    """Dropped/queued injection (no session task) must raise, not ack."""
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    adapter = _FakeAdapter(lambda a: None)

    async def swallow(event):
        return None  # queued/dropped — never registers a session task

    adapter.handle_message = swallow
    manager = cm.CharterManager(adapter=adapter)

    async def scenario():
        with pytest.raises(RuntimeError):
            await manager._inject_turn("hello")

    asyncio.get_event_loop().run_until_complete(scenario())


def test_inject_turn_refuses_busy_charter_session(monkeypatch):
    """A still-running prior turn must defer injection, not ack the old task."""
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    async def slow_turn(adapter):
        await asyncio.sleep(5)

    adapter = _FakeAdapter(slow_turn)
    manager = cm.CharterManager(adapter=adapter)
    handled = []

    async def scenario():
        # occupy the charter session with a running task
        from gateway.platforms.base import MessageEvent, MessageType
        src = adapter.build_source(chat_id="42", chat_name="home",
                                   chat_type="channel", user_id="charter",
                                   user_name="charter")
        ev = MessageEvent(text="first", message_type=MessageType.TEXT,
                          source=src, internal=True)
        await adapter.handle_message(ev)
        orig_handle = adapter.handle_message

        async def counting_handle(event):
            handled.append(event)
            await orig_handle(event)

        adapter.handle_message = counting_handle
        with pytest.raises(RuntimeError, match="busy"):
            await manager._inject_turn("second")
        # refused BEFORE calling handle_message — never queued/merged
        assert handled == []
        for t in adapter._session_tasks.values():
            t.cancel()

    asyncio.get_event_loop().run_until_complete(scenario())


def test_inject_turn_requires_new_task_not_prev(monkeypatch):
    """If handle_message leaves the previous (done) task in place — event
    queued/merged/dropped — the ack must fail, not reuse the old task."""
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    async def turn(adapter):
        adapter.turns_completed += 1

    adapter = _FakeAdapter(turn)
    manager = cm.CharterManager(adapter=adapter)

    async def scenario():
        # first injection succeeds and leaves a completed task under the key
        await manager._inject_turn("first")
        assert adapter.turns_completed == 1

        async def swallow(event):
            return None  # queued/dropped — no new task registered

        adapter.handle_message = swallow
        with pytest.raises(RuntimeError, match="not scheduled"):
            await manager._inject_turn("second")
        assert adapter.turns_completed == 1

    asyncio.get_event_loop().run_until_complete(scenario())


def test_charter_session_identity_contract(monkeypatch):
    """Integration (real key functions): the goal-manager lookup and every
    injected turn resolve to the SAME session key, and that key differs from
    the owner's session in the same channel — so the armed goal, kickoffs,
    and answers all land in one dedicated charter session that never
    collides with Garrett's own conversation."""
    from plugins.platforms.discord import charter as cm
    from gateway.session import Platform, SessionSource, build_session_key

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    captured = []

    class _SourceAdapter:
        config = types.SimpleNamespace(extra={})

        def build_source(self, **kw):
            src = SessionSource(platform=Platform.DISCORD,
                                chat_id=kw["chat_id"],
                                chat_type=kw.get("chat_type", "channel"),
                                chat_name=kw.get("chat_name"),
                                user_id=kw.get("user_id"),
                                user_name=kw.get("user_name"))
            captured.append(src)
            return src

    manager = cm.CharterManager(adapter=_SourceAdapter())
    s1 = manager._charter_source()   # used by _goal_manager
    s2 = manager._charter_source()   # used by _inject_turn

    # gateway SessionStore._generate_session_key and adapter handle_message
    # both delegate to build_session_key(source, ...) — same pure function
    k1 = build_session_key(s1, group_sessions_per_user=True,
                           thread_sessions_per_user=False)
    k2 = build_session_key(s2, group_sessions_per_user=True,
                           thread_sessions_per_user=False)
    assert k1 == k2

    owner = SessionSource(platform=Platform.DISCORD, chat_id="42",
                          chat_type="channel", user_id="garrett-user-id",
                          user_name="Garrett")
    k_owner = build_session_key(owner, group_sessions_per_user=True,
                                thread_sessions_per_user=False)
    assert k_owner != k1  # owner conversation is a separate session


def test_charter_pause_resume_lifecycle(r, monkeypatch):
    """arm → /charter pause → /charter resume must resume the goal."""
    from plugins.platforms.discord import charter as cm
    import asyncio

    monkeypatch.setenv("AGENT_NAME", "jade")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "42")

    charter = store.save_charter(r, "jade", {"objectives": ["obj"]})
    manager = cm.CharterManager(adapter=MagicMock())
    injected = []

    async def ok_inject(text):
        injected.append(text)

    manager._inject_turn = ok_inject
    loop = asyncio.get_event_loop()

    # arm
    mgr = MagicMock()
    mgr.state = None
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.set.assert_called_once()
    rendered = mgr.set.call_args[0][0]

    # owner paused via /charter pause → tick paused the goal with our reason
    state = MagicMock()
    state.status = "paused"
    state.goal = rendered
    state.paused_reason = "charter paused by owner"
    mgr.reset_mock()
    mgr.state = state

    # cooldown active right after arm: resume waits for the window
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.resume.assert_not_called()

    # after cooldown, /charter resume (status back to active) resumes it
    r.set("goalcharter:v1:lastkick:jade", "0")
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.resume.assert_called_once()
    mgr.set.assert_not_called()  # resumed, not re-armed
    assert any("resumed" in t for t in injected[1:])

    # a goal paused by someone else is left alone
    state.paused_reason = "manual pause by operator"
    mgr.reset_mock()
    r.set("goalcharter:v1:lastkick:jade", "0")
    loop.run_until_complete(manager._ensure_goal_armed(store, r, mgr, charter))
    mgr.resume.assert_not_called()
    mgr.set.assert_not_called()
