"""model_routing tool tests — audit/downshift/escalate/status actions."""
import json

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def economy_cfg(monkeypatch):
    import tools.model_routing_tool as mrt
    monkeypatch.setattr(
        mrt, "_load_cfg",
        lambda: {
            "model": {"default": "anthropic/claude-opus-4.6", "provider": "openrouter"},
            "routing": {"economy": "deepseek/deepseek-chat"},
        },
    )


def _tool(**kwargs):
    from tools.model_routing_tool import model_routing
    return json.loads(model_routing(**kwargs))


def _make_job(model="strong-model"):
    from cron.jobs import create_job
    return create_job(prompt="poll the feed", schedule="every 30m",
                      name="feed-poll", model=model)


def test_audit_lists_cron_jobs_with_models(temp_home, economy_cfg):
    job = _make_job()
    out = _tool(action="audit")
    assert out["success"] is True
    assert out["economy_model"]["model"] == "deepseek/deepseek-chat"
    assert out["primary_model"] == "anthropic/claude-opus-4.6"
    entry = next(j for j in out["cron_jobs"] if j["id"] == job["id"])
    assert entry["model"] == "strong-model"
    assert entry["downshifted"] is False


def test_downshift_then_status_and_escalate(temp_home, economy_cfg):
    from cron.jobs import get_job

    job = _make_job()
    out = _tool(action="downshift", job_ids=[job["id"]], reason="owner approved")
    assert out["success"] is True
    assert out["cron"][0]["new_model"] == "deepseek/deepseek-chat"
    assert get_job(job["id"])["model"] == "deepseek/deepseek-chat"

    status = _tool(action="status")
    assert any(j["id"] == job["id"] for j in status["downshifted_cron_jobs"])

    out = _tool(action="escalate", job_ids=[job["id"]], reason="output was bad")
    assert out["cron"][0]["new_model"] == "strong-model"
    assert get_job(job["id"])["model"] == "strong-model"


def test_downshift_refuses_without_economy_model(temp_home, monkeypatch):
    import tools.model_routing_tool as mrt
    monkeypatch.setattr(mrt, "_load_cfg", lambda: {"model": {"default": "x"}})
    job = _make_job()
    out = _tool(action="downshift", job_ids=[job["id"]])
    assert out["success"] is False
    assert "economy model" in out["error"].lower()


def test_downshift_rejects_no_agent_jobs(temp_home, economy_cfg):
    from cron.jobs import create_job
    job = create_job(prompt=None, schedule="every 30m", name="watchdog",
                     script="check.sh", no_agent=True)
    out = _tool(action="downshift", job_ids=[job["id"]])
    assert "no_agent" in out["cron"][0]["error"]


def test_unknown_action_errors(temp_home, economy_cfg):
    out = _tool(action="bogus")
    assert out["success"] is False


def test_escalate_requires_ids(temp_home, economy_cfg):
    out = _tool(action="escalate")
    assert out["success"] is False


def test_tool_is_registered():
    import tools.model_routing_tool  # noqa: F401 — self-registers
    from tools.registry import registry
    entry = registry.get_entry("model_routing")
    assert entry is not None
    assert entry.toolset == "cronjob"
