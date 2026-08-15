"""End-to-end tests for the browser-login credential type.

Exercises add_service / update_service for the new ``browser`` auth kind and
the grant-gated ``/api/vault/browser/{id}/credentials`` handoff endpoint via
Starlette's synchronous TestClient (real Redis/fakeredis + real Fernet).

Run from services/vault/:
    pytest tests/test_browser_login.py -v
"""
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("VAULT_ADMIN_PASSWORD", "browser-test-pass")
os.environ.setdefault("VAULT_MASTER_KEY", "")

import app as vault_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

_TEST_SESSION = "browser-login-test-session"
_CONN_ID = "TEST_BROWSER_LOGIN"
_AGENT = "sabrina"


@pytest.fixture(scope="module")
def client():
    vault_app.SESSION_TOKENS[_TEST_SESSION] = time.time() + 86400
    jar = httpx.Cookies()
    jar.set("vault_session", _TEST_SESSION)
    with TestClient(vault_app.app, raise_server_exceptions=True, cookies=jar) as c:
        yield c
    vault_app.SESSION_TOKENS.pop(_TEST_SESSION, None)
    # cleanup
    vault_app.r.delete(f"vault:conn:{_CONN_ID}")
    vault_app.r.zrem("vault:conns", _CONN_ID)
    vault_app.r.delete(f"vault:grant:{_AGENT}:{_CONN_ID}")


def _add_browser_conn(client, **overrides):
    form = {
        "service": "browser_login",
        "name": _CONN_ID,
        "login_url": "https://www.example.com/login",
        "browser_username": "alice@example.com",
        "browser_password": "s3cr3t-pass",
    }
    form.update(overrides)
    return client.post("/services/add", data=form, follow_redirects=False)


def test_add_browser_connection(client):
    resp = _add_browser_conn(client)
    assert resp.status_code == 303
    assert "error" not in resp.headers.get("location", "")
    conn = vault_app.store.get(_CONN_ID)
    assert conn is not None
    assert conn["auth"]["kind"] == "browser"
    secrets = vault_app.store.get_secrets(_CONN_ID)
    assert secrets["username"] == "alice@example.com"
    assert secrets["password"] == "s3cr3t-pass"
    assert secrets["login_url"] == "https://www.example.com/login"


def test_add_requires_username_and_password(client):
    resp = _add_browser_conn(client, name="TMP_BROWSER_MISSING",
                             browser_password="")
    assert resp.status_code == 303
    assert "error" in resp.headers.get("location", "")
    assert vault_app.store.get("TMP_BROWSER_MISSING") is None


def test_add_requires_valid_login_url(client):
    resp = _add_browser_conn(client, name="TMP_BROWSER_BADURL",
                             login_url="not-a-url")
    assert resp.status_code == 303
    assert "error" in resp.headers.get("location", "")
    assert vault_app.store.get("TMP_BROWSER_BADURL") is None


def test_conn_view_hides_password(client):
    _add_browser_conn(client)
    view = vault_app._conn_view(vault_app.store.get(_CONN_ID))
    assert view["browser_username"] == "alice@example.com"
    assert view["login_url"] == "https://www.example.com/login"
    # Password must never appear anywhere in the public view.
    assert "s3cr3t-pass" not in str(view)
    assert "password" not in view


def test_credentials_endpoint_requires_grant(client):
    _add_browser_conn(client)
    # Ensure no grant.
    vault_app.store.set_grant(_AGENT, _CONN_ID, False)
    token = _issue_agent_token()
    resp = client.post(f"/api/vault/browser/{_CONN_ID}/credentials",
                       json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_credentials_endpoint_returns_creds_with_grant(client):
    _add_browser_conn(client)
    vault_app.store.set_grant(_AGENT, _CONN_ID, True)
    token = _issue_agent_token()
    resp = client.post(f"/api/vault/browser/{_CONN_ID}/credentials",
                       json={}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "alice@example.com"
    assert data["password"] == "s3cr3t-pass"
    assert data["login_url"] == "https://www.example.com/login"


def test_credentials_endpoint_rejects_non_browser_conn(client):
    # Create a plain custom (header) connection, then hit the browser endpoint.
    cid = "TEST_NOT_BROWSER"
    vault_app.store.save(cid, service="custom", label="x",
                         base_url="https://api.example.com",
                         auth={"kind": "header", "header_name": "Authorization",
                               "prefix": "Bearer "},
                         secrets={"api_key": "k"}, status="ready")
    vault_app.store.set_grant(_AGENT, cid, True)
    try:
        token = _issue_agent_token()
        resp = client.post(f"/api/vault/browser/{cid}/credentials",
                           json={}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 409
    finally:
        vault_app.r.delete(f"vault:conn:{cid}")
        vault_app.r.zrem("vault:conns", cid)
        vault_app.r.delete(f"vault:grant:{_AGENT}:{cid}")


def _issue_agent_token() -> str:
    """Register a known agent token the same way init_agents does:
    store its hash on the agent record and index the agent name.
    """
    import secrets as _s
    token = _s.token_urlsafe(24)
    vault_app.r.hset(f"{vault_app.PFX_AGENT}{_AGENT}", mapping={
        "token_hash": vault_app.hash_token(token),
        "token_plain": token,
    })
    vault_app.r.zadd(vault_app.PFX_AGENT_INDEX, {_AGENT: 0})
    return token
