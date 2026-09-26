"""Actual dashboard middleware with isolated CRM Redis and fake identity provider."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient

from crm.service import CRMService


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from crm import api
    from hermes_cli import web_server
    svc = CRMService(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(api, "service", lambda: svc)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", None, raising=False)
    # No lifespan: do not start gateway workers or any runtime integrations.
    return svc, web_server, TestClient(web_server.app)


def request_create(client, headers=None, key="create"):
    return client.post("/api/crm/action", headers=headers or {}, json={
        "action": "create", "args": {"lead": {"name": "Synthetic boundary test"}, "idempotency_key": key}})


def configure_source(svc):
    assert svc.execute("source_save", {"id": "canaok", "revision": 1, "enabled": True,
                                     "mapping": {"name": "contact", "email": "address"}}, "test", "owner")["ok"]
    return svc.execute("source_rotate_secret", {"id": "canaok"}, "test", "owner")["result"]["secret"]


def test_actual_token_auth_owner_and_rejection(boundary):
    svc, web, client = boundary
    assert request_create(client).status_code == 401
    assert request_create(client, {web._SESSION_HEADER_NAME: "invalid"}).status_code == 401
    assert client.get("/api/crm/discover").status_code == 401
    result = request_create(client, {web._SESSION_HEADER_NAME: web._SESSION_TOKEN})
    assert result.status_code == 200
    assert result.json()["result"]["history"][0]["actor"] == "dashboard:local-owner"
    assert len(svc.store.records("leads")) == 1


def test_actual_gated_auth_owner_and_rejection(boundary, monkeypatch):
    svc, web, client = boundary
    from hermes_cli.dashboard_auth import middleware
    from hermes_cli.dashboard_auth.base import Session
    session = Session("owner-123", "test@example.invalid", "Test", "org", "test-provider",
                      int(time.time()) + 3600, "accepted-cookie", "")
    provider = SimpleNamespace(name="test-provider", verify_session=lambda *, access_token: session if access_token == "accepted-cookie" else None)
    monkeypatch.setattr(middleware, "list_session_providers", lambda: [provider])
    monkeypatch.setattr(web.app.state, "auth_required", True)
    assert request_create(client).status_code == 401
    # A local dashboard token must never substitute for gated cookies.
    assert request_create(client, {web._SESSION_HEADER_NAME: web._SESSION_TOKEN}).status_code == 401
    client.cookies.set("hermes_session_at", "wrong-cookie")
    assert request_create(client).status_code == 401
    client.cookies.set("hermes_session_at", "accepted-cookie")
    result = request_create(client)
    assert result.status_code == 200
    assert result.json()["result"]["history"][0]["actor"] == "dashboard:test-provider:owner-123"
    assert len(svc.store.records("leads")) == 1


@pytest.mark.parametrize("gated", [False, True])
def test_actual_exact_webhook_bypass_both_modes(boundary, monkeypatch, gated):
    svc, web, client = boundary
    monkeypatch.setattr(web.app.state, "auth_required", gated)
    secret = configure_source(svc)
    headers = {"Authorization": "Bearer " + secret}
    payload = {"event_id": "mapped-one", "contact": "Mapped test", "address": "test@example.invalid"}
    url = "/api/crm/webhook/canaok"
    denied = client.post(url, json=payload)
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"  # reached source verifier
    response = client.post(url, headers=headers, json=payload)
    assert response.status_code == 200
    lead = svc.store.lead(response.json()["result"]["id"])
    assert lead["name"] == "Mapped test"
    for path in ("/api/crm/webhook/canaok/extra", "/api/crm/webhook/canaok-extra/extra", "/api/crm/action"):
        assert client.post(path, headers=headers, json=payload).status_code == 401
    assert client.get(url, headers=headers).status_code == 401


def test_mapping_rate_limit_and_secret_redaction(boundary, caplog, monkeypatch):
    svc, _, client = boundary
    secret = configure_source(svc)
    invalid = svc.execute("source_save", {"id": "badmap", "mapping": {"revision": "x"}}, "owner", "owner")
    assert invalid["error"]["code"] == "validation"
    invalid = svc.execute("source_save", {"id": "badmap", "mapping": {"name": {"nested": "unsupported"}}}, "owner", "owner")
    assert invalid["error"]["code"] == "validation"
    headers = {"Authorization": "Bearer " + secret}
    url = "/api/crm/webhook/canaok"
    # Freeze minute bucket; prefill through isolated Redis, not live credentials.
    from crm import api
    monkeypatch.setattr(api.time, "time", lambda: 12000)
    svc.redis.set("crm:v1:rate:canaok:200", 60)
    limited = client.post(url, headers=headers, json={"event_id": "limit", "contact": "Private payload marker"})
    assert limited.status_code == 429
    discovery = svc.execute("discover", {}, "agent:test", "agent")
    sources = svc.execute("sources", {}, "agent:test", "agent")
    assert secret not in json.dumps([discovery, sources, limited.json()])
    assert "secret_hash" not in json.dumps([discovery, sources])
    assert secret not in caplog.text
    assert "Private payload marker" not in caplog.text
    # Storage errors must not expose the exception's embedded secret/payload.
    monkeypatch.setattr(svc.store, "records", lambda _: (_ for _ in ()).throw(RuntimeError(secret)))
    error = svc.execute("list", {}, "agent:test", "agent")
    assert error["error"]["code"] == "unavailable"
    assert secret not in json.dumps(error) + caplog.text


def test_crm_native_discovery_and_platform_exposure(tmp_path):
    from tools.registry import discover_builtin_tools, registry
    from toolsets import _HERMES_CORE_TOOLS, resolve_toolset
    # Exercise the real AST-based startup discovery on the CRM module only,
    # without importing unrelated provider tools with environment side effects.
    module = Path(__file__).resolve().parents[1] / "tools" / "crm.py"
    (tmp_path / "crm.py").write_text(module.read_text())
    assert discover_builtin_tools(tmp_path) == ["tools.crm"]
    entry = registry.get_entry("crm")
    assert entry is not None and entry.toolset == "vault"
    assert "crm" in _HERMES_CORE_TOOLS
    for platform in ("vault", "hermes-cli", "hermes-discord", "hermes-telegram", "hermes-cron"):
        assert "crm" in resolve_toolset(platform)
    schemas = registry.get_definitions({"crm"}, quiet=True)
    assert schemas[0]["function"]["name"] == "crm"