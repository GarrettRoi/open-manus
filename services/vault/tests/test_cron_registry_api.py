import json
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as vault_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

SESSION = "cron-registry-api-session"
CSRF = "cron-registry-csrf"
PREFIX = "fleet:cron:v1"


@pytest.fixture
def client():
    vault_app.SESSION_TOKENS[SESSION] = time.time() + 600
    vault_app.SESSION_CSRF[SESSION] = CSRF
    jar = httpx.Cookies()
    jar.set("vault_session", SESSION)
    for key in vault_app.r.scan_iter(f"{PREFIX}:*"):
        vault_app.r.delete(key)
    with TestClient(vault_app.app, cookies=jar) as c:
        yield c
    vault_app.SESSION_TOKENS.pop(SESSION, None)
    vault_app.SESSION_CSRF.pop(SESSION, None)


def _seed():
    vault_app.r.sadd(f"{PREFIX}:agents", "harmony")
    vault_app.r.hset(
        f"{PREFIX}:agent:harmony",
        mapping={"heartbeat_epoch": str(time.time()), "job_count": "1"},
    )
    vault_app.r.hset(
        f"{PREFIX}:jobs:harmony",
        "job-1",
        json.dumps({
            "id": "job-1", "name": "Briefing", "enabled": True,
            "control_revision": 0, "schedule_display": "daily",
        }),
    )


def _headers(**extra):
    return {
        "Origin": "http://testserver",
        "X-CSRF-Token": CSRF,
        **extra,
    }


def test_page_and_list_require_admin(client):
    _seed()
    assert client.get("/cron-jobs").status_code == 200
    assert client.get("/api/admin/cron-jobs").json()["stats"]["total"] == 1
    with TestClient(vault_app.app) as anonymous:
        assert anonymous.get("/api/admin/cron-jobs").status_code == 401


def test_browser_write_requires_origin_and_csrf(client):
    _seed()
    body = {"agent": "harmony", "job_id": "job-1", "enabled": False, "revision": 0}
    assert client.post("/api/admin/cron-jobs/toggle", json=body).status_code == 403
    assert client.post(
        "/api/admin/cron-jobs/toggle", json=body,
        headers={"Origin": "https://evil.example", "X-CSRF-Token": CSRF},
    ).status_code == 403
    assert client.post(
        "/api/admin/cron-jobs/toggle", json=body, headers=_headers()
    ).status_code == 200


def test_conflict_and_idempotency(client):
    _seed()
    body = {"agent": "harmony", "job_id": "job-1", "enabled": False, "revision": 9}
    assert client.post(
        "/api/admin/cron-jobs/toggle", json=body, headers=_headers()
    ).status_code == 409
    body["revision"] = 0
    headers = _headers(**{"Idempotency-Key": "toggle-one"})
    first = client.post("/api/admin/cron-jobs/toggle", json=body, headers=headers)
    second = client.post("/api/admin/cron-jobs/toggle", json=body, headers=headers)
    assert first.status_code == 200
    assert second.json()["status"] == "idempotent"


def test_freeze_endpoint(client):
    response = client.post(
        "/api/admin/cron-jobs/freeze",
        json={"enabled": True, "reason": "maintenance"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
