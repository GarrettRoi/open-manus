import json
import os
import sys
import time

import fakeredis
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cron_registry import CronRegistryStore


def _store(lease=180):
    return CronRegistryStore(
        fakeredis.FakeRedis(decode_responses=True),
        heartbeat_lease=lease,
    )


def _seed(store, agent="harmony", job_id="job-1", enabled=True, revision=0,
          heartbeat=None):
    heartbeat = time.time() if heartbeat is None else heartbeat
    store.r.sadd(store._key("agents"), agent)
    store.r.hset(
        store._key("agent:" + agent),
        mapping={"heartbeat_epoch": str(heartbeat), "job_count": "1"},
    )
    snapshot = {
        "id": job_id,
        "name": "Daily briefing",
        "enabled": enabled,
        "state": "scheduled" if enabled else "paused",
        "control_revision": revision,
        "schedule_display": "daily at 8",
        "description": "- Gather updates\n- Send briefing",
    }
    store.r.hset(store._key("jobs:" + agent), job_id, json.dumps(snapshot))
    return snapshot


def test_list_distinguishes_stale_from_disabled():
    store = _store(lease=10)
    _seed(store, enabled=True, heartbeat=time.time() - 60)
    result = store.list()
    assert result["agents"][0]["stale"] is True
    assert result["jobs"][0]["enabled"] is True
    assert result["jobs"][0]["stale"] is True
    assert result["stats"]["stale_agents"] == 1


def test_toggle_pending_then_applied():
    store = _store()
    snapshot = _seed(store)
    result = store.toggle("harmony", "job-1", False, expected_revision=0)
    assert result["status"] == "ok"
    listed = store.list()["jobs"][0]
    assert listed["pending"] is True
    assert listed["desired_enabled"] is False
    assert store.list()["stats"]["enabled"] == 1
    assert store.list()["stats"]["paused"] == 0

    snapshot["enabled"] = False
    snapshot["control_revision"] = result["revision"]
    store.r.hset(store._key("jobs:harmony"), "job-1", json.dumps(snapshot))
    listed = store.list()["jobs"][0]
    assert listed["pending"] is False


def test_toggle_conflict_and_idempotency():
    store = _store()
    _seed(store, revision=4)
    conflict = store.toggle("harmony", "job-1", False, expected_revision=3)
    assert conflict["status"] == "conflict"
    first = store.toggle(
        "harmony", "job-1", False, expected_revision=4,
        idempotency_key="same-request",
    )
    second = store.toggle(
        "harmony", "job-1", False,
        idempotency_key="same-request",
    )
    assert first["operation_id"] == second["operation_id"]
    assert second["status"] == "idempotent"


def test_bulk_filter_and_fleet_freeze():
    store = _store()
    _seed(store, "harmony", "a")
    _seed(store, "samantha", "b")
    outcome = store.bulk(
        False,
        filters={"agent": "harmony"},
        idempotency_key="filtered",
        freeze=True,
    )
    assert outcome["count"] == 1
    assert store.r.hget(store._key("freeze"), "enabled") is None

    outcome = store.bulk(
        False,
        filters={"state": "all"},
        idempotency_key="all",
        freeze=True,
    )
    assert outcome["count"] == 2
    assert store.r.hget(store._key("freeze"), "enabled") == "true"


def test_bulk_selected_reports_missing_target():
    store = _store()
    _seed(store, "harmony", "exists")
    outcome = store.bulk(
        False,
        keys=["harmony:exists", "harmony:deleted"],
        idempotency_key="selected",
    )
    assert outcome["count"] == 2
    assert {row["status"] for row in outcome["outcomes"]} == {"ok", "not_found"}


def test_stale_desired_does_not_override_newer_applied_revision():
    store = _store()
    _seed(store, revision=3, enabled=True)
    store.r.hset(
        store._key("desired:harmony"),
        "job-1",
        json.dumps({"enabled": False, "revision": 2, "operation_id": "old"}),
    )
    row = store.list()["jobs"][0]
    assert row["pending"] is False
    assert row["desired_enabled"] is True
    assert row["control_revision"] == 3


def test_invalid_identifier_rejected():
    store = _store()
    with pytest.raises(ValueError):
        store.toggle("../other:key", "x", False)


def test_audit_is_bounded():
    store = CronRegistryStore(
        fakeredis.FakeRedis(decode_responses=True), audit_limit=3
    )
    _seed(store)
    for i in range(5):
        store.set_freeze(bool(i % 2), idempotency_key=str(i))
    assert store.r.llen(store._key("audit")) == 3
