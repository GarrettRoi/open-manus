"""Tests for the per-agent task-board thread mirror (render + diff logic)."""

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Stub discord before importing the module (module only imports it lazily,
# but keep parity with the dispatch tests' environment).
sys.modules.setdefault("discord", MagicMock())

from plugins.platforms.discord import taskboard as tb


def _kan(id_, title, status):
    return {"kind": "kanban", "id": id_, "title": title, "status": status}


def _goal(title, status="active", id_="goal:s1"):
    return {"kind": "goal", "id": id_, "title": title, "status": status}


def _disp(id_, title, status, frm="lexi", thread_id="123"):
    return {"kind": "dispatch", "id": id_, "title": title, "status": status,
            "from": frm, "thread_id": thread_id}


def _snap(*items):
    return {tb.snapshot_key(i): i for i in items}


# ----------------------------------------------------------------------
# render_board
# ----------------------------------------------------------------------

def test_render_board_sections_and_grouping():
    snap = _snap(
        _goal("Ship the fleet dashboard"),
        _disp("7", "Summarize logs", "working"),
        _kan("KAN-1", "Write docs", "todo"),
        _kan("KAN-2", "Deploy fix", "running"),
        _kan("KAN-3", "Old chore", "done"),
        _kan("KAN-4", "Waiting on review", "review"),
    )
    board = tb.render_board("samantha", snap)
    assert "Samantha — task board" in board
    assert "🎯 Goal" in board and "Ship the fleet dashboard" in board
    assert "📨 Dispatch chains" in board and "chain #7" in board and "<#123>" in board
    assert "📋 Pending" in board and "KAN-1" in board
    assert "🔧 Active" in board and "KAN-2" in board
    assert "Blocked / review" in board and "KAN-4" in board
    assert "✅ Done (48h)" in board and "KAN-3" in board


def test_render_board_empty_sections_show_none():
    board = tb.render_board("addison", {})
    assert "-# none" in board
    assert len(board) < 3900


def test_render_board_caps_long_sections():
    items = [_kan(f"KAN-{i}", f"Task {i}", "todo") for i in range(20)]
    board = tb.render_board("vera", _snap(*items))
    assert "…and 8 more" in board


# ----------------------------------------------------------------------
# diff_updates
# ----------------------------------------------------------------------

def test_diff_new_task_and_status_move():
    old = _snap(_kan("KAN-1", "Write docs", "todo"))
    new = _snap(_kan("KAN-1", "Write docs", "running"),
                _kan("KAN-2", "New thing", "todo"))
    ups = tb.diff_updates(old, new)
    assert any("KAN-1" in u and "todo" in u and "running" in u for u in ups)
    assert any("🆕" in u and "KAN-2" in u for u in ups)


def test_diff_completion_and_goal_lifecycle():
    old = _snap(_kan("KAN-1", "Write docs", "running"), _goal("Old goal"))
    new = _snap(_kan("KAN-1", "Write docs", "done"), )
    ups = tb.diff_updates(old, new)
    assert any(u.startswith("✅") and "KAN-1" in u for u in ups)
    assert any("goal finished" in u for u in ups)


def test_diff_dispatch_chain_lifecycle():
    old = _snap(_disp("7", "Summarize logs", "pending"))
    mid = _snap(_disp("7", "Summarize logs", "working"))
    ups = tb.diff_updates(old, mid)
    assert any("pending" in u and "working" in u for u in ups)
    ups2 = tb.diff_updates(mid, {})
    assert any("chain #7 closed" in u for u in ups2)


def test_diff_done_item_aging_out_is_silent():
    old = _snap(_kan("KAN-9", "Old chore", "done"))
    assert tb.diff_updates(old, {}) == []


def test_diff_new_done_item_not_announced_as_new():
    # A done item entering the snapshot (e.g. completed while offline) should
    # not produce a spurious 🆕 line.
    ups = tb.diff_updates({}, _snap(_kan("KAN-5", "Did it", "done")))
    assert all("🆕" not in u for u in ups)


# ----------------------------------------------------------------------
# collectors (failure paths must return [] — the loop must never die)
# ----------------------------------------------------------------------

def test_collect_kanban_absent_module_returns_empty():
    with patch.dict(sys.modules, {"hermes_cli": None}):
        assert tb._collect_kanban("lexi") == []


def test_collect_goals_missing_db_returns_empty():
    fake = types.ModuleType("hermes_state")
    fake.DEFAULT_DB_PATH = "/nonexistent/state.db"
    with patch.dict(sys.modules, {"hermes_state": fake}), \
         patch.dict("os.environ", {"HERMES_STATE_DB": ""}):
        assert tb._collect_goals() == []


def test_collect_goals_reads_active_and_paused_only(tmp_path):
    import sqlite3
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    rows = [
        ("goal:s1", json.dumps({"goal": "Active goal", "status": "active"})),
        ("goal:s2", json.dumps({"goal": "Paused goal", "status": "paused"})),
        ("goal:s3", json.dumps({"goal": "Done goal", "status": "done"})),
        ("other:x", json.dumps({"goal": "not a goal"})),
    ]
    conn.executemany("INSERT INTO state_meta VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    with patch.dict("os.environ", {"HERMES_STATE_DB": str(db)}):
        goals = tb._collect_goals()
    titles = {g["title"] for g in goals}
    assert titles == {"Active goal", "Paused goal"}


def test_redis_keys_use_exclusive_namespace():
    # Must never collide with the legacy skills/task_board `taskboard:*` keys.
    for name in ("thread", "snapshot", "board_render", "board_msg"):
        key = tb._k(name, "lexi")
        assert key.startswith("discord:taskboard:v1:")
        assert not key.startswith("taskboard:")


def test_in_board_territory_gate():
    with patch.dict("os.environ", {"TASK_BOARD_CHANNEL_ID": "999"}):
        assert tb.in_board_territory({"999"}) is True          # board root
        assert tb.in_board_territory({"555", "999"}) is True   # thread under it
        assert tb.in_board_territory({"123"}) is False         # elsewhere
        assert tb.in_board_territory(None) is False            # DMs
    with patch.dict("os.environ", {"TASK_BOARD_CHANNEL_ID": ""}):
        assert tb.in_board_territory({"999"}) is False         # disabled


def test_manager_disabled_without_channel_or_redis():
    with patch.dict("os.environ", {"TASK_BOARD_CHANNEL_ID": "", "REDIS_URL": "x"}):
        assert tb.TaskBoardManager(MagicMock()).enabled is False
    with patch.dict("os.environ", {"TASK_BOARD_CHANNEL_ID": "1", "REDIS_URL": ""}):
        assert tb.TaskBoardManager(MagicMock()).enabled is False
    with patch.dict("os.environ", {"TASK_BOARD_CHANNEL_ID": "1", "REDIS_URL": "x"}):
        assert tb.TaskBoardManager(MagicMock()).enabled is True
