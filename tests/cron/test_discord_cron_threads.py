"""Tests for the LOCAL pinned cron-jobs / cron-history Discord threads feature.

Uses a lightweight fake of the discord.py surface the module touches, so the
reconcile/edit/delete/restart flows are exercised without a live gateway.
"""

import asyncio
import json
import sys
import types

import pytest


@pytest.fixture()
def dct(tmp_path, monkeypatch):
    """Import cron.discord_cron_threads with isolated state + stub discord."""
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class HTTPException(Exception):
            pass

        class Forbidden(HTTPException):
            pass

        class NotFound(HTTPException):
            pass

        discord.HTTPException = HTTPException
        discord.Forbidden = Forbidden
        discord.NotFound = NotFound
        monkeypatch.setitem(sys.modules, "discord", discord)

    import cron.discord_cron_threads as mod

    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "555")
    monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "_disabled", False)
    monkeypatch.setattr(mod, "_message_map", None)
    monkeypatch.setattr(mod, "_jobs_thread_id", None)
    monkeypatch.setattr(mod, "_history_thread_id", None)
    monkeypatch.setattr(mod, "_bootstrap_lock", None)
    mod._last_posted.clear()
    return mod


class _Msg:
    _next = [100]

    def __init__(self, author_id, content, thread):
        _Msg._next[0] += 1
        self.id = _Msg._next[0]
        self.author = types.SimpleNamespace(id=author_id)
        self.content = content
        self.thread = thread

    async def pin(self):
        pass

    async def delete(self):
        self.thread.msgs = [m for m in self.thread.msgs if m.id != self.id]

    async def edit(self, content=None):
        self.content = content


class _Thread:
    _next = [1000]

    def __init__(self, name, parent):
        _Thread._next[0] += 1
        self.id = _Thread._next[0]
        self.name = name
        self.parent_id = parent.id
        self.archived = False
        self.auto_archive_duration = 10080
        self.msgs = []

    async def send(self, content):
        m = _Msg(1, content, self)
        self.msgs.append(m)
        return m

    def get_partial_message(self, mid):
        import discord

        for m in self.msgs:
            if m.id == mid:
                return m

        class _Ghost:
            async def delete(self):
                raise discord.NotFound()

            async def edit(self, content=None):
                raise discord.NotFound()

        return _Ghost()

    def history(self, limit=200):
        msgs = list(reversed(self.msgs))

        async def gen():
            for m in msgs:
                yield m

        return gen()

    async def edit(self, **kw):
        self.archived = kw.get("archived", self.archived)
        self.auto_archive_duration = kw.get(
            "auto_archive_duration", self.auto_archive_duration
        )


class _Channel:
    def __init__(self):
        self.id = 555
        self.threads = []
        self.sent = []

    async def send(self, content):
        m = _Msg(1, content, None)
        self.sent.append(m)
        return m

    async def create_thread(self, name, message=None, auto_archive_duration=None):
        t = _Thread(name, self)
        self.threads.append(t)
        return t

    def archived_threads(self, limit=50):
        async def gen():
            return
            yield

        return gen()


class _Client:
    def __init__(self, channel):
        self.user = types.SimpleNamespace(id=1)
        self._channel = channel

    def get_channel(self, cid):
        if cid == self._channel.id:
            return self._channel
        for t in self._channel.threads:
            if t.id == cid:
                return t
        return None


@pytest.fixture()
def env(dct, monkeypatch):
    channel = _Channel()
    adapter = types.SimpleNamespace(_client=_Client(channel))
    jobs = [
        {"id": "j1", "name": "Job One", "schedule_display": "every 30m",
         "next_run_at": "2026-08-13T10:00", "enabled": True, "state": "scheduled"},
        {"id": "j2", "name": "Job Two", "schedule_display": "0 9 * * *",
         "next_run_at": "2026-08-14T09:00", "enabled": True, "state": "scheduled"},
    ]
    monkeypatch.setattr(
        dct, "list_jobs", lambda include_disabled=True: [dict(j) for j in jobs]
    )
    return dct, channel, adapter, jobs


def test_bootstrap_creates_pinned_threads_once(env):
    dct, channel, adapter, jobs = env

    async def run():
        await dct._sync_async(adapter)
        await dct._sync_async(adapter)

    asyncio.run(run())
    names = sorted(t.name for t in channel.threads)
    assert names == sorted([dct.JOBS_THREAD_NAME, dct.HISTORY_THREAD_NAME])
    # One pinned anchor message per thread, no duplicates on re-sync.
    assert len(channel.sent) == 2


def test_reconcile_post_edit_delete(env):
    dct, channel, adapter, jobs = env

    async def run():
        await dct._sync_async(adapter)
        jt = next(t for t in channel.threads if t.name == dct.JOBS_THREAD_NAME)
        assert len(jt.msgs) == 2

        # Pause j1 → post edited in place.
        jobs[0]["enabled"] = False
        jobs[0]["state"] = "paused"
        await dct._sync_async(adapter)
        assert len(jt.msgs) == 2
        assert "⏸️ paused" in jt.msgs[0].content

        # Remove j2 → its post disappears.
        del jobs[1]
        await dct._sync_async(adapter)
        assert len(jt.msgs) == 1
        assert "j1" in jt.msgs[0].content

    asyncio.run(run())


def test_restart_reconciliation_and_reuse(env):
    dct, channel, adapter, jobs = env

    async def run():
        await dct._sync_async(adapter)
        jt = next(t for t in channel.threads if t.name == dct.JOBS_THREAD_NAME)

        # Simulate gateway restart: wipe in-memory state, mutate jobs "offline".
        dct._message_map = None
        dct._last_posted.clear()
        del jobs[1]  # removed while down
        jobs.append({"id": "j3", "name": "Job Three", "schedule_display": "every 1h",
                     "next_run_at": None, "enabled": True, "state": "scheduled"})
        await dct._sync_async(adapter)
        ids = sorted(
            dct._ID_MARKER_RE.search(m.content).group(1) for m in jt.msgs
        )
        assert ids == ["j1", "j3"]
        # Threads reused, never duplicated; archived thread revived.
        jt.archived = True
        await dct._sync_async(adapter)
        assert jt.archived is False
        assert len(channel.threads) == 2

    asyncio.run(run())


def test_history_append_only(env):
    dct, channel, adapter, jobs = env

    async def run():
        await dct._log_run_async(adapter, jobs[0], True, None, "2026-08-13 12:00:00")
        await dct._log_run_async(adapter, jobs[0], False, "boom", "2026-08-13 12:05:00")

    asyncio.run(run())
    ht = next(t for t in channel.threads if t.name == dct.HISTORY_THREAD_NAME)
    assert len(ht.msgs) == 2
    assert "✅" in ht.msgs[0].content
    assert "❌" in ht.msgs[1].content and "boom" in ht.msgs[1].content


def test_state_persisted(env):
    dct, channel, adapter, jobs = env
    asyncio.run(dct._sync_async(adapter))
    state = json.loads(dct.STATE_FILE.read_text())
    assert state["jobs_thread_id"] and state["history_thread_id"]
    assert set(state["messages"]) == {"j1", "j2"}


def test_hooks_never_raise_without_gateway(dct):
    # No adapter/loop cached → both public hooks must silently no-op.
    dct._cached_adapter = None
    dct._cached_loop = None
    dct.schedule_sync(None, None)
    dct.request_sync()
    dct.log_run({"id": "x", "name": "X"}, True)


def test_disabled_without_home_channel(dct, monkeypatch):
    monkeypatch.delenv("DISCORD_HOME_CHANNEL", raising=False)
    assert dct._enabled() is False
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "555")
    monkeypatch.setenv("HERMES_CRON_DISCORD_THREADS", "0")
    assert dct._enabled() is False


def test_forbidden_disables_feature(env):
    import discord

    dct, channel, adapter, jobs = env

    async def deny(*a, **kw):
        raise discord.Forbidden()

    channel.create_thread = deny
    asyncio.run(dct._sync_async(adapter))
    assert dct._disabled is True
    # Subsequent hooks no-op quietly.
    dct.schedule_sync(None, None)
