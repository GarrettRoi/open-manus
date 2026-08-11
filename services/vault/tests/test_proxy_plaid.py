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


def test_plaid_get_not_injected(client):
    """GET requests carry no body; nothing to inject."""
    r = _proxy(client, {"method": "GET", "path": "/health"})
    assert r.status_code == 200, r.text
    assert "json" not in _FakeAsyncClient.captured or \
        _FakeAsyncClient.captured.get("json") is None


def test_plaid_non_dict_json_rejected(client):
    r = _proxy(client, {"method": "POST", "path": "/accounts/get", "json": [1, 2]})
    assert r.status_code == 400
    assert "JSON object" in r.text


def test_plaid_missing_secret_fails_closed(client):
    """Missing stored secret must 409, never forward agent-supplied creds."""
    vault_app.store.save(
        CONN_ID,
        service="plaid",
        label="Plaid test",
        base_url="https://production.plaid.com",
        auth={"kind": "header", "header_name": "PLAID-CLIENT-ID", "prefix": ""},
        secrets={"api_key": CLIENT_ID},  # no PLAID-SECRET
    )
    _FakeAsyncClient.captured = {}
    r = _proxy(client, {
        "method": "POST", "path": "/accounts/get",
        "json": {"client_id": "agent-supplied", "secret": "agent-supplied"},
    })
    assert r.status_code == 409, r.text
    assert _FakeAsyncClient.captured == {}, "request must not reach upstream"


def test_evil_host_not_matched(client):
    """evilplaid.com is neither injected into nor allowed at all."""
    r = _proxy(client, {"method": "POST", "path": "https://evilplaid.com/x",
                        "json": {}})
    assert r.status_code == 403  # host allowlist blocks it outright


# ── Stored-Item access-token injection ─────────────────────────────────────

ITEM_TOKEN = "access-production-item-token-abc123"


@pytest.fixture()
def item(client):
    key = vault_app.item_store.save(
        CONN_ID,
        institution_name="Discover Bank",
        institution_id="ins_33",
        item_id="item-xyz-1",
        access_token=ITEM_TOKEN,
    )
    yield key
    vault_app.item_store.delete_all(CONN_ID)


def test_vault_item_token_injected(client, item):
    r = _proxy(client, {
        "method": "POST", "path": "/accounts/balance/get",
        "json": {"vault_item": item},
    })
    assert r.status_code == 200, r.text
    jbody = _FakeAsyncClient.captured["json"]
    assert jbody["access_token"] == ITEM_TOKEN
    assert "vault_item" not in jbody
    assert jbody["client_id"] == CLIENT_ID


def test_vault_item_by_institution_name(client, item):
    r = _proxy(client, {
        "method": "POST", "path": "/accounts/get",
        "json": {"vault_item": "discover"},
    })
    assert r.status_code == 200, r.text
    assert _FakeAsyncClient.captured["json"]["access_token"] == ITEM_TOKEN


def test_vault_item_overrides_agent_token(client, item):
    """Agent-supplied access_token must lose to the stored Item token."""
    r = _proxy(client, {
        "method": "POST", "path": "/accounts/get",
        "json": {"vault_item": item, "access_token": "agent-evil-token"},
    })
    assert r.status_code == 200, r.text
    assert _FakeAsyncClient.captured["json"]["access_token"] == ITEM_TOKEN


def test_vault_item_unknown_fails_closed(client, item):
    _FakeAsyncClient.captured = {}
    r = _proxy(client, {
        "method": "POST", "path": "/accounts/get",
        "json": {"vault_item": "NOSUCHBANK"},
    })
    assert r.status_code == 409, r.text
    assert _FakeAsyncClient.captured == {}, "request must not reach upstream"
    assert ITEM_TOKEN not in r.text


def test_item_token_scrubbed_from_response(client, item):
    """Plaid echoes access tokens in some responses — they must be scrubbed."""
    class _EchoUpstream:
        status_code = 200
        headers = {"content-type": "application/json"}
        encoding = "utf-8"
        content = json.dumps({"access_token": ITEM_TOKEN, "ok": True}).encode()

    orig = _FakeAsyncClient.request

    async def echo_request(self, method, url, **kwargs):
        _FakeAsyncClient.captured = {"method": method, "url": url, **kwargs}
        return _EchoUpstream()

    _FakeAsyncClient.request = echo_request
    try:
        r = _proxy(client, {"method": "POST", "path": "/item/get",
                            "json": {"vault_item": item}})
    finally:
        _FakeAsyncClient.request = orig
    assert r.status_code == 200, r.text
    assert ITEM_TOKEN not in r.text
    assert "***vault***" in r.text


def test_item_token_scrubbed_even_without_vault_item(client, item):
    """All stored Item tokens are scrubbed, not just the one injected."""
    class _EchoUpstream:
        status_code = 200
        headers = {"content-type": "application/json"}
        encoding = "utf-8"
        content = json.dumps({"leak": ITEM_TOKEN}).encode()

    orig = _FakeAsyncClient.request

    async def echo_request(self, method, url, **kwargs):
        _FakeAsyncClient.captured = {"method": method, "url": url, **kwargs}
        return _EchoUpstream()

    _FakeAsyncClient.request = echo_request
    try:
        r = _proxy(client, {"method": "POST", "path": "/institutions/get",
                            "json": {"count": 1}})
    finally:
        _FakeAsyncClient.request = orig
    assert r.status_code == 200, r.text
    assert ITEM_TOKEN not in r.text


def test_vault_item_with_missing_base_creds_fails_closed(client, item):
    """Even with a valid Item, missing client_id/secret must 409."""
    vault_app.store.save(
        CONN_ID,
        service="plaid",
        label="Plaid test",
        base_url="https://production.plaid.com",
        auth={"kind": "header", "header_name": "PLAID-CLIENT-ID", "prefix": ""},
        secrets={"api_key": CLIENT_ID},  # no PLAID-SECRET
    )
    _FakeAsyncClient.captured = {}
    r = _proxy(client, {"method": "POST", "path": "/accounts/get",
                        "json": {"vault_item": item}})
    assert r.status_code == 409, r.text
    assert _FakeAsyncClient.captured == {}
    assert ITEM_TOKEN not in r.text


def test_binary_response_scrubbed(client):
    """Credentials reflected in binary bodies must be scrubbed pre-base64."""
    import base64

    class _BinUpstream:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}
        encoding = "utf-8"
        content = b"prefix" + SECRET.encode() + b"mid" + CLIENT_ID.encode() + b"suffix"

    orig = _FakeAsyncClient.request

    async def bin_request(self, method, url, **kwargs):
        _FakeAsyncClient.captured = {"method": method, "url": url, **kwargs}
        return _BinUpstream()

    _FakeAsyncClient.request = bin_request
    try:
        r = _proxy(client, {"method": "POST", "path": "/accounts/get", "json": {}})
    finally:
        _FakeAsyncClient.request = orig
    assert r.status_code == 200, r.text
    decoded = base64.b64decode(r.json()["body_base64"])
    assert SECRET.encode() not in decoded
    assert CLIENT_ID.encode() not in decoded
    assert b"***vault***" in decoded
