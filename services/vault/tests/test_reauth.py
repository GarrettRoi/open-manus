"""Tests for the agent-facing OAuth re-auth link flow (dev request #40).

Covers POST /api/vault/reauth/{conn_id} (grant-gating, kind/config checks,
consent-URL issuance) and the structured oauth_reauth_required detail helper.
"""
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("VAULT_ADMIN_PASSWORD", "reauth-test-pass")
os.environ.setdefault("VAULT_MASTER_KEY", "")

import app as vault_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TEST_SESSION = "reauth-test-session"
_CONN_ID = "TEST_REAUTH_GOOGLE"
_AGENT = "samantha"


@pytest.fixture(scope="module")
def client():
    vault_app.SESSION_TOKENS[_TEST_SESSION] = time.time() + 86400
    jar = httpx.Cookies()
    jar.set("vault_session", _TEST_SESSION)
    with TestClient(vault_app.app, raise_server_exceptions=True, cookies=jar) as c:
        yield c
    vault_app.SESSION_TOKENS.pop(_TEST_SESSION, None)
    for cid in (_CONN_ID, "TEST_REAUTH_NOTOAUTH", "TEST_REAUTH_NOCLIENT"):
        vault_app.r.delete(f"vault:conn:{cid}")
        vault_app.r.zrem("vault:conns", cid)
        vault_app.r.delete(f"vault:grant:{_AGENT}:{cid}")


def _issue_agent_token() -> str:
    import secrets as _s
    token = _s.token_urlsafe(24)
    vault_app.r.hset(f"{vault_app.PFX_AGENT}{_AGENT}", mapping={
        "token_hash": vault_app.hash_token(token),
        "token_plain": token,
    })
    vault_app.r.zadd(vault_app.PFX_AGENT_INDEX, {_AGENT: 0})
    return token


def _save_oauth_conn(cid=_CONN_ID, secrets=None):
    vault_app.store.save(
        cid, service="google", label="Test Google",
        base_url="https://www.googleapis.com",
        auth={"kind": "oauth2"},
        secrets=secrets if secrets is not None else {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-client-secret",
        },
        status="ready")


def _post_reauth(client, token, cid=_CONN_ID):
    return client.post(f"/api/vault/reauth/{cid}",
                       headers={"Authorization": f"Bearer {token}"})


def test_reauth_requires_grant(client, monkeypatch):
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "https://vault.example.com")
    _save_oauth_conn()
    vault_app.store.set_grant(_AGENT, _CONN_ID, False)
    assert _post_reauth(client, _issue_agent_token()).status_code == 403


def test_reauth_returns_consent_url(client, monkeypatch):
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "https://vault.example.com")
    _save_oauth_conn()
    vault_app.store.set_grant(_AGENT, _CONN_ID, True)
    resp = _post_reauth(client, _issue_agent_token())
    assert resp.status_code == 200
    data = resp.json()
    assert data["connection"] == _CONN_ID
    assert data["url"].startswith("https://accounts.google.com/")
    assert "state=" in data["url"]
    assert "client_id=test-client-id" in data["url"]
    # secret must never be in the URL
    assert "test-client-secret" not in data["url"]
    # state must be stored server-side (single-use, TTL)
    state = data["url"].split("state=")[1].split("&")[0]
    assert vault_app.store.pop_oauth_state(state)["conn_id"] == _CONN_ID


def test_reauth_rejects_non_oauth_connection(client, monkeypatch):
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "https://vault.example.com")
    cid = "TEST_REAUTH_NOTOAUTH"
    vault_app.store.save(cid, service="custom", label="x",
                         base_url="https://api.example.com",
                         auth={"kind": "header", "header_name": "Authorization",
                               "prefix": "Bearer "},
                         secrets={"api_key": "k"}, status="ready")
    vault_app.store.set_grant(_AGENT, cid, True)
    assert _post_reauth(client, _issue_agent_token(), cid).status_code == 409


def test_reauth_requires_client_id(client, monkeypatch):
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "https://vault.example.com")
    cid = "TEST_REAUTH_NOCLIENT"
    vault_app.store.save(cid, service="google", label="x",
                         base_url="https://www.googleapis.com",
                         auth={"kind": "oauth2"}, secrets={}, status="ready")
    vault_app.store.set_grant(_AGENT, cid, True)
    assert _post_reauth(client, _issue_agent_token(), cid).status_code == 409


def test_reauth_requires_public_url(client, monkeypatch):
    monkeypatch.setattr(vault_app, "PUBLIC_URL", "")
    _save_oauth_conn()
    vault_app.store.set_grant(_AGENT, _CONN_ID, True)
    assert _post_reauth(client, _issue_agent_token()).status_code == 503


def test_oauth_public_reason_never_forwards_provider_text():
    # Raw provider bodies (which can reflect secrets) must never reach agents.
    raw = ("Token refresh failed (400): {\"error\":\"invalid_grant\","
           "\"echo_secret\":\"SUPER-SECRET-REFRESH-TOKEN\"}")
    reason = vault_app._oauth_public_reason(raw)
    assert "SUPER-SECRET" not in reason
    assert "refresh" in reason.lower()
    assert "No refresh token" in vault_app._oauth_public_reason(
        "No refresh token stored — reconnect this service")


def test_scrub_secret_values():
    secrets_d = {"client_secret": "abcd1234efgh5678", "client_id": "pub"}
    out = vault_app._scrub_secret_values(
        "provider said: abcd1234efgh5678 invalid", secrets_d)
    assert "abcd1234efgh5678" not in out
    assert "***vault***" in out


def test_oauth_state_is_single_use(client):
    vault_app.store.put_oauth_state("teststate123", {"conn_id": _CONN_ID,
                                                     "service": "google"})
    first = vault_app.store.pop_oauth_state("teststate123")
    second = vault_app.store.pop_oauth_state("teststate123")
    assert first and first["conn_id"] == _CONN_ID
    assert second is None


def test_reauth_detail_helper_flags_connection(client):
    _save_oauth_conn()
    detail = vault_app._reauth_required_detail(_CONN_ID, "token expired")
    assert detail["error"] == "oauth_reauth_required"
    assert detail["connection"] == _CONN_ID
    assert "reauth" in detail["action"]
    assert vault_app.r.hget(f"vault:conn:{_CONN_ID}", "status") == "needs_reauth"
