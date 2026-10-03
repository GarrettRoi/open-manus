"""Owner read views use only an injected fake, never the fleet datastore."""
import json
from datetime import datetime, timezone

import fakeredis
import pytest

from crm.service import CRMService
from crm.store import PREFIX
from crm import read_views


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("CRM_TIMEZONE", "America/Chicago")
    # UTC October 3, but still October 2 in the household's example zone.
    monkeypatch.setattr(read_views.time, "time",
                        lambda: datetime(2026, 10, 3, 1, tzinfo=timezone.utc).timestamp())
    return CRMService(fakeredis.FakeRedis(decode_responses=True))


def call(svc, action, args=None, role="owner"):
    result = svc.execute(action, args or {}, "test:" + role, role)
    assert result["ok"], result
    return result["result"]


def lead(svc, name, **fields):
    return call(svc, "create", {"lead": {"name": name, **fields}, "idempotency_key": name})


def test_business_scoped_counts_and_date_boundaries(svc, monkeypatch):
    lead(svc, "Late", business="real_estate", next_action_date="2026-10-01")
    lead(svc, "Today", business="real_estate", next_action_date="2026-10-02")
    lead(svc, "Future", business="real_estate", next_action_date="2026-10-03")
    lead(svc, "Undated", business="real_estate")
    lead(svc, "Closed", business="real_estate", next_action_date="2026-09-01", status="won")
    lead(svc, "Lost", business="real_estate", next_action_date="2026-09-01", status="lost")
    old = lead(svc, "Archived", business="real_estate", next_action_date="2026-09-01")
    call(svc, "archive", {"id": old["id"], "revision": 1})
    lead(svc, "DJ", business="dj_wedding", next_action_date="2026-10-01")
    result = call(svc, "summary", {"business": "real_estate"})
    assert result["total"] == 6
    assert result["by_business"]["dj_wedding"] == 0
    assert result["by_status"]["new"] == 4
    assert result["urgency"] == {"ready_now": 2, "overdue": 1}
    assert result["today"] == "2026-10-02"
    assert result["timezone"] == "America/Chicago"
    rows = call(svc, "list", {"business": "real_estate", "urgency": "ready_now"})["items"]
    assert [r["name"] for r in rows] == ["Late", "Today"]
    assert call(svc, "summary", {"archived": True})["urgency"]["overdue"] == 0
    monkeypatch.setenv("CRM_TIMEZONE", "UTC")
    assert call(svc, "summary", {"business": "real_estate"})["urgency"] == {"ready_now": 3, "overdue": 2}


def test_filters_search_and_pagination(svc):
    for i in range(3):
        lead(svc, f"Person {i}", email=f"user{i}@example.org", phone=f"555010{i}",
             business="other", next_action_date=f"2026-10-0{i+1}")
    for query in ["Person 1", "USER1@", "5550101"]:
        assert call(svc, "list", {"query": query})["total"] == 1
    result = call(svc, "list", {"sort": "next_action", "page": 2, "limit": 1})
    assert result["items"][0]["name"] == "Person 1"
    assert result["total"] == 3
    assert "history" not in result["items"][0]
    assert call(svc, "list", {"page": 8})["items"] == []


def test_activity_uses_existing_history_including_agent_notes(svc):
    item = lead(svc, "Buyer", business="real_estate")
    call(svc, "note", {"id": item["id"], "revision": 1, "text": "Private note"}, role="agent")
    call(svc, "status", {"id": item["id"], "revision": 2, "status": "contacted"})
    lead(svc, "Wedding", business="dj_wedding")
    first = call(svc, "activity", {"business": "real_estate", "limit": 2})
    second = call(svc, "activity", {"business": "real_estate", "limit": 2, "page": 2})
    assert first["total"] == 3
    assert [e["action"] for e in first["items"] + second["items"]] == ["status", "note", "create"]
    assert first["items"][1]["actor"] == "test:agent"
    assert first["items"][1]["lead_id"] == item["id"]
    assert "Private note" not in json.dumps(first)
    assert call(svc, "activity", {"business": "other"})["total"] == 0
    assert call(svc, "activity", {"business": "real_estate"}, role="agent")["total"] == 3
    assert svc.execute("activity", {}, "source:test", "source")["error"]["code"] == "forbidden"
    call(svc, "archive", {"id": item["id"], "revision": 3})
    assert call(svc, "activity", {"business": "real_estate"})["total"] == 0
    assert call(svc, "activity", {"archived": True})["total"] == 4


@pytest.mark.parametrize("action,args", [
    ("list", {"urgency": "guess"}), ("list", {"sort": "unknown"}),
    ("activity", {"limit": 101}), ("activity", {"page": 1001}),
    ("summary", {"page": 1}), ("activity", {"actor": "owner"}),
])
def test_strict_read_schemas(svc, action, args):
    assert svc.execute(action, args, "test", "owner")["error"]["code"] == "validation"


def test_budgets_and_invalid_timezone_fail_explicitly(svc, monkeypatch):
    lead(svc, "One")
    monkeypatch.setattr(read_views, "MAX_LEADS", 0)
    assert "5000" in svc.execute("list")["error"]["message"]
    monkeypatch.setattr(read_views, "MAX_LEADS", 5000)
    monkeypatch.setattr(read_views, "MAX_BYTES", 1)
    assert "read budget" in svc.execute("summary")["error"]["message"]
    monkeypatch.setattr(read_views, "MAX_BYTES", 100000)
    monkeypatch.setattr(read_views, "MAX_EVENTS", 0)
    assert "read budget" in svc.execute("activity")["error"]["message"]
    monkeypatch.setenv("CRM_TIMEZONE", "invalid/timezone")
    assert "CRM_TIMEZONE" in svc.execute("summary")["error"]["message"]


def test_hscan_duplicates_do_not_inflate_totals(svc, monkeypatch):
    item = lead(svc, "One")
    raw = svc.redis.hget(PREFIX + "leads", item["id"])
    monkeypatch.setattr(svc.redis, "hscan_iter", lambda *a, **kw: iter([(item["id"], raw), (item["id"], raw)]))
    assert call(svc, "summary")["total"] == 1