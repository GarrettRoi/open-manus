"""Focused coverage for the owner-managed multi-project dispatch registry.

These tests use the vault test bootstrap's fakeredis fallback. They exercise
only configuration/discovery; no dispatcher task, Replit request, or live
OAuth flow is started.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest.importorskip("services.vault.dev_projects")

import app as vault_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from services.vault import dev_projects  # noqa: E402


SESSION = "project-registry-test-session"
CSRF = "project-registry-csrf"
AGENT_TOKEN = "project-registry-agent-token"


@pytest.fixture()
def client(monkeypatch):
    # Keep this test isolated from any Redis state left by another vault test.
    vault_app.r.delete("replitmcp:projects", "replitmcp:target_repl")
    async def idle_dispatch(_mcp):
        await asyncio.Event().wait()

    async def idle_backup(_redis):
        await asyncio.Event().wait()

    monkeypatch.setattr(vault_app.replit_mcp_mod, "dispatch_loop", idle_dispatch)
    monkeypatch.setattr(vault_app.vault_backup, "backup_loop", idle_backup)
    vault_app.SESSION_TOKENS[SESSION] = time.time() + 600
    vault_app.SESSION_CSRF[SESSION] = CSRF
    jar = httpx.Cookies()
    jar.set("vault_session", SESSION)
    with TestClient(vault_app.app, raise_server_exceptions=True, cookies=jar) as c:
        yield c
    vault_app.r.delete("replitmcp:projects", "replitmcp:target_repl")
    vault_app.SESSION_TOKENS.pop(SESSION, None)
    vault_app.SESSION_CSRF.pop(SESSION, None)


def _save(client, payload, *, csrf=CSRF, content_type="application/json"):
    headers = {"Content-Type": content_type, "Origin": "http://testserver"}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return client.post("/api/admin/replit-mcp/config", json=payload, headers=headers)


def test_configuration_requires_admin_and_browser_csrf(client):
    no_cookie = TestClient(vault_app.app)
    response = no_cookie.post(
        "/api/admin/replit-mcp/config",
        json={"projects": {"alpha": "repl-alpha"}},
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401

    response = _save(client, {"projects": {"alpha": "repl-alpha"}}, csrf=None)
    assert response.status_code == 403

    response = _save(
        client,
        {"projects": {"alpha": "repl-alpha"}},
        content_type="text/plain",
    )
    # FastAPI validates the typed body before entering the handler when the
    # media type is not JSON.
    assert response.status_code == 422


def test_projects_are_normalized_and_persisted_atomically(client):
    response = _save(
        client,
        {
            "target_repl": "repl-default",
            "projects": {
                " Research Lab ": "repl-research",
                "open-manus": "repl-explicit",
            },
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["projects"] == {
        "open-manus": "repl-explicit",
        "research-lab": "repl-research",
    }
    assert data["target_repl"] == "repl-default"

    stored = json.loads(vault_app.r.get("replitmcp:projects"))
    assert stored == data["projects"]
    assert vault_app.r.get("replitmcp:target_repl") == "repl-default"

    # The explicit open-manus entry wins over the legacy fallback.
    status = client.get("/api/admin/replit-mcp/status")
    assert status.status_code == 200
    assert status.json()["project_targets"]["open-manus"] == "repl-explicit"


def test_registry_only_update_preserves_legacy_default_and_delete_is_persistent(client):
    assert _save(
        client,
        {"target_repl": "repl-default", "projects": {"alpha": "repl-alpha"}},
    ).status_code == 200

    response = _save(client, {"projects": {}})
    assert response.status_code == 200
    assert response.json()["projects"] == {}
    assert response.json()["target_repl"] == "repl-default"
    assert vault_app.r.get("replitmcp:target_repl") == "repl-default"


def test_contract_rejects_name_and_id_collisions():
    with pytest.raises(ValueError):
        dev_projects.validate_projects({
            "Research Lab": "repl-one",
            " research-lab ": "repl-two",
        })
    with pytest.raises(ValueError):
        dev_projects.validate_projects({"bad/name": "repl-same"})


def test_agent_discovery_returns_names_not_project_ids(client):
    token_hash = vault_app.hash_token(AGENT_TOKEN)
    vault_app.r.hset(
        "vault:agent:harmony",
        mapping={"name": "harmony", "token_hash": token_hash},
    )
    vault_app.r.zadd("vault:agents", {"harmony": time.time()})
    try:
        assert _save(
            client,
            {"target_repl": "repl-default", "projects": {"Research Lab": "repl-secret-id"}},
        ).status_code == 200
        response = client.get(
            "/api/vault/projects",
            headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        )
        assert response.status_code == 200
        assert response.json()["projects"] == ["open-manus", "research-lab"]
        assert "repl-secret-id" not in response.text
    finally:
        vault_app.r.delete("vault:agent:harmony")
        vault_app.r.zrem("vault:agents", "harmony")


def test_dashboard_includes_owner_registry_controls(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Dev Request Destinations" in response.text
    assert "saveProjectRegistry" in response.text
    assert "X-CSRF-Token" in response.text
    assert "no new credentials" in response.text.lower()
