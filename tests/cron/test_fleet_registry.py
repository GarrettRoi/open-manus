import contextlib
import json
import sys
import threading
import types

import fakeredis

from cron import registry


def _job(job_id="job-1", enabled=True, revision=0):
    return {
        "id": job_id,
        "name": "Morning research",
        "prompt": "Find three market updates and summarize them",
        "schedule": {"kind": "cron", "expr": "0 8 * * *", "display": "daily at 8"},
        "schedule_display": "daily at 8",
        "enabled": enabled,
        "state": "scheduled" if enabled else "paused",
        "skills": ["research"],
        "registry_control_revision": revision,
    }


def _wire(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setenv("AGENT_NAME", "Harmony")
    monkeypatch.setattr(registry, "_redis", lambda: client)
    return client


def test_snapshot_is_sanitized_and_discovers_agent(monkeypatch):
    client = _wire(monkeypatch)
    monkeypatch.setattr(registry, "schedule_llm_description", lambda *args: False)
    job = _job()
    job["prompt"] += "\nAPI_KEY=do-not-publish-this"
    assert registry.publish_snapshot([job])
    assert client.smembers(f"{registry.PREFIX}:agents") == {"harmony"}
    row = json.loads(client.hget(f"{registry.PREFIX}:jobs:harmony", "job-1"))
    assert "do-not-publish-this" not in json.dumps(row)
    assert "prompt" not in row
    assert row["description"].startswith("- Schedule:")
    assert row["description_fingerprint"]


def test_snapshot_records_deletion_tombstone(monkeypatch):
    client = _wire(monkeypatch)
    monkeypatch.setattr(registry, "schedule_llm_description", lambda *args: False)
    registry.publish_snapshot([_job()])
    registry.publish_snapshot([])
    assert client.hget(f"{registry.PREFIX}:jobs:harmony", "job-1") is None
    assert client.hget(f"{registry.PREFIX}:tombstones:harmony", "job-1")


def test_desired_state_applies_only_newer_revision(monkeypatch):
    client = _wire(monkeypatch)
    monkeypatch.setattr(registry, "schedule_llm_description", lambda *args: False)
    records = [_job(revision=2)]
    saved = []
    import cron.jobs as jobs
    monkeypatch.setattr(jobs, "_jobs_lock", contextlib.nullcontext)
    monkeypatch.setattr(jobs, "load_jobs", lambda: records)
    monkeypatch.setattr(jobs, "_save_jobs_unlocked", lambda rows: saved.append(rows))
    monkeypatch.setattr(jobs, "compute_next_run", lambda *args: "2099-01-01T00:00:00+00:00")

    client.hset(
        f"{registry.PREFIX}:desired:harmony",
        "job-1",
        json.dumps({"enabled": False, "revision": 1, "operation_id": "stale"}),
    )
    assert registry.reconcile_desired_state()
    assert records[0]["enabled"] is True
    assert saved == []

    client.hset(
        f"{registry.PREFIX}:desired:harmony",
        "job-1",
        json.dumps({"enabled": False, "revision": 3, "operation_id": "new"}),
    )
    assert registry.reconcile_desired_state()
    assert records[0]["enabled"] is False
    assert records[0]["registry_control_revision"] == 3
    assert len(saved) == 1


def test_missing_job_stays_pending(monkeypatch):
    client = _wire(monkeypatch)
    import cron.jobs as jobs
    monkeypatch.setattr(jobs, "_jobs_lock", contextlib.nullcontext)
    monkeypatch.setattr(jobs, "load_jobs", lambda: [])
    client.hset(
        f"{registry.PREFIX}:desired:harmony",
        "gone",
        json.dumps({"enabled": False, "revision": 1}),
    )
    assert registry.reconcile_desired_state()
    assert client.hexists(f"{registry.PREFIX}:desired:harmony", "gone")


def test_freeze_and_no_redis_are_safe(monkeypatch):
    client = _wire(monkeypatch)
    assert not registry.fleet_freeze_active()
    assert registry.request_fleet_freeze("maintenance", "owner")
    assert registry.fleet_freeze_active()
    monkeypatch.setattr(registry, "_redis", lambda: None)
    assert registry.publish_snapshot([]) is False
    assert registry.reconcile_desired_state() is False
    assert registry.fleet_freeze_active() is False


def test_llm_description_enrichment_is_cached(monkeypatch):
    client = _wire(monkeypatch)

    class Message:
        content = "- Gather current data\n- Compare important changes\n- Deliver a concise report"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    import agent.auxiliary_client as auxiliary
    monkeypatch.setattr(auxiliary, "call_llm", lambda **kwargs: Response())
    fingerprint, _ = registry._description(_job())
    assert registry.schedule_llm_description(_job(), fingerprint)
    assert client.get(f"{registry.PREFIX}:description-source:{fingerprint}") == "llm"
    assert "Gather current data" in client.get(f"{registry.PREFIX}:description:{fingerprint}")


def test_quoted_secret_is_redacted_and_skips_llm(monkeypatch):
    client = _wire(monkeypatch)
    job = _job()
    job["prompt"] = 'Call API with {"token": "SuperSecretValue123456789"}'
    assert "SuperSecretValue" not in registry._safe_prompt(job["prompt"])
    called = False

    class Auxiliary:
        @staticmethod
        def call_llm(**kwargs):
            nonlocal called
            called = True

    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", Auxiliary)
    fingerprint, _ = registry._description(job)
    assert registry.schedule_llm_description(job, fingerprint) is False
    assert called is False


def test_redis_client_has_short_network_timeouts(monkeypatch):
    captured = {}

    class Redis:
        @staticmethod
        def from_url(url, **kwargs):
            captured.update(kwargs)
            return object()

    monkeypatch.setenv("REDIS_URL", "redis://unreachable.invalid")
    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=Redis))
    assert registry._redis() is not None
    assert captured["socket_connect_timeout"] <= 0.5
    assert captured["socket_timeout"] <= 0.75
    assert captured["retry_on_timeout"] is False


def test_debounced_sync_republishes_current_file_not_captured_jobs(monkeypatch):
    first_started = threading.Event()
    release = threading.Event()
    calls = []

    def publish(jobs=None):
        calls.append(jobs)
        if len(calls) == 1:
            first_started.set()
            assert release.wait(2)
        return True

    monkeypatch.setattr(registry, "publish_snapshot", publish)
    registry._sync_pending = False
    registry._sync_requested = False
    assert registry.schedule_sync([_job("old")])
    assert first_started.wait(2)
    assert registry.schedule_sync([_job("new")]) is False
    release.set()
    for _ in range(100):
        if len(calls) == 2 and not registry._sync_pending:
            break
        threading.Event().wait(0.01)
    assert calls == [None, None]
