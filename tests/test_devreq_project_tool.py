"""Agent destination discovery and request normalization, with no network I/O."""
import asyncio
import json
from unittest.mock import patch

import fakeredis
import pytest

from tools import dev_requests as dr
from services.vault import replit_mcp


def test_discovery_returns_names_not_ids_or_tokens():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"second-app": "private-project-id"}))
    r.set("replitmcp:target_repl", "legacy-id")
    r.set("replitmcp:tokens", "secret-token")
    with patch.object(dr, "_redis", return_value=r):
        result = dr.dev_request_tool({"action": "projects"})
    assert set(json.loads(result)["projects"]) == {"second-app", "open-manus"}
    assert "private-project-id" not in result
    assert "legacy-id" not in result
    assert "secret-token" not in result


def test_submit_names_destination_and_still_requires_approval():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"second-app": "second-id"}))
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "Fix layout", "description": "Details",
            "work_scope": "project_app", "project": "  SECOND   App ",
        }))
    assert result["project"] == "second-app"
    assert result["work_scope"] == "project_app"
    assert result["status"] == "pending"
    assert r.llen("devreq:pending") == 1
    assert r.llen("devreq:dispatch") == 0


def test_unknown_destination_is_rejected_before_record_create():
    r = fakeredis.FakeRedis(decode_responses=True)
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "Title", "description": "Details",
            "work_scope": "project_app", "project": "unregistered",
        }))
    assert "error" in result
    assert r.get("devreq:seq") is None
    assert r.llen("devreq:pending") == 0


def test_fleet_scope_cannot_route_to_vowsok_even_when_configured():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "vowsok-id"}))
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "OAuth", "description": "Details",
            "work_scope": "fleet_platform", "project": "vowsok",
        }))
    assert "must target 'open-manus'" in result["error"]
    assert r.get("devreq:seq") is None


def test_sole_vowsok_registry_does_not_default_missing_target():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "vowsok-id"}))
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "OAuth", "description": "Details",
            "work_scope": "fleet_platform",
        }))
    assert "project is required" in result["error"]
    assert r.get("devreq:seq") is None


def test_missing_scope_is_rejected():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "vowsok-id"}))
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "Feature", "description": "Details",
            "project": "vowsok",
        }))
    assert "work_scope is required" in result["error"]


def test_invalid_project_name_is_rejected_before_record_create():
    r = fakeredis.FakeRedis(decode_responses=True)
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "Feature", "description": "Details",
            "work_scope": "project_app", "project": "../vowsok",
        }))
    assert "invalid project name" in result["error"]
    assert r.get("devreq:seq") is None


def test_registry_change_blocks_approval_without_consuming_pending():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "old-id"}))
    with patch.object(dr, "_redis", return_value=r):
        item = dr.submit_request(
            "Feature", "Details", "vowsok", "project_app")
        r.set("replitmcp:projects", json.dumps({"vowsok": "new-id"}))
        with pytest.raises(ValueError, match="changed since submission"):
            dr.set_status(item["id"], "approved", "owner")
    stored = json.loads(r.get(f"devreq:item:{item['id']}"))
    assert stored["status"] == "pending"
    assert r.lrange("devreq:pending", 0, -1) == [item["id"]]


def test_approval_pins_submission_snapshot():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "stable-id"}))
    with patch.object(dr, "_redis", return_value=r), \
            patch.object(dr, "_enqueue_if_unclaimed", return_value=True):
        item = dr.submit_request(
            "Feature", "Details", "vowsok", "project_app")
        approved = dr.set_status(item["id"], "approved", "owner")
    assert approved["dispatch_project"] == "vowsok"
    assert approved["dispatch_repl_id"] == "stable-id"


def test_unpinned_scoped_dispatch_refuses_changed_registry():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "new-id"}))
    item = {
        "id": "41", "status": "approved", "work_scope": "project_app",
        "project": "vowsok", "submitted_repl_id": "old-id",
    }
    mcp = type("MCP", (), {"r": r})()
    with pytest.raises(
            replit_mcp.ReplitMCPRoutingError,
            match="changed since submission"):
        asyncio.run(replit_mcp._resolve_and_pin_route(
            mcp, "41", "lease-token", item))


def test_started_record_keeps_immutable_dispatch_pin_after_registry_change():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("replitmcp:projects", json.dumps({"vowsok": "new-id"}))
    item = {
        "id": "42", "status": "approved", "dispatch_status": "started",
        "work_scope": "project_app", "project": "vowsok",
        "submitted_repl_id": "old-id",
        "dispatch_project": "vowsok", "dispatch_repl_id": "old-id",
    }
    mcp = type("MCP", (), {"r": r})()
    route = asyncio.run(replit_mcp._resolve_and_pin_route(
        mcp, "42", "lease-token", item))
    assert route == ("vowsok", "old-id")
    assert item["dispatch_repl_id"] == "old-id"