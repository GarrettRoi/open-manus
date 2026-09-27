"""Isolated discovery contract: grant revocation and owner-only sharing."""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import fakeredis
import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as vault_app  # noqa: E402
from connections import ConnectionStore  # noqa: E402


@pytest.fixture
def setup(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(vault_app, "r", r)
    monkeypatch.setattr(vault_app, "store", ConnectionStore(r, lambda s: s, lambda s: s))
    for cid, service in (("MAIL", "email"), ("DRIVE", "google")):
        vault_app.store.save(cid, service=service, status="ready")
        vault_app.store.set_grant("samantha", cid, True)
    vault_app.r.zadd(vault_app.PFX_AGENT_INDEX, {"harmony": 1})
    vault_app.r.hset(f"{vault_app.PFX_AGENT}harmony", mapping={
        "token_hash": vault_app.hash_token("test-agent-token")})
    client = TestClient(vault_app.app)
    yield client, r
    client.close()


def _admin(client, body, *, headers=None):
    return client.put("/api/admin/discovery/samantha", json=body, headers=headers or {
        "X-Vault-Admin-Token": "vault-test-admin-password"})


def _get(client, path):
    return client.get(path, headers={"Authorization": "Bearer test-agent-token"})


def test_discovery_grants_filters_revocation_and_freshness(setup):
    client, r = setup
    body = {"connections": [
        {"connection_id": "MAIL", "capabilities": ["Inbox triage"],
         "shareable_accounts": ["shared@example.org"]},
        {"connection_id": "DRIVE", "capabilities": ["Docs"],
         "shareable_accounts": []}]}
    assert _admin(client, body).status_code == 200
    r.set("dispatch:availability:samantha", json.dumps({"updated_at": time.time()}))
    response = _get(client, "/api/vault/discovery?capability=inbox&account=shared@example.org")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 1
    assert result["results"][0]["availability"]["state"] == "online"
    assert result["results"][0]["checked_at"] > 0
    assert result["results"][0]["connections"] == [{
        "service": "email", "capabilities": ["inbox triage"],
        "shareable_accounts": ["shared@example.org"], "granted": True,
        "configuration": "ready",
        "health": {"state": "unknown", "checked_at": None, "freshness": "unknown"}}]
    assert _get(client, "/api/vault/discovery?service=google&account=shared@example.org").json()["total"] == 0
    assert _get(client, "/api/vault/discovery?query=docs").json()["total"] == 1
    vault_app.r.hset("vault:conn:DRIVE", "status", "needs_login")
    drive = _get(client, "/api/vault/discovery?service=google").json()["results"][0]["connections"][0]
    assert drive["configuration"] == "not_ready"
    assert drive["health"]["state"] == "unknown"
    r.hset("vault:conn:DRIVE", mapping={
        "last_test_ok": "1", "last_test_at": datetime.now(timezone.utc).isoformat()})
    drive = _get(client, "/api/vault/discovery?service=google").json()["results"][0]["connections"][0]
    assert drive["health"]["state"] == "verified"
    assert drive["health"]["freshness"] == "fresh"
    r.hset("vault:conn:DRIVE", "last_test_at",
           (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    assert _get(client, "/api/vault/discovery?service=google").json()["results"][0]["connections"][0]["health"]["freshness"] == "stale"
    assert client.get("/api/admin/discovery/samantha", headers={
        "X-Vault-Admin-Token": "vault-test-admin-password"}).json()["connections"] == [
            {"connection_id": "MAIL", "capabilities": ["inbox triage"],
             "shareable_accounts": ["shared@example.org"]},
            {"connection_id": "DRIVE", "capabilities": ["docs"], "shareable_accounts": []}]
    vault_app.store.set_grant("samantha", "MAIL", False)
    assert _get(client, "/api/vault/discovery?account=shared@example.org").json()["total"] == 0
    assert len(_get(client, "/api/vault/discovery/samantha").json()["connections"]) == 1
    r.set("dispatch:availability:samantha", json.dumps({"updated_at": time.time() - 400}))
    assert _get(client, "/api/vault/discovery/samantha").json()["availability"]["state"] == "stale"


def test_granted_services_without_approval_or_valid_metadata(setup):
    client, r = setup
    base = _get(client, "/api/vault/discovery?service=email").json()
    assert base["total"] == 1
    assert base["results"][0]["connections"][0]["capabilities"] == []
    assert base["results"][0]["connections"][0]["shareable_accounts"] == []
    for broken in ("not-json", "[]", '{"connections": 42}',
                   '{"connections":[{"connection_id":"MAIL","capabilities":"private","shareable_accounts":[]}]}'):
        r.set("vault:discovery:samantha", broken)
        result = _get(client, "/api/vault/discovery/samantha")
        assert result.status_code == 200
        assert len(result.json()["connections"]) == 2
        assert all(not c["capabilities"] and not c["shareable_accounts"]
                   for c in result.json()["connections"])
    vault_app.store.set_grant("samantha", "MAIL", False)
    assert _get(client, "/api/vault/discovery?service=email").json()["total"] == 0


def test_admin_validation_and_agent_auth(setup):
    client, _ = setup
    body = {"connections": [{"connection_id": "MAIL", "capabilities": ["private"],
                              "shareable_accounts": ["admin@example.org"]}]}
    assert client.put("/api/admin/discovery/samantha", json=body).status_code == 401
    assert client.get("/api/admin/discovery/samantha").status_code == 401
    assert client.get("/api/vault/discovery").status_code == 401
    assert _admin(client, body).status_code == 200
    assert _admin(client, {"connections": [{"connection_id": "NO_GRANT",
        "capabilities": [], "shareable_accounts": []}]}).status_code == 400
    assert _admin(client, {"connections": [{"connection_id": "MAIL",
        "capabilities": ["ok"], "shareable_accounts": ["not an email"]}]}).status_code == 400
    assert _get(client, "/api/vault/discovery?limit=21").status_code == 400
    assert _get(client, "/api/vault/discovery/not-real").status_code == 404


def test_admin_browser_session_requires_csrf(setup):
    client, _ = setup
    session = "discovery-test"
    vault_app.SESSION_TOKENS[session] = time.time() + 600
    vault_app.SESSION_CSRF[session] = "csrf-token"
    client.cookies.set("vault_session", session)
    body = {"connections": []}
    try:
        assert _admin(client, body, headers={"Origin": "http://testserver"}).status_code == 403
        assert _admin(client, body, headers={
            "Origin": "http://testserver", "X-CSRF-Token": "csrf-token"}).status_code == 200
    finally:
        vault_app.SESSION_TOKENS.pop(session, None)
        vault_app.SESSION_CSRF.pop(session, None)