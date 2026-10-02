"""Hermetic developer channel API and private dispatch prompt coverage."""
import json
import os
import sys
import time
from unittest.mock import AsyncMock

import fakeredis
import pytest
from redis.exceptions import ConnectionError
from starlette.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import app as vault_app
from services.vault import dev_clarifications, replit_mcp


@pytest.fixture
def channel(monkeypatch):
    cache = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(vault_app, "r", cache)
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "https://vault.invalid")
    monkeypatch.setattr(vault_app, "ADMIN_API_TOKEN", "test-admin")
    for rid, dest in (("1", "repl-one"), ("2", "repl-two")):
        cache.set(f"devreq:item:{rid}", json.dumps({
            "id": rid, "agent": "lexi", "status": "approved",
            "project": "open-manus", "dispatch_repl_id": dest,
        }))
    token = dev_clarifications.ClarificationStore(cache).provision(
        "1", "repl-one", "replit:repl-one")
    client = TestClient(vault_app.app)  # no startup/background tasks
    yield cache, client, token
    client.close()


BASE = "/api/dev-requests/1/destinations/repl-one/clarifications"


def ask(client, token, key="one", question="Which behavior is intended?", base=BASE):
    return client.post(base, headers={"Authorization": f"Bearer {token}"},
                       json={"question": question, "idempotency_key": key})


def test_bearer_only_and_scope(channel):
    cache, client, token = channel
    session = "clarification-api-session"
    vault_app.SESSION_TOKENS[session] = time.time() + 60
    try:
        client.cookies.set("vault_session", session)
        assert client.post(BASE, json={"question": "Q", "idempotency_key": "k"},
                           headers={"X-Vault-Admin-Token": "test-admin"}).status_code == 401
        assert ask(client, token, base=BASE.replace("repl-one", "repl-two")).status_code == 401
        assert ask(client, token, base=BASE.replace("/1/", "/2/")).status_code == 401
        response = ask(client, token)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        qid = response.json()["id"]
        assert client.get(f"{BASE}/{qid}").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get(f"{BASE}/{qid}", headers=headers).status_code == 200
        cache.delete(dev_clarifications.ClarificationStore._cap(token))
        assert client.get(f"{BASE}/{qid}", headers=headers).status_code == 401
    finally:
        vault_app.SESSION_TOKENS.pop(session, None)


def test_idempotency_and_rate_limit(channel):
    cache, client, token = channel
    first = ask(client, token)
    assert ask(client, token).json()["id"] == first.json()["id"]
    assert ask(client, token, question="Different").status_code == 400
    for key in ("two", "three", "four"):
        assert ask(client, token, key).status_code == 200
    limited = ask(client, token, "five")
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["retry-after"]) <= 300


def test_redis_failure_is_safe_503(channel, monkeypatch):
    cache, client, token = channel
    def unavailable(*args, **kwargs):
        raise ConnectionError("private redis password should not leak")
    monkeypatch.setattr(cache, "eval", unavailable)
    response = ask(client, token)
    assert response.status_code == 503
    assert "password" not in response.text


def test_admin_provision_requires_auth_csrf_and_https(channel, monkeypatch):
    cache, client, token = channel
    endpoint = "/api/admin/dev-requests/1/clarifications/provision"
    body = {"destination": "repl-one", "developer_id": "replit:repl-one"}
    assert client.post(endpoint, json=body).status_code == 401
    response = client.post(endpoint, json=body,
                           headers={"X-Vault-Admin-Token": "test-admin"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["expires_in"] == 86400
    session = "clarification-provision-session"
    vault_app.SESSION_TOKENS[session] = time.time() + 60
    vault_app.SESSION_CSRF[session] = "test-csrf"
    try:
        client.cookies.set("vault_session", session)
        assert client.post(endpoint, json=body).status_code == 403
        headers = {"Origin": "http://testserver", "X-CSRF-Token": "test-csrf"}
        assert client.post(endpoint, json=body, headers=headers).status_code == 200
        monkeypatch.setattr(vault_app, "PUBLIC_URL", "http://vault.invalid")
        assert client.post(endpoint, json=body, headers=headers).status_code == 503
    finally:
        vault_app.SESSION_TOKENS.pop(session, None)
        vault_app.SESSION_CSRF.pop(session, None)


@pytest.mark.parametrize("url", ["", "http://vault.invalid", "https://user:pw@vault.invalid",
                                 "https://vault.invalid?secret=x"])
def test_prompt_fails_closed_without_safe_https_url(channel, url):
    cache, client, token = channel
    mcp = replit_mcp.ReplitMCP(cache, str, str, url)
    item = json.loads(cache.get("devreq:item:1"))
    before = set(cache.keys())
    with pytest.raises(replit_mcp.ReplitMCPError, match="HTTPS"):
        replit_mcp._provision_developer_prompt(mcp, "1", item, "repl-one")
    assert set(cache.keys()) == before


@pytest.mark.asyncio
async def test_private_prompt_only_hashed_token_is_stored(channel):
    cache, client, existing = channel
    mcp = replit_mcp.ReplitMCP(cache, str, str, "https://vault.invalid")
    item = json.loads(cache.get("devreq:item:1"))
    prompt = replit_mcp._provision_developer_prompt(mcp, "1", item, "repl-one")
    token = prompt.split("Authorization: Bearer ", 1)[1].splitlines()[0]
    mcp._mcp_call_tool = AsyncMock(return_value={"accepted": True, "echo": prompt})
    result = await mcp.start_agent_run(prompt, repl_id="repl-one")
    assert mcp._mcp_call_tool.call_args.args[1]["replId"] == "repl-one"
    assert dev_clarifications.ClarificationStore._cap(token) in cache.keys()
    stored = "\n".join(str(cache.get(k)) for k in cache.keys())
    assert token not in stored
    assert token not in json.dumps(item)
    assert token not in json.dumps(replit_mcp._safe_dispatch_result(result))
    assert ask(client, token).status_code == 200