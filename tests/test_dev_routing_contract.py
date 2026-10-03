"""Isolated routing contract tests. Real Lua via fakeredis[lua], no fleet I/O."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import fakeredis
import pytest
from services.vault import dev_routing as routing, dev_projects as projects, replit_mcp as mcp
from tools import dev_requests as dr


@pytest.fixture
def r(monkeypatch):
    monkeypatch.setenv("DISCORD_OWNER_ID", "owner")
    fake = fakeredis.FakeRedis(decode_responses=True)
    assert fake.eval("return 1", 0) == 1
    fake.set(projects.K_PROJECTS, json.dumps({"open-manus": "fleet", "app": "app-id"}))
    with patch.object(dr, "_redis", return_value=fake):
        yield fake


def approved(r, project="app"):
    item = dr.submit_request("Title", "Details", project,
                             "fleet_platform" if project == "open-manus" else "project_app")
    return dr.set_status(item["id"], "approved", "owner", routing.review_token(r, item))


def test_two_projects_and_lua_lease(r):
    for name, target in [("app", "app-id"), ("open-manus", "fleet")]:
        item = approved(r, name)
        assert routing.dispatch_route(r, item) == (name, target)
        assert mcp.acquire_lease(r, item["id"], "token")
        assert mcp.mark_provider_attempt(r, item["id"], "token", item)
        assert item["provider_repl_id"] == target
        assert not mcp.mark_provider_attempt(r, item["id"], "token", item)
        assert mcp.finalize_lease(r, item["id"], "token", item, 900)


@pytest.mark.parametrize("change", [
    {"work_scope": None}, {"project": None}, {"submitted_repl_id": None},
    {"dispatch_project": "open-manus"}, {"dispatch_repl_id": "fleet"},
    {"submitted_repl_id": "../bad"}, {"work_scope": "fleet_platform"},
    {"title": "edited"}, {"decided_by": None}])
def test_inconsistent_pins_fail_every_enqueue_path(r, change):
    item = approved(r)
    item.update(change)
    r.set(f"devreq:item:{item['id']}", json.dumps(item))
    for force in (False, True):
        with pytest.raises(ValueError):
            mcp.admin_enqueue(r, item["id"], force)
    assert mcp.sweep_dispatch_backlog(r) == []
    with pytest.raises(ValueError):
        routing.dispatch_route(r, item)


def test_effective_default_collision_and_override(r):
    r.set(projects.K_TARGET, "app-id")
    assert projects.resolve_project(r, "app") == ("app", "app-id")
    r.set(projects.K_PROJECTS, json.dumps({"app": "app-id"}))
    with pytest.raises(ValueError, match="more than one"):
        projects.resolve_project(r, "app")


def test_immutable_mapping_and_disabled_name(r):
    item = approved(r)
    r.set(projects.K_PROJECTS, json.dumps({"app": "replacement"}))
    assert routing.dispatch_route(r, item) == ("app", "app-id")
    r.set(projects.K_PROJECTS, "{}")
    with pytest.raises(ValueError):
        routing.dispatch_route(r, item)


def test_correction_preserves_evidence_and_requires_new_confirmation(r):
    item = approved(r)
    old_token = routing.review_token(r, item)
    fixed = routing.correct_route(r, item["id"], "fleet_platform", "open-manus", old_token, "owner")
    assert fixed["original_submission"]["project"] == "app"
    assert fixed["routing_history"][0]["dispatch_repl_id"] == "app-id"
    assert r.llen("devreq:dispatch") == 0
    with pytest.raises(ValueError):
        dr.set_status(item["id"], "approved", "owner", old_token)
    new = dr.set_status(item["id"], "approved", "owner", routing.review_token(r, fixed))
    assert new["dispatch_repl_id"] == "fleet"


def test_attempt_and_live_lease_cannot_be_forced_or_corrected(r):
    item = approved(r)
    assert mcp.acquire_lease(r, item["id"], "worker")
    assert mcp.admin_enqueue(r, item["id"], True) == 0
    with pytest.raises(ValueError):
        routing.correct_route(r, item["id"], "project_app", "app", routing.review_token(r, item), "owner")
    assert mcp.mark_provider_attempt(r, item["id"], "worker", item)
    mcp.release_lease(r, item["id"], "worker")
    with pytest.raises(ValueError, match="already issued"):
        mcp.admin_enqueue(r, item["id"], True)


def test_approval_confirmation_race(r):
    item = dr.submit_request("Title", "Details", "app", "project_app")
    token = routing.review_token(r, item)
    r.set(projects.K_PROJECTS, json.dumps({"app": "different"}))
    with pytest.raises(ValueError, match="changed"):
        dr.set_status(item["id"], "approved", "owner", token)
    assert r.llen("devreq:dispatch") == 0


def test_legacy_requires_review_not_configuration_inference(r):
    item = {"id": "legacy", "status": "pending", "title": "Old"}
    r.set("devreq:item:legacy", json.dumps(item))
    with pytest.raises(ValueError):
        dr.set_status("legacy", "approved", "owner", routing.review_token(r, item))
    fixed = routing.correct_route(r, "legacy", "project_app", "app", routing.review_token(r, item), "owner")
    assert fixed["status"] == "pending"


def test_concurrent_approvers_only_queue_once(r):
    item = dr.submit_request("Title", "Details", "app", "project_app")
    token = routing.review_token(r, item)
    def decide(_):
        try:
            return dr.set_status(item["id"], "approved", "owner", token)
        except ValueError:
            return None
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(decide, range(2)))
    assert r.llen("devreq:dispatch") == 1


def test_store_rejects_nonowner_even_with_current_confirmation(r):
    item = dr.submit_request("Title", "Details", "app", "project_app")
    token = routing.review_token(r, item)
    with pytest.raises(ValueError, match="configured owner"):
        dr.set_status(item["id"], "approved", "other-reviewer", token)
    with pytest.raises(ValueError, match="configured owner"):
        routing.correct_route(r, item["id"], "project_app", "app", token, "other-reviewer")
    assert r.llen("devreq:dispatch") == 0