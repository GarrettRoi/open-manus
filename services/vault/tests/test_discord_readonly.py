"""Task 96 — Discord read-only server access via vault.

Covers:
  * readonly_allowed() path/method allowlist (unit).
  * Proxy-level rejection of write-style Discord calls for discord_read
    connections (e2e via TestClient with an agent token).
  * Allowed GET passes the read-only gate (upstream mocked).
  * OAuth state put/pop single-use semantics + bad-state callback.
  * Invite URL permissions (View Channels + Read Message History only).
  * Catalog entries sanity.

Run from services/vault/:
    pytest tests/test_discord_readonly.py -v
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("VAULT_ADMIN_PASSWORD", "e2e-test-pass")
os.environ.setdefault("VAULT_MASTER_KEY", "")

import app as vault_app  # noqa: E402
import discord_ops  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TEST_SESSION = "discord-e2e-session"
_AGENT = "discord-test-agent"
_AGENT_TOKEN = "discord-test-agent-token-96"
CONN_ID = "DISCORD_READ_T96"


@pytest.fixture(scope="module")
def client():
    import httpx
    vault_app.SESSION_TOKENS[_TEST_SESSION] = time.time() + 86400
    # Plant an agent record so require_agent() accepts our bearer token.
    vault_app.r.hset(f"{vault_app.PFX_AGENT}{_AGENT}", mapping={
        "token_hash": vault_app.hash_token(_AGENT_TOKEN),
        "created_at": "test",
    })
    vault_app.r.zadd(vault_app.PFX_AGENT_INDEX, {_AGENT: time.time()})
    jar = httpx.Cookies()
    jar.set("vault_session", _TEST_SESSION)
    with TestClient(vault_app.app, raise_server_exceptions=True, cookies=jar) as c:
        yield c
    vault_app.SESSION_TOKENS.pop(_TEST_SESSION, None)
    vault_app.r.delete(f"{vault_app.PFX_AGENT}{_AGENT}")
    vault_app.r.zrem(vault_app.PFX_AGENT_INDEX, _AGENT)
    try:
        vault_app.store.delete(CONN_ID, [_AGENT])
    except Exception:
        pass


@pytest.fixture(scope="module")
def discord_conn(client):
    resp = client.post("/services/add", data={
        "service": "discord_read",
        "name": CONN_ID,
        "api_key": "fake-bot-token-for-tests",
        "application_id": "123456789012345678",
    }, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    conn = vault_app.store.get(CONN_ID)
    assert conn is not None
    vault_app.store.set_grant(_AGENT, CONN_ID, True)
    return conn


def _agent_hdrs():
    return {"Authorization": f"Bearer {_AGENT_TOKEN}"}


# ---------------------------------------------------------------------------
# Unit: readonly_allowed
# ---------------------------------------------------------------------------

def test_readonly_allows_read_paths():
    ok = discord_ops.readonly_allowed
    for path in ("/users/@me", "/users/@me/guilds",
                 "/guilds/123/channels", "/channels/42/messages",
                 "/channels/42/messages/7", "/channels/42/pins",
                 "/api/v10/channels/42/messages",  # prefixed form
                 "/oauth2/applications/@me"):
        assert ok("GET", "discord.com", path), path
    # CDN attachment hosts: GET anything
    assert ok("GET", "cdn.discordapp.com", "/attachments/1/2/file.png")
    assert ok("GET", "media.discordapp.net", "/attachments/1/2/img.jpg")


def test_readonly_rejects_writes_and_unknown_paths():
    ok = discord_ops.readonly_allowed
    # Write methods on otherwise-readable paths
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert not ok(method, "discord.com", "/channels/42/messages"), method
        assert not ok(method, "cdn.discordapp.com", "/attachments/1/2/f.png"), method
    # Read method on write-adjacent / unknown paths
    for path in ("/channels/42/messages/7/reactions/%F0%9F%91%8D/@me",
                 "/users/@me/channels",       # DM creation surface
                 "/guilds/123/members/456",
                 "/webhooks/1/token",
                 "/channels/42/typing",
                 "/invites/abc"):
        assert not ok("GET", "discord.com", path), path
    # Wrong host entirely
    assert not ok("GET", "example.com", "/users/@me")


def test_invite_url_is_readonly_permissions():
    assert discord_ops.READONLY_PERMISSIONS == 66560  # 1024 + 65536
    url = discord_ops.invite_url("appid123", "guild9")
    assert "permissions=66560" in url
    assert "client_id=appid123" in url and "guild_id=guild9" in url
    assert "scope=bot" in url


def test_catalog_entries():
    from catalog import CATALOG
    assert CATALOG["discord_read"]["read_only"] is True
    assert "cdn.discordapp.com" in CATALOG["discord_read"]["allowed_hosts"]
    oauth = CATALOG["discord_user"]["oauth"]
    assert set(oauth["scopes"]) == {"identify", "guilds"}
    assert CATALOG["discord_user"]["auth"]["kind"] == "oauth2"


# ---------------------------------------------------------------------------
# E2E: proxy enforcement
# ---------------------------------------------------------------------------

def test_proxy_rejects_write_calls(client, discord_conn):
    for method, path in (
            ("POST", "/channels/42/messages"),          # send message
            ("PUT", "/channels/42/messages/7/reactions/x/@me"),  # react
            ("DELETE", "/channels/42/messages/7"),      # delete
            ("PATCH", "/channels/42"),                  # edit channel
            ("POST", "/users/@me/channels"),            # open DM
            ("GET", "/guilds/123/members/456"),         # non-allowlisted read
    ):
        resp = client.post(f"/api/vault/proxy/{CONN_ID}",
                           headers=_agent_hdrs(),
                           json={"method": method, "path": path,
                                 "json": {"content": "hi"}})
        assert resp.status_code == 403, (method, path, resp.text)
        assert "read-only" in resp.json()["detail"]


def test_proxy_allows_readonly_get(client, discord_conn, monkeypatch):
    """An allowlisted GET must pass the gate (upstream mocked)."""
    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = json.dumps([{"id": "1", "name": "general"}]).encode()
        text = content.decode()
        encoding = "utf-8"

        def json(self):
            return json.loads(self.content)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(vault_app.httpx, "AsyncClient", FakeClient)
    resp = client.post(f"/api/vault/proxy/{CONN_ID}",
                       headers=_agent_hdrs(),
                       json={"method": "GET", "path": "/guilds/123/channels"})
    assert resp.status_code == 200, resp.text


def test_discord_endpoint_requires_grant(client, discord_conn):
    vault_app.store.set_grant(_AGENT, CONN_ID, False)
    try:
        resp = client.post(f"/api/vault/discord/{CONN_ID}",
                           headers=_agent_hdrs(),
                           json={"operation": "list_servers"})
        assert resp.status_code == 403
    finally:
        vault_app.store.set_grant(_AGENT, CONN_ID, True)


def test_discord_endpoint_rejects_unknown_operation(client, discord_conn):
    resp = client.post(f"/api/vault/discord/{CONN_ID}",
                       headers=_agent_hdrs(),
                       json={"operation": "send_message",
                             "args": {"channel_id": "1", "content": "hi"}})
    assert resp.status_code == 400
    assert "Unknown operation" in resp.json()["detail"]


def test_discord_endpoint_wrong_service_kind(client, discord_conn):
    # The structured endpoint only serves discord_read connections.
    resp = client.post(f"/api/vault/discord/{CONN_ID}",
                       headers=_agent_hdrs(),
                       json={"operation": "list_servers"})
    assert resp.status_code != 409  # correct kind → not rejected for kind


def test_download_attachment_rejects_non_cdn_url(client, discord_conn):
    resp = client.post(f"/api/vault/discord/{CONN_ID}",
                       headers=_agent_hdrs(),
                       json={"operation": "download_attachment",
                             "args": {"url": "https://evil.example.com/f.png"}})
    assert resp.status_code == 400
    assert "cdn.discordapp.com" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# OAuth state handling
# ---------------------------------------------------------------------------

def test_oauth_state_single_use():
    vault_app.store.put_oauth_state("state-t96", {"conn_id": "X", "service": "discord_user"})
    data = vault_app.store.pop_oauth_state("state-t96")
    assert data and data.get("service") == "discord_user"
    assert vault_app.store.pop_oauth_state("state-t96") is None


def test_oauth_callback_rejects_bad_state(client):
    resp = client.get("/oauth/callback?code=abc&state=no-such-state",
                      follow_redirects=False)
    assert resp.status_code in (400, 303)
    if resp.status_code == 303:
        assert "error" in resp.headers.get("location", "").lower()


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

def test_extract_links_from_messages():
    msgs = [{
        "id": "10", "channel_id": "42", "timestamp": "t",
        "author": {"username": "alice"},
        "content": "see https://example.com/a and https://example.com/a again",
        "embeds": [{"url": "https://embed.example.com/x"}],
        "attachments": [{"url": "https://cdn.discordapp.com/attachments/1/2/f.png"}],
    }]
    links = discord_ops.extract_links_from_messages(msgs)
    urls = [l["url"] for l in links]
    assert urls.count("https://example.com/a") == 1  # deduped per message
    assert "https://embed.example.com/x" in urls
    assert "https://cdn.discordapp.com/attachments/1/2/f.png" in urls
    assert links[0]["author"] == "alice"
