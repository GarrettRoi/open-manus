"""Read-only ticket projection; all data is local test fixtures."""
from unittest.mock import patch

from plugins.platforms.discord import taskboard as tb


def test_ticket_delivery_and_blocked_result_are_visible():
    item = {
        "kind": "dispatch", "ticket": True, "id": "42", "title": "Read a file",
        "from": "raven", "status": "blocked", "delivery": "reconciliation",
        "delivery_reason": "Execution lease expired; inspect side effects",
        "result": "Required input: approved file path",
    }
    board = tb.render_board("cora", {"dispatch:42": item})
    assert "ticket #42" in board
    assert "**blocked**" in board
    assert "reconciliation" in board
    assert "approved file path" in board
    assert len(board) <= 3900


def test_delivery_only_change_produces_update_without_model():
    before = {
        "kind": "dispatch", "ticket": True, "id": "42", "title": "Read",
        "from": "raven", "status": "queued", "delivery": "queued",
    }
    after = dict(before, delivery="retrying", delivery_reason="Recipient unavailable")
    updates = tb.diff_updates({"dispatch:42": before}, {"dispatch:42": after})
    assert len(updates) == 1
    assert "Recipient unavailable" in updates[0]


def test_recent_result_remains_visible_after_leaving_active_set():
    from tools import agent_dispatch
    from tools.dispatch_tickets import TicketStore
    import fakeredis
    r = fakeredis.FakeRedis(decode_responses=True)
    ticket = {
        "id": "42", "to": "cora", "from": "raven", "objective": "Read",
        "status": "succeeded", "updated_at": 1000,
        "result": "File checked", "delivery": {"state": "delivered"},
    }
    with patch.object(agent_dispatch, "_redis", return_value=r), \
            patch.object(TicketStore, "list_for", return_value=[ticket]), \
            patch.object(tb.time, "time", return_value=1001):
        items = tb._collect_dispatch("cora")
    assert len(items) == 1
    assert items[0]["result"] == "File checked"