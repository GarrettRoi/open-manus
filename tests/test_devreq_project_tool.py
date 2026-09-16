"""Agent destination discovery and request normalization, with no network I/O."""
import json
from unittest.mock import patch

import fakeredis
import pytest

from tools import dev_requests as dr


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


@pytest.mark.parametrize("project,expected", [
    ("  SECOND   App ", "second-app"),
    (" \t ", "open-manus"),
    ("", "open-manus"),
])
def test_submit_names_destination_and_still_requires_approval(project, expected):
    r = fakeredis.FakeRedis(decode_responses=True)
    with patch.object(dr, "_redis", return_value=r):
        result = json.loads(dr.dev_request_tool({
            "action": "submit", "title": "Fix layout", "description": "Details",
            "project": project,
        }))
    assert result["project"] == expected
    assert result["status"] == "pending"
    assert r.llen("devreq:pending") == 1
    assert r.llen("devreq:dispatch") == 0


def test_unknown_destination_is_preserved_not_changed_to_default():
    r = fakeredis.FakeRedis(decode_responses=True)
    with patch.object(dr, "_redis", return_value=r):
        item = dr.submit_request("Title", "Details", project="unregistered")
    assert item["project"] == "unregistered"
    assert item["status"] == "pending"