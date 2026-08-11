"""Plaid Item lifecycle: link-token creation, public_token exchange,
update-mode re-auth, and removal — plus fail-closed behavior when the base
Plaid credentials are missing.

Run from services/vault/:
    pytest tests/test_plaid_items.py -v
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

CONN_ID = "E2E_PLAID_ITEMS"
CLIENT_ID = "plaidclient1234567890"
SECRET = "plaidsecretvalue1234567890"
ADMIN_HDR = {"X-Vault-Admin-Token": "e2e-test-pass"}


class _FakePlaidResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakePlaidClient:
    """Captures the outgoing Plaid API call; response set per-test."""
    captured = {}
    response = _FakePlaidResponse(200, {})

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        _FakePlaidClient.captured = {"url": url, **kwargs}
        return _FakePlaidClient.response

    async def request(self, method, url, **kwargs):
        _FakePlaidClient.captured = {"method": method, "url": url, **kwargs}
        return _FakePlaidClient.response


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(vault_app.httpx, "AsyncClient", _FakePlaidClient)
    vault_app.store.save(
        CONN_ID,
        service="plaid",
        label="Plaid test",
        base_url="https://sandbox.plaid.com",
        auth={"kind": "header", "header_name": "PLAID-CLIENT-ID", "prefix": ""},
        secrets={"api_key": CLIENT_ID, "extra_headers": {"PLAID-SECRET": SECRET}},
    )
    _FakePlaidClient.captured = {}
    with TestClient(vault_app.app) as c:
        yield c
    vault_app.item_store.delete_all(CONN_ID)
    vault_app.store.delete(CONN_ID, [])


def test_link_token_created(client):
    _FakePlaidClient.response = _FakePlaidResponse(200, {"link_token": "link-tok-1"})
    r = client.post(f"/api/admin/plaid/{CONN_ID}/link-token", json={},
                    headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    assert r.json() == {"link_token": "link-tok-1"}
    sent = _FakePlaidClient.captured
    assert sent["url"].endswith("/link/token/create")
    body = sent["json"]
    assert body["client_id"] == CLIENT_ID
    assert body["secret"] == SECRET
    assert body["products"] == ["transactions"]
    assert "access_token" not in body


def test_link_token_requires_admin(client):
    r = client.post(f"/api/admin/plaid/{CONN_ID}/link-token", json={})
    assert r.status_code == 401


def test_link_token_missing_creds_fails(client):
    vault_app.store.set_secrets(CONN_ID, {"api_key": CLIENT_ID})  # no secret
    _FakePlaidClient.captured = {}
    r = client.post(f"/api/admin/plaid/{CONN_ID}/link-token", json={},
                    headers=ADMIN_HDR)
    assert r.status_code == 409, r.text
    assert "client_id or secret" in r.text
    assert _FakePlaidClient.captured == {}, "must not call Plaid"


def test_exchange_persists_item_without_leaking_token(client):
    _FakePlaidClient.response = _FakePlaidResponse(
        200, {"access_token": "access-sandbox-tok-9", "item_id": "item-9"})
    r = client.post(
        f"/api/admin/plaid/{CONN_ID}/exchange",
        json={"public_token": "public-tok", "institution_name": "Discover",
              "institution_id": "ins_33"},
        headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    item = data["item"]
    assert item["institution_name"] == "Discover"
    assert item["item_id"] == "item-9"
    assert item["status"] == "active"
    # The access token must never appear in the response.
    assert "access-sandbox-tok-9" not in r.text
    # ...but is stored (encrypted) and retrievable server-side.
    assert vault_app.item_store.get_access_token(CONN_ID, item["key"]) == \
        "access-sandbox-tok-9"
    # Stored encrypted, not plaintext.
    raw = vault_app.r.hget(f"vault:plaid_item:{CONN_ID}:{item['key']}", "access_enc")
    assert raw and "access-sandbox-tok-9" not in raw


def test_exchange_missing_public_token(client):
    r = client.post(f"/api/admin/plaid/{CONN_ID}/exchange", json={},
                    headers=ADMIN_HDR)
    assert r.status_code == 400


def test_update_mode_link_token_uses_stored_item_token(client):
    key = vault_app.item_store.save(
        CONN_ID, institution_name="Capital One", institution_id="ins_9",
        item_id="item-co", access_token="access-co-token")
    _FakePlaidClient.response = _FakePlaidResponse(200, {"link_token": "link-upd"})
    r = client.post(f"/api/admin/plaid/{CONN_ID}/link-token",
                    json={"item_key": key}, headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    body = _FakePlaidClient.captured["json"]
    assert body["access_token"] == "access-co-token"
    assert "products" not in body  # update mode: no products


def test_update_mode_unknown_item_404(client):
    r = client.post(f"/api/admin/plaid/{CONN_ID}/link-token",
                    json={"item_key": "NOPE"}, headers=ADMIN_HDR)
    assert r.status_code == 404


def test_reauth_done_marks_active(client):
    key = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-d", access_token="tok", status="login_required")
    r = client.post(f"/api/admin/plaid/{CONN_ID}/items/{key}/reauth-done",
                    headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    assert vault_app.item_store.get(CONN_ID, key)["status"] == "active"


def test_remove_calls_plaid_and_deletes(client):
    key = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-d", access_token="access-remove-me")
    _FakePlaidClient.response = _FakePlaidResponse(200, {"removed": True})
    r = client.post(f"/api/admin/plaid/{CONN_ID}/items/{key}/remove",
                    headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    sent = _FakePlaidClient.captured
    assert sent["url"].endswith("/item/remove")
    assert sent["json"]["access_token"] == "access-remove-me"
    assert vault_app.item_store.get(CONN_ID, key) is None
    assert "access-remove-me" not in r.text


def test_remove_deletes_locally_even_if_plaid_fails(client):
    key = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-d2", access_token="tok-x-12345678")
    _FakePlaidClient.response = _FakePlaidResponse(
        400, {"error_code": "ITEM_NOT_FOUND", "error_message": "gone"})
    r = client.post(f"/api/admin/plaid/{CONN_ID}/items/{key}/remove",
                    headers=ADMIN_HDR)
    assert r.status_code == 200, r.text
    assert r.json().get("warning")
    assert vault_app.item_store.get(CONN_ID, key) is None


def test_same_item_id_updates_in_place(client):
    k1 = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-same", access_token="tok-1")
    k2 = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-same", access_token="tok-2")
    assert k1 == k2
    assert len(vault_app.item_store.list_items(CONN_ID)) == 1
    assert vault_app.item_store.get_access_token(CONN_ID, k1) == "tok-2"


def test_two_institutions_side_by_side(client):
    k1 = vault_app.item_store.save(
        CONN_ID, institution_name="Discover", institution_id="ins_33",
        item_id="item-a", access_token="tok-a")
    k2 = vault_app.item_store.save(
        CONN_ID, institution_name="Capital One", institution_id="ins_9",
        item_id="item-b", access_token="tok-b")
    assert k1 != k2
    assert len(vault_app.item_store.list_items(CONN_ID)) == 2
    vault_app.item_store.delete(CONN_ID, k1)
    items = vault_app.item_store.list_items(CONN_ID)
    assert [i["key"] for i in items] == [k2]


def test_non_plaid_connection_rejected(client):
    vault_app.store.save(
        "E2E_NOTPLAID_ITEMS", service="custom", label="Other",
        base_url="https://api.example.com",
        auth={"kind": "header", "header_name": "X-Key", "prefix": ""},
        secrets={"api_key": "k"})
    try:
        r = client.post("/api/admin/plaid/E2E_NOTPLAID_ITEMS/link-token",
                        json={}, headers=ADMIN_HDR)
        assert r.status_code == 400
    finally:
        vault_app.store.delete("E2E_NOTPLAID_ITEMS", [])
