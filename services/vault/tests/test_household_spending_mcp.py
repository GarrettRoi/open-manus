"""Household preset contract: real local apps, fake Redis and mock HTTP only.

Run with --confcutdir=services/vault/tests so the hermetic vault bootstrap
replaces Redis constructors before importing the vault application.
"""
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as vault_app  # noqa: E402
from catalog import get_template  # noqa: E402
from connections import ConnectionStore  # noqa: E402
from services.household.app import Config, create_app  # noqa: E402


MCP_URL = "https://household.test/mcp"  # test transport only; no deployed host
CONNECTION = "TEST_HOUSEHOLD"
SELECTED = "samantha"
OTHER = "harmony"
ADMIN_HEADERS = {"X-Vault-Admin-Token": "isolated-test-admin"}
AGENT_TOKENS = {
    SELECTED: "isolated-test-selected-vault-identity",
    OTHER: "isolated-test-other-vault-identity",
}
SUMMARY_TOOLS = {"household_summary", "household_snapshot", "household_items"}
PURCHASE_TOOLS = {"household_purchases", "household_purchase", "household_people", "household_audit"}


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("External networking forbidden in Household Vault tests")

    def test_dns(host, *args, **kwargs):
        assert host == "household.test"
        # Exercise the real HTTPS/public-IP guard without making a DNS request.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", test_dns)


@pytest.fixture
def env(tmp_path, monkeypatch):
    # RESP2 also keeps redis-py's RESP3 maintenance discovery from trying DNS
    # for fakeredis's synthetic host; this client has no real connection.
    cache = fakeredis.FakeRedis(decode_responses=True, protocol=2)
    monkeypatch.setattr(vault_app, "r", cache)
    monkeypatch.setattr(vault_app, "store", ConnectionStore(
        cache, vault_app.encrypt_value, vault_app.decrypt_value))
    monkeypatch.setattr(vault_app, "ADMIN_API_TOKEN", ADMIN_HEADERS["X-Vault-Admin-Token"])
    for agent, token in AGENT_TOKENS.items():
        cache.hset(f"{vault_app.PFX_AGENT}{agent}", mapping={
            "token_hash": vault_app.hash_token(token),
        })
        cache.zadd(vault_app.PFX_AGENT_INDEX, {agent: 0})

    household = create_app(Config(
        data_dir=tmp_path / "isolated-household",
        production=False,
        owner_username="owner",
        owner_password="isolated-test-owner-password",
    ))
    household_client = TestClient(household)
    login = household_client.post("/api/login", json={
        "username": "owner", "password": "isolated-test-owner-password",
    })
    assert login.status_code == 200
    owner_headers = {"X-CSRF-Token": login.json()["csrf"]}
    # No TestClient context manager: no Vault startup/background runtime runs.
    vault_client = TestClient(vault_app.app)
    seen = []
    token = None

    def transport(request):
        assert str(request.url) == MCP_URL
        assert request.headers["authorization"] == f"Bearer {token}"
        body = json.loads(request.content)
        assert all(value not in json.dumps(body) for value in AGENT_TOKENS.values())
        seen.append(body)
        response = household_client.post("/mcp", json=body, headers={
            "Authorization": request.headers["authorization"],
            "MCP-Protocol-Version": request.headers["mcp-protocol-version"],
            "Accept": request.headers["accept"],
        })
        return httpx.Response(response.status_code, content=response.content,
                              headers=response.headers)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(transport), **kwargs))

    def issue(scopes):
        nonlocal token
        response = household_client.post("/api/agent-tokens", headers=owner_headers, json={
            "name": "Isolated Vault reporting", "scopes": scopes, "expires_days": 7,
        })
        assert response.status_code == 201
        token = response.json()["token"]
        return response.json()

    yield SimpleNamespace(
        vault=vault_client, household=household_client, cache=cache,
        owner_headers=owner_headers, issue=issue, seen=seen,
    )
    vault_client.close()
    household_client.close()
    cache.close()


def connect(env, token, grant_agents=None):
    response = env.vault.post("/api/admin/connections", headers=ADMIN_HEADERS, json={
        "service": "household_spending", "name": CONNECTION,
        "base_url": MCP_URL, "bearer_token": token,
        "grant_agents": grant_agents or [],
    })
    assert response.status_code == 200, response.text
    assert token not in response.text
    assert token not in str(env.cache.hgetall(f"vault:conn:{CONNECTION}"))
    return response.json()["connection"]


def call(env, agent=SELECTED, tool="household_summary"):
    return env.vault.post(f"/api/vault/mcp/{CONNECTION}", json={
        "tool": tool, "arguments": {},
    }, headers={"Authorization": f"Bearer {AGENT_TOKENS[agent]}"})


def grant(env, granted):
    response = env.vault.post("/api/admin/grants", headers=ADMIN_HEADERS, json={
        "agent": SELECTED, "conn_id": CONNECTION, "granted": granted,
    })
    assert response.status_code == 200


def test_named_catalog_registration_and_editable_required_url(env):
    template = get_template("household_spending")
    assert template["label"] == "Household Spending"
    assert template["auth"] == {"kind": "mcp_bearer"}
    assert template["is_mcp"] is True
    assert template["base_url"] == ""
    assert template["allowed_hosts"] == []
    assert template["fields"][0]["name"] == "base_url"
    assert template["fields"][0]["required"] is True
    catalog = env.vault.get("/api/admin/overview", headers=ADMIN_HEADERS).json()["catalog"]
    assert catalog["household_spending"]["base_url"] == ""
    assert catalog["household_spending"]["fields"] == template["fields"]


@pytest.mark.parametrize("url", ["", "http://household.test/mcp"])
def test_preset_has_no_default_and_rejects_non_https(env, url):
    issued = env.issue(["summary:read"])
    response = env.vault.post("/api/admin/connections", headers=ADMIN_HEADERS, json={
        "service": "household_spending", "name": CONNECTION,
        "base_url": url, "bearer_token": issued["token"],
    })
    assert response.status_code == 400
    assert vault_app.store.get(CONNECTION) is None
    assert env.seen == []


@pytest.mark.parametrize("scopes,expected", [
    (["summary:read"], SUMMARY_TOOLS),
    (["purchases:read"], PURCHASE_TOOLS),
    (["purchases:read", "summary:read"], SUMMARY_TOOLS | PURCHASE_TOOLS),
])
def test_real_household_handshake_scoped_manifest_and_selected_grants(env, scopes, expected):
    issued = env.issue(scopes)
    connection = connect(env, issued["token"], grant_agents=[SELECTED])
    assert connection["service"] == "household_spending"
    assert connection["auth_kind"] == "mcp_bearer"
    assert connection["base_url"] == MCP_URL
    assert vault_app.store.allowed_hosts(vault_app.store.get(CONNECTION)) == ["household.test"]
    assert vault_app.store.has_grant(SELECTED, CONNECTION)
    assert all(not vault_app.store.has_grant(agent, CONNECTION)
               for agent in vault_app.AGENT_NAMES if agent != SELECTED)

    env.seen.clear()
    sync = env.vault.post(f"/api/admin/mcp-bearer/{CONNECTION}/sync-tools",
                          headers=ADMIN_HEADERS)
    assert sync.status_code == 200, sync.text
    assert {tool["name"] for tool in sync.json()["tools"]} == expected
    assert [request["method"] for request in env.seen] == [
        "initialize", "notifications/initialized", "tools/list",
    ]
    assert env.seen[0]["params"]["clientInfo"]["name"] == "open-manus-vault"
    manifest = json.loads(env.cache.get(f"vault:conn:{CONNECTION}:mcp_tools"))
    assert all(tool["annotations"]["readOnlyHint"] for tool in manifest)

    listing = env.vault.get("/api/vault/list", headers={
        "Authorization": f"Bearer {AGENT_TOKENS[SELECTED]}",
    })
    assert listing.status_code == 200
    assert issued["token"] not in listing.text
    available = listing.json()["available_connections"]
    assert len(available) == 1
    assert {tool["name"] for tool in available[0]["mcp_tools"]} == expected
    response = call(env, tool="household_summary" if "summary:read" in scopes
                    else "household_purchases")
    assert response.status_code == 200, response.text
    assert response.json().get("isError", False) is False
    assert "structuredContent" in response.json()
    assert issued["token"] not in response.text


def test_no_default_grants_scope_denial_and_both_revocation_layers(env):
    issued = env.issue(["summary:read"])
    connect(env, issued["token"])
    assert all(not vault_app.store.has_grant(agent, CONNECTION)
               for agent in vault_app.AGENT_NAMES)
    env.seen.clear()
    assert call(env).status_code == 403
    assert env.seen == []  # no upstream call before explicit owner grant

    grant(env, True)
    assert call(env).status_code == 200
    env.seen.clear()
    assert call(env, OTHER).status_code == 403
    assert env.seen == []
    denied = call(env, tool="household_purchases")
    assert denied.status_code == 502  # Household's JSON-RPC scope denial
    assert "Token lacks the required scope" in denied.json()["detail"]
    assert issued["token"] not in denied.text

    grant(env, False)
    env.seen.clear()
    assert call(env).status_code == 403
    assert env.seen == []
    grant(env, True)
    revoked = env.household.delete(f"/api/agent-tokens/{issued['id']}",
                                  headers=env.owner_headers)
    assert revoked.status_code == 200
    response = call(env)
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "mcp_token_expired"
    assert issued["token"] not in response.text
    assert [request["method"] for request in env.seen] == ["initialize"]