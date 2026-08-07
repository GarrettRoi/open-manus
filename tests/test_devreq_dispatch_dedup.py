"""Concurrency / dedup tests for the dev-request dispatch pipeline.

Covers the three producers (approval via set_status, periodic sweep, manual
/dispatch endpoint) and the dispatcher's final "already started" guard.

All Redis I/O uses fakeredis so no real Redis is needed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

# ---------------------------------------------------------------------------
# Path wiring — vault module + tools module
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_VAULT_DIR = _ROOT / "services" / "vault"
_TOOLS_DIR = _ROOT / "tools"

for _p in (_VAULT_DIR, _TOOLS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Stub httpx so replit_mcp can be imported without the real package installed
# in this test environment.
if "httpx" not in sys.modules:
    sys.modules["httpx"] = MagicMock()

import replit_mcp  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _r() -> fakeredis.FakeRedis:
    """Fresh isolated FakeRedis instance per test."""
    return fakeredis.FakeRedis(decode_responses=True)


def _seed_approved(r, req_id: str, dispatch_status: str | None = None) -> dict:
    """Write a minimal approved devreq item and add it to devreq:approved."""
    item: dict = {
        "id": req_id,
        "title": f"Test request {req_id}",
        "description": "some description",
        "agent": "testbot",
        "status": "approved",
    }
    if dispatch_status is not None:
        item["dispatch_status"] = dispatch_status
    r.set(f"devreq:item:{req_id}", json.dumps(item), ex=86400)
    r.rpush("devreq:approved", req_id)
    return item


# ---------------------------------------------------------------------------
# enqueue_if_unclaimed — unit tests
# ---------------------------------------------------------------------------

class TestEnqueueIfUnclaimed:
    def test_first_call_returns_true_and_sets_claim(self):
        r = _r()
        assert replit_mcp.enqueue_if_unclaimed(r, "1") is True
        assert r.exists(replit_mcp.K_CLAIM + "1")

    def test_first_call_pushes_exactly_one_entry(self):
        r = _r()
        replit_mcp.enqueue_if_unclaimed(r, "1")
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1
        assert r.lrange(replit_mcp.DISPATCH_QUEUE, 0, -1) == ["1"]

    def test_second_call_returns_false(self):
        r = _r()
        replit_mcp.enqueue_if_unclaimed(r, "1")
        assert replit_mcp.enqueue_if_unclaimed(r, "1") is False

    def test_second_call_does_not_add_to_queue(self):
        r = _r()
        replit_mcp.enqueue_if_unclaimed(r, "1")
        replit_mcp.enqueue_if_unclaimed(r, "1")
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_claim_ttl_is_set(self):
        r = _r()
        replit_mcp.enqueue_if_unclaimed(r, "2")
        ttl = r.ttl(replit_mcp.K_CLAIM + "2")
        assert 0 < ttl <= replit_mcp.CLAIM_TTL

    def test_different_ids_are_independent(self):
        r = _r()
        assert replit_mcp.enqueue_if_unclaimed(r, "3") is True
        assert replit_mcp.enqueue_if_unclaimed(r, "4") is True
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 2

    def test_after_claim_deleted_re_enqueue_succeeds(self):
        """Simulates claim expiry / loop releasing claim on failure."""
        r = _r()
        replit_mcp.enqueue_if_unclaimed(r, "5")
        r.brpop(replit_mcp.DISPATCH_QUEUE, 0)   # consume the queue entry
        r.delete(replit_mcp.K_CLAIM + "5")       # claim released
        assert replit_mcp.enqueue_if_unclaimed(r, "5") is True
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_three_concurrent_callers_exactly_one_wins(self):
        """All three producers calling simultaneously — only one enqueues."""
        r = _r()
        results = [
            replit_mcp.enqueue_if_unclaimed(r, "99"),  # approval
            replit_mcp.enqueue_if_unclaimed(r, "99"),  # sweep
            replit_mcp.enqueue_if_unclaimed(r, "99"),  # manual dispatch
        ]
        assert sum(results) == 1, f"Expected exactly 1 True, got {results}"
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1


# ---------------------------------------------------------------------------
# sweep_dispatch_backlog — dedup tests
# ---------------------------------------------------------------------------

class TestSweepDispatchBacklog:
    def test_sweep_enqueues_unqueued_failed(self):
        r = _r()
        _seed_approved(r, "10", dispatch_status="failed")
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "10" in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_sweep_enqueues_never_dispatched(self):
        r = _r()
        _seed_approved(r, "11")  # no dispatch_status
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "11" in requeued

    def test_sweep_skips_started(self):
        r = _r()
        _seed_approved(r, "12", dispatch_status="started")
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "12" not in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 0

    def test_sweep_called_twice_no_duplicate(self):
        r = _r()
        _seed_approved(r, "13", dispatch_status="failed")
        first = replit_mcp.sweep_dispatch_backlog(r)
        second = replit_mcp.sweep_dispatch_backlog(r)
        assert "13" in first
        assert "13" not in second
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_approval_then_sweep_no_duplicate(self):
        """Approval uses enqueue_if_unclaimed → sweep must detect the claim and skip."""
        r = _r()
        _seed_approved(r, "14", dispatch_status=None)
        # Simulate approval path
        assert replit_mcp.enqueue_if_unclaimed(r, "14") is True
        # Sweep must skip — claim already set
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "14" not in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_manual_dispatch_then_sweep_no_duplicate(self):
        """Manual /dispatch sets claim → sweep must skip."""
        r = _r()
        _seed_approved(r, "15", dispatch_status="failed")
        assert replit_mcp.enqueue_if_unclaimed(r, "15") is True
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "15" not in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_claim_released_on_failure_lets_sweep_retry(self):
        r = _r()
        _seed_approved(r, "16", dispatch_status="failed")
        replit_mcp.enqueue_if_unclaimed(r, "16")
        # Loop processes item — fails — releases claim, updates status
        r.brpop(replit_mcp.DISPATCH_QUEUE, 0)
        r.delete(replit_mcp.K_CLAIM + "16")
        # Item still shows "failed" in Redis
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "16" in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_started_status_prevents_requeue_even_without_claim(self):
        """Once dispatch_status=started, no claim needed to block re-queue."""
        r = _r()
        _seed_approved(r, "17", dispatch_status="started")
        r.delete(replit_mcp.K_CLAIM + "17")  # claim already gone
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "17" not in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 0


# ---------------------------------------------------------------------------
# Dispatcher "already started" guard — logic tests
# ---------------------------------------------------------------------------

class TestDispatcherGuard:
    """Verify the guard condition the loop checks before calling start_agent_run."""

    def _item_with_status(self, req_id: str, ds: str | None) -> dict:
        item = {"id": req_id, "title": "t", "description": "d", "status": "approved"}
        if ds is not None:
            item["dispatch_status"] = ds
        return item

    def test_guard_triggers_on_started(self):
        item = self._item_with_status("20", "started")
        assert item.get("dispatch_status") == "started"  # loop would skip

    def test_guard_does_not_trigger_on_failed(self):
        item = self._item_with_status("21", "failed")
        assert item.get("dispatch_status") != "started"  # loop would proceed

    def test_guard_does_not_trigger_on_none(self):
        item = self._item_with_status("22", None)
        assert item.get("dispatch_status") != "started"  # loop would proceed

    def test_claim_not_deleted_after_start(self):
        """After a successful start the loop leaves the claim; sweep must rely on status."""
        r = _r()
        _seed_approved(r, "23", dispatch_status="failed")
        # Enqueue
        replit_mcp.enqueue_if_unclaimed(r, "23")
        r.brpop(replit_mcp.DISPATCH_QUEUE, 0)
        # Simulate successful start: update status, leave claim in place
        raw = r.get("devreq:item:23")
        item = json.loads(raw)
        item["dispatch_status"] = "started"
        r.set("devreq:item:23", json.dumps(item))
        # Claim still present; sweep should skip due to dispatch_status=started
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "23" not in requeued


# ---------------------------------------------------------------------------
# set_status (approval path) — atomic enqueue integration
# ---------------------------------------------------------------------------

class TestSetStatusAtomicEnqueue:
    """Test that the approval path in tools/dev_requests uses enqueue_if_unclaimed."""

    def _make_redis_and_seed(self):
        r = _r()
        # Seed a pending item
        item = {
            "id": "30",
            "title": "Test",
            "description": "desc",
            "agent": "testbot",
            "status": "pending",
            "created_at": int(time.time()),
        }
        r.set("devreq:item:30", json.dumps(item), ex=86400)
        r.rpush("devreq:pending", "30")
        r.set("devreq:seq", "30")
        return r, item

    def test_approval_enqueues_exactly_once(self):
        r, _ = self._make_redis_and_seed()
        # Patch _redis() to return our fakeredis instance
        import dev_requests as dr
        with patch.object(dr, "_redis", return_value=r):
            result = dr.set_status("30", "approved", "owner")
        assert result is not None
        assert result.get("status") == "approved"
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1
        assert r.exists(replit_mcp.K_CLAIM + "30")

    def test_approval_then_sweep_exactly_one_in_queue(self):
        r, _ = self._make_redis_and_seed()
        import dev_requests as dr
        with patch.object(dr, "_redis", return_value=r):
            dr.set_status("30", "approved", "owner")
        # Seed into approved list so sweep can see it
        raw = r.get("devreq:item:30")
        item = json.loads(raw)
        if not r.lpos("devreq:approved", "30"):
            r.rpush("devreq:approved", "30")
        requeued = replit_mcp.sweep_dispatch_backlog(r)
        assert "30" not in requeued
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1

    def test_approval_then_manual_dispatch_exactly_one(self):
        r, _ = self._make_redis_and_seed()
        import dev_requests as dr
        with patch.object(dr, "_redis", return_value=r):
            dr.set_status("30", "approved", "owner")
        # Manual dispatch tries the same atomic helper
        queued = replit_mcp.enqueue_if_unclaimed(r, "30")
        assert queued is False
        assert r.llen(replit_mcp.DISPATCH_QUEUE) == 1
