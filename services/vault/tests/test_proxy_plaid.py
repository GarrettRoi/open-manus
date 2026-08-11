"""Plaid proxy body-credential injection.

Plaid authenticates with client_id/secret in the JSON request body, not
headers.  The proxy must inject the stored credentials into the outgoing
body for *.plaid.com hosts (and scrub them from any reflected response).

Run from services/vault/:
    pytest tests/test_proxy_plaid.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("VAULT_ADMIN_PASSWORD", "e2e-test-pass")
os.environ.setdefault("VAULT_MASTER_KEY", "")

import app as vault_app  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

CONN_ID = "e2e_plaid_proxy"
CLIENT_ID = "plaidclient1234567890"
SECRET = "plaidsecretvalue1234567890"


class _FakeUpstream:
    status_code = 200
    headers = {"content-type": "application/json"}
    encoding = "utf-8"
    content = json.dumps({"accounts": []}).encode()


class _FakeAsyncClient:
    """Captures the outgoing request instead of hitting the network."""
    captured = {}

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kwargs):
        _FakeAsyncClient.captured = {"method": method, "url": url, **kwargs}
        return _FakeUpstream()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(vault_app, "require_agent", lambda request: "samantha")
    monkeypatch.setattr(vault_app.store, "has_grant", lambda agent, cid: True)
    monkeypatch.setattr(vault_app.httpx, "AsyncClient", _FakeAsyncClient)
    vault_app.store.save(
        CONN_ID,
        service="plaid",
        label="Plaid test",
        base_url="https://production.plaid.com",
        auth={"kind": "header", "header_name": "PLAID-CLIENT-ID", "prefix": ""},
        secrets={"api_key": CLIENT_ID, "extra_headers": {"PLAID-SECRET": SECRET}},
    )
    with TestClient(vault_app.app) as c:
        yield c
    vault_app.store.delete(CONN_ID, [])


def _proxy(client, body):
    return client.post(f"/api/vault/proxy/{CONN_ID}", json=body)


def test_plaid_body_injection(client):
    r = _proxy(client, {
        "method": "POST",
        "path": "/accounts/balance/get",
        "json": {"access_token": "access-sandbox-123"},
    })
    assert r.status_code == 200, r.text
    sent = _FakeAsyncClient.captured
    assert sent["url"].endswith("/accounts/balance/get")
    jbody = sent["json"]
    assert jbody["client_id"] == CLIENT_ID
    assert jbody["secret"] == SECRET
    assert jbody["access_token"] == "access-sandbox-123"
    # Header injection still present (harmless, Plaid also accepts headers).
    assert sent["headers"].get("PLAID-CLIENT-ID") == CLIENT_ID


def test_plaid_body_injection_no_body(client):
    """POST with no json body at all still gets credentials injected."""
    r = _proxy(client, {"method": "POST", "path": "/accounts/get"})
    assert r.status_code == 200, r.text
    jbody = _FakeAsyncClient.captured["json"]
    assert jbody == {"client_id": CLIENT_ID, "secret": SECRET}


def test_plaid_agent_supplied_creds_overridden(client):
    """Vault-stored credentials win over anything the agent passes."""
    r = _proxy(client, {
        "method": "POST",
        "path": "/accounts/get",
        "json": {"client_id": "bogus", "secret": "bogus"},
    })
    assert r.status_code == 200, r.text
    jbody = _FakeAsyncClient.captured["json"]
    assert jbody["client_id"] == CLIENT_ID
    assert jbody["secret"] == SECRET


def test_non_plaid_host_untouched(client, monkeypatch):
    """Body injection must not fire for other hosts."""
    vault_app.store.save(
        "e2e_notplaid_proxy",
        service="custom",
        label="Other",
        base_url="https://api.example.com",
        auth={"kind": "header", "header_name": "X-Key", "prefix": ""},
        secrets={"api_key": "k1234567890"},
    )
    try:
        r = client.post("/api/vault/proxy/e2e_notplaid_proxy", json={
            "method": "POST", "path": "/v1/x", "json": {"a": 1},
        })
        assert r.status_code == 200, r.text
        assert _FakeAsyncClient.captured["json"] == {"a": 1}
    finally:
        vault_app.store.delete("e2e_notplaid_proxy", [])
