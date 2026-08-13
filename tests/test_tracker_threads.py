"""Tests for tools/tracker_threads.py — agent-managed pinned tracking threads.

All Redis I/O uses fakeredis; all Discord REST calls hit a fake in-memory
Discord (messages, threads, pins) so lifecycle + reconciliation are testable.
"""

import json
import os
from unittest import mock

import fakeredis
import pytest

from tools import tracker_threads as tt
from tools.discord_tool import DiscordAPIError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeDiscord:
    """Minimal in-memory Discord REST for the endpoints the tool uses."""

    def __init__(self):
        self.seq = 1000
        self.messages = {}   # id -> {channel_id, content}
        self.threads = {}    # id -> {archived, name}
        self.pins = set()    # (channel_id, message_id)

    def _next(self):
        self.seq += 1
        return str(self.seq)

    def request(self, method, path, token, params=None, body=None, timeout=15):
        parts = path.strip("/").split("/")
        # POST /channels/{cid}/messages
        if method == "POST" and parts[0] == "channels" and parts[-1] == "messages":
            mid = self._next()
            self.messages[mid] = {"channel_id": parts[1], "content": body["content"]}
            return {"id": mid}
        # POST /channels/{cid}/messages/{mid}/threads
        if method == "POST" and parts[-1] == "threads" and "messages" in parts:
            if parts[3] not in self.messages:
                raise DiscordAPIError(404, "unknown message")
            tid = self._next()
            self.threads[tid] = {"archived": False, "name": body["name"]}
            return {"id": tid, "name": body["name"]}
        # GET /channels/{id}
        if method == "GET" and parts[0] == "channels" and len(parts) == 2:
            th = self.threads.get(parts[1])
            if th is None:
                raise DiscordAPIError(404, "unknown channel")
            return {"id": parts[1], "thread_metadata": {"archived": th["archived"]}}
        # PATCH /channels/{id}  (archive/unarchive)
        if method == "PATCH" and parts[0] == "channels" and len(parts) == 2:
            self.threads[parts[1]]["archived"] = bool(body.get("archived"))
            return {"id": parts[1]}
        # PATCH /channels/{cid}/messages/{mid}
        if method == "PATCH" and "messages" in parts:
            if parts[3] not in self.messages:
                raise DiscordAPIError(404, "unknown message")
            self.messages[parts[3]]["content"] = body["content"]
            return {"id": parts[3]}
        # DELETE /channels/{cid}/messages/{mid}
        if method == "DELETE" and "messages" in parts:
            if parts[3] not in self.messages:
                raise DiscordAPIError(404, "unknown message")
            del self.messages[parts[3]]
            return None
        # PUT/DELETE pins
        if parts[2] == "pins":
            key = (parts[1], parts[3])
            if method == "PUT":
                self.pins.add(key)
            else:
                self.pins.discard(key)
            return None
        raise AssertionError(f"unhandled {method} {path}")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "bianca")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "HOME1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setenv("REDIS_URL", "redis://fake")


@pytest.fixture
def r(monkeypatch):
    fr = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(tt, "_redis", lambda: fr)
    return fr


@pytest.fixture
def dc(monkeypatch):
    fake = FakeDiscord()
    monkeypatch.setattr("tools.discord_tool._discord_request", fake.request)
    return fake


def call(**kw):
    return json.loads(tt.tracking_thread_tool(**kw))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_create_pins_anchor_and_registers_thread(env, r, dc):
    out = call(action="create_tracker", name="day trades", description="rolling log")
    assert out["ok"] and out["created"] and out["pinned"]
    tid = out["thread_id"]
    assert tid in dc.threads
    assert r.sismember(tt._THREADS_SET, tid)
    # anchor pinned in home channel
    assert any(c == "HOME1" for c, _ in dc.pins)


def test_create_is_idempotent_reuses_thread(env, r, dc):
    a = call(action="create_tracker", name="day trades")
    b = call(action="create_tracker", name="day trades")
    assert b["reused"] and b["thread_id"] == a["thread_id"]
    assert len(dc.threads) == 1


def test_create_recreates_when_thread_deleted(env, r, dc):
    a = call(action="create_tracker", name="day trades")
    del dc.threads[a["thread_id"]]
    b = call(action="create_tracker", name="day trades")
    assert b["created"] and b["thread_id"] != a["thread_id"]


def test_add_edit_delete_entry_roundtrip(env, r, dc):
    call(action="create_tracker", name="positions")
    add = call(action="add_entry", name="positions", entry_key="QQQ",
               content="long 10 @ 450")
    mid = add["message_id"]
    assert "long 10 @ 450" in dc.messages[mid]["content"]
    assert "[QQQ]" in dc.messages[mid]["content"]

    edit = call(action="edit_entry", name="positions", entry_key="QQQ",
                content="long 10 @ 450 · +1.2%")
    assert edit["edited"]
    assert "+1.2%" in dc.messages[mid]["content"]

    keys = call(action="list_entries", name="positions")
    assert keys["entry_keys"] == ["QQQ"]

    dele = call(action="delete_entry", name="positions", entry_key="QQQ")
    assert dele["deleted"] and mid not in dc.messages
    assert call(action="list_entries", name="positions")["entry_keys"] == []


def test_add_on_existing_key_edits_in_place(env, r, dc):
    call(action="create_tracker", name="positions")
    a = call(action="add_entry", name="positions", entry_key="SPY", content="v1")
    b = call(action="add_entry", name="positions", entry_key="SPY", content="v2")
    assert b["edited"]
    assert dc.messages[a["message_id"]]["content"].endswith("v2")
    # no duplicate message posted
    thread_msgs = [m for m in dc.messages.values()
                   if m["channel_id"] not in ("HOME1",)]
    assert len(thread_msgs) == 1


def test_edit_missing_key_errors(env, r, dc):
    call(action="create_tracker", name="positions")
    out = call(action="edit_entry", name="positions", entry_key="NOPE", content="x")
    assert "error" in out


def test_edit_reconciles_deleted_message(env, r, dc):
    call(action="create_tracker", name="positions")
    a = call(action="add_entry", name="positions", entry_key="QQQ", content="v1")
    del dc.messages[a["message_id"]]
    out = call(action="edit_entry", name="positions", entry_key="QQQ", content="v2")
    assert "error" in out
    # stale record dropped → re-add works
    again = call(action="add_entry", name="positions", entry_key="QQQ", content="v2")
    assert again["added"]


def test_entry_ops_unarchive_thread(env, r, dc):
    a = call(action="create_tracker", name="log")
    dc.threads[a["thread_id"]]["archived"] = True
    out = call(action="add_entry", name="log", entry_key="k1", content="hello")
    assert out["added"]
    assert dc.threads[a["thread_id"]]["archived"] is False


def test_archive_tracker_cleans_up(env, r, dc):
    a = call(action="create_tracker", name="log")
    out = call(action="archive_tracker", name="log")
    assert out["archived"]
    assert dc.threads[a["thread_id"]]["archived"] is True
    assert not r.sismember(tt._THREADS_SET, a["thread_id"])
    assert call(action="list_trackers")["count"] == 0


def test_list_trackers(env, r, dc):
    call(action="create_tracker", name="a")
    call(action="create_tracker", name="b")
    call(action="add_entry", name="b", entry_key="k", content="x")
    out = call(action="list_trackers")
    assert out["count"] == 2
    by = {t["tracker"]: t for t in out["trackers"]}
    assert by["b"]["entries"] == 1


def test_agent_scoping_isolated(env, r, dc, monkeypatch):
    call(action="create_tracker", name="mine")
    monkeypatch.setenv("AGENT_NAME", "lexi")
    assert call(action="list_trackers")["count"] == 0
    out = call(action="add_entry", name="mine", entry_key="k", content="x")
    assert "error" in out


def test_missing_home_channel_errors(env, r, dc, monkeypatch):
    monkeypatch.delenv("DISCORD_HOME_CHANNEL")
    out = call(action="create_tracker", name="x")
    assert "error" in out


def test_discord_403_degrades_gracefully(env, r, dc, monkeypatch):
    def boom(*a, **k):
        raise DiscordAPIError(403, "Missing Permissions")
    monkeypatch.setattr("tools.discord_tool._discord_request", boom)
    out = call(action="create_tracker", name="x")
    assert "error" in out and "permission" in out["error"].lower()


def test_403_on_thread_check_preserves_state(env, r, dc, monkeypatch):
    """A permission failure must NOT be treated as thread-gone: tracker
    record, entry ids, and territory-gate membership all stay intact."""
    a = call(action="create_tracker", name="log")
    call(action="add_entry", name="log", entry_key="k1", content="v1")
    real = dc.request

    def forbidden(method, path, token, params=None, body=None, timeout=15):
        if method == "GET" and path == f"/channels/{a['thread_id']}":
            raise DiscordAPIError(403, "Missing Access")
        return real(method, path, token, params=params, body=body, timeout=timeout)

    monkeypatch.setattr("tools.discord_tool._discord_request", forbidden)
    for action, kw in (("create_tracker", {}),
                       ("add_entry", {"entry_key": "k2", "content": "x"}),
                       ("edit_entry", {"entry_key": "k1", "content": "y"})):
        out = call(action=action, name="log", **kw)
        assert "error" in out and "permission" in out["error"].lower()
    # nothing was dropped
    assert tt._load_tracker(r, "bianca", "log") is not None
    assert r.hget(tt._entries_key("bianca", "log"), "k1")
    assert r.sismember(tt._THREADS_SET, a["thread_id"])
    # and once the permission issue clears, everything still works
    monkeypatch.setattr("tools.discord_tool._discord_request", real)
    assert call(action="create_tracker", name="log")["reused"]
    assert call(action="edit_entry", name="log", entry_key="k1",
                content="v2")["edited"]


def test_unknown_action_errors(env, r, dc):
    out = call(action="bogus", name="x")
    assert "error" in out


def test_territory_gate(env, r, dc):
    a = call(action="create_tracker", name="log")
    assert tt.in_tracker_territory({a["thread_id"]}) is True
    assert tt.in_tracker_territory({"999"}) is False
    assert tt.in_tracker_territory(set()) is False


def test_territory_gate_immediate_after_create(env, r, dc):
    """A tracker created AFTER the cache was populated is gated at once —
    no TTL window where the new thread is unguarded."""
    assert tt.in_tracker_territory({"111"}) is False  # populate cache (empty)
    a = call(action="create_tracker", name="log")
    assert tt.in_tracker_territory({a["thread_id"]}) is True
    call(action="archive_tracker", name="log")
    assert tt.in_tracker_territory({a["thread_id"]}) is False


def test_territory_gate_cross_process_consistency(env, r, dc):
    """A second adapter process (separate cache instance) sees a tracker
    created elsewhere immediately, via the Redis version counter."""
    assert tt.in_tracker_territory({"111"}) is False  # warm this process
    a = call(action="create_tracker", name="log")
    # Simulate another process: fresh module-level cache, same Redis.
    other = {"ver": None, "ids": frozenset()}
    with mock.patch.object(tt, "_territory_cache", other):
        assert tt.in_tracker_territory({a["thread_id"]}) is True
        # ...and it also observes the removal without any delay.
        call(action="archive_tracker", name="log")
        assert tt.in_tracker_territory({a["thread_id"]}) is False


def test_territory_gate_fails_open(env, monkeypatch):
    tt._territory_cache.update(ver=None, ids=frozenset())
    monkeypatch.setattr(tt, "_redis", mock.Mock(side_effect=RuntimeError("down")))
    assert tt.in_tracker_territory({"123"}) is False


def test_registered_in_registry():
    from tools.registry import registry
    entry = registry.get_entry("tracking_thread")
    assert entry is not None and entry.toolset == "vault"
