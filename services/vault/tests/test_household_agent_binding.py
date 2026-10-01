"""Household Vault identity guard: fake Redis and mocked MCP, never live services."""

import copy
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as vault_app  # noqa: E402
import custom_mcp  # noqa: E402
from connections import ConnectionStore  # noqa: E402


REAL_LIST_TOOLS = custom_mcp.list_tools
CID = "HOUSEHOLD_BOUND_TEST"
URL = "https://household.test/mcp"
UPSTREAM_TOKEN = "isolated-upstream-household-token"
ADMIN = {"X-Vault-Admin-Token": "isolated-vault-binding-admin"}
TOKENS = {
    "samantha": "isolated-samantha-vault-token",
    "harmony": "isolated-harmony-vault-token",
}
READ = "household_summary"
WRITE = "household_purchase_create"
UPLOAD = "household_upload"


def manifest(agent="samantha", names=(READ, WRITE, UPLOAD)):
    return [
        {
            "name": name,
            "description": "Scoped Household tool",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": name in custom_mcp.HOUSEHOLD_READ_TOOLS},
            **({"_meta": {"household_agent_id": agent}} if agent is not None else {}),
        }
        for name in names
    ]


@pytest.fixture
def env(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("External networking forbidden in Household binding tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    cache = fakeredis.FakeRedis(decode_responses=True, protocol=2)
    monkeypatch.setattr(vault_app, "r", cache)
    monkeypatch.setattr(vault_app, "store", ConnectionStore(
        cache, vault_app.encrypt_value, vault_app.decrypt_value))
    monkeypatch.setattr(vault_app, "ADMIN_API_TOKEN", ADMIN["X-Vault-Admin-Token"])
    for agent, token in TOKENS.items():
        cache.hset(f"{vault_app.PFX_AGENT}{agent}", mapping={
            "token_hash": vault_app.hash_token(token),
        })
        cache.zadd(vault_app.PFX_AGENT_INDEX, {agent: 0})
    state = SimpleNamespace(
        cache=cache, tools=manifest(), calls=[], lists=[], list_error=None,
    )

    async def fake_list(url, token):
        state.lists.append((url, token))
        if state.list_error:
            raise state.list_error
        return copy.deepcopy(state.tools)

    async def fake_call(url, token, name, arguments):
        state.calls.append((url, token, name, arguments))
        return {"content": [{"type": "text", "text": "Scoped upstream result"}]}

    async def fake_probe(cid):
        return {"ok": True}

    monkeypatch.setattr(custom_mcp, "list_tools", fake_list)
    monkeypatch.setattr(custom_mcp, "call_tool", fake_call)
    monkeypatch.setattr(vault_app, "_run_connection_test", fake_probe)
    # No context manager: Vault startup/background runtime must never execute.
    state.client = TestClient(vault_app.app)
    yield state
    state.client.close()
    cache.close()


def create(env, service="household_spending", grants=None):
    return env.client.post("/api/admin/connections", headers=ADMIN, json={
        "service": service, "name": CID, "base_url": URL,
        "bearer_token": UPSTREAM_TOKEN, "grant_agents": grants or [],
    })


def call(env, agent="samantha", tool=WRITE, arguments=None, headers=None):
    return env.client.post(f"/api/vault/mcp/{CID}", json={
        "tool": tool, "arguments": arguments or {},
        "agent_id": "samantha", "agent": "samantha",
    }, headers={
        "Authorization": f"Bearer {TOKENS[agent]}",
        **(headers or {}),
    })


def set_grant(env, agent, granted=True):
    return env.client.post("/api/admin/grants", headers=ADMIN, json={
        "agent": agent, "conn_id": CID, "granted": granted,
    })


def test_binding_is_preserved_by_real_list_transport_cache_and_agent_listing(env, monkeypatch):
    # Exercise the actual list_tools projection, with only the HTTP session
    # mocked. Metadata must survive untouched, not be rebuilt from tool args.
    async def session(url, token, **kwargs):
        assert (url, token) == (URL, UPSTREAM_TOKEN)
        assert kwargs["method"] == "tools/list"
        return {"result": {"tools": copy.deepcopy(env.tools)}}

    monkeypatch.setattr(custom_mcp, "_validate_server_url", lambda url: None)
    monkeypatch.setattr(custom_mcp, "_mcp_session", session)
    monkeypatch.setattr(custom_mcp, "list_tools", REAL_LIST_TOOLS)
    response = create(env, grants=["samantha"])
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["mcp_tools"] == env.tools
    cached = json.loads(env.cache.get(f"vault:conn:{CID}:mcp_tools"))
    assert cached == env.tools
    assert UPSTREAM_TOKEN not in env.cache.get(f"vault:conn:{CID}:household_manifest_proof")
    sync = env.client.post(f"/api/admin/mcp-bearer/{CID}/sync-tools", headers=ADMIN)
    assert sync.status_code == 200, sync.text
    cached = json.loads(env.cache.get(f"vault:conn:{CID}:mcp_tools"))
    assert all(tool["_meta"]["household_agent_id"] == "samantha" for tool in cached)
    listing = env.client.get("/api/vault/list", headers={
        "Authorization": f"Bearer {TOKENS['samantha']}",
    })
    assert listing.status_code == 200, listing.text
    assert listing.json()["available_connections"][0]["mcp_tools"] == env.tools
    assert UPSTREAM_TOKEN not in listing.text


def test_bound_tools_allow_only_authenticated_matching_agent_even_with_wrong_owner_grant(env):
    response = create(env)
    assert response.status_code == 200, response.text
    assert UPSTREAM_TOKEN not in response.text
    assert not any(vault_app.store.has_grant(agent, CID) for agent in TOKENS)
    assert call(env).status_code == 403
    assert env.calls == []
    assert set_grant(env, "samantha").status_code == 200
    for name in (READ, WRITE, UPLOAD):
        response = call(env, tool=name)
        assert response.status_code == 200, response.text
    assert set_grant(env, "harmony").status_code == 403
    assert not vault_app.store.has_grant("harmony", CID)
    # A historical grant or direct grant-store edit cannot bypass forwarding.
    vault_app.store.set_grant("harmony", CID, True)
    env.calls.clear()
    for name in (READ, WRITE, UPLOAD):
        response = call(env, "harmony", name, arguments={
            "agent_id": "samantha", "agent": "samantha", "name": "samantha",
        }, headers={"X-Agent-Id": "samantha", "X-Agent-Name": "samantha"})
        assert response.status_code == 403, response.text
        assert "different authenticated fleet agent" in response.text
    assert env.calls == []
    assert set_grant(env, "harmony", False).status_code == 200
    assert set_grant(env, "samantha", False).status_code == 200
    assert call(env).status_code == 403


def test_create_rejects_all_immediate_grants_if_any_agent_mismatches(env):
    response = create(env, grants=["samantha", "harmony"])
    assert response.status_code == 403, response.text
    assert not any(vault_app.store.has_grant(agent, CID) for agent in TOKENS)


@pytest.mark.parametrize("kind", [
    "no_metadata", "partial", "conflicting", "empty", "nonstring",
    "malformed_meta", "missing_manifest", "empty_manifest", "invalid_json",
    "wrong_container", "malformed_tool", "missing_proof", "stale_token",
    "stale_url", "changed_manifest", "write_marked_readonly",
    "read_marked_write", "whitespace_binding",
])
def test_unsafe_or_stale_manifests_fail_closed_without_forwarding(env, kind):
    assert create(env, grants=["samantha"]).status_code == 200
    tools = copy.deepcopy(env.tools)
    if kind == "no_metadata":
        for tool in tools:
            tool.pop("_meta")
    elif kind == "partial":
        tools[0].pop("_meta")
    elif kind == "conflicting":
        tools[0]["_meta"]["household_agent_id"] = "harmony"
    elif kind == "empty":
        tools[0]["_meta"]["household_agent_id"] = ""
    elif kind == "nonstring":
        tools[0]["_meta"]["household_agent_id"] = {"agent": "samantha"}
    elif kind == "whitespace_binding":
        tools[0]["_meta"]["household_agent_id"] = " samantha "
    elif kind == "malformed_meta":
        tools[0]["_meta"] = "samantha"
    elif kind == "empty_manifest":
        tools = []
    elif kind == "wrong_container":
        tools = {"tools": tools}
    elif kind == "malformed_tool":
        tools[0] = None
    elif kind == "write_marked_readonly":
        tools = manifest(None, names=(WRITE,))
        tools[0]["annotations"]["readOnlyHint"] = True
    elif kind == "read_marked_write":
        tools = manifest(None, names=(READ,))
        tools[0]["annotations"]["readOnlyHint"] = False
    if kind == "missing_manifest":
        env.cache.delete(f"vault:conn:{CID}:mcp_tools")
    elif kind == "invalid_json":
        env.cache.set(f"vault:conn:{CID}:mcp_tools", "{")
    elif kind == "missing_proof":
        env.cache.delete(f"vault:conn:{CID}:household_manifest_proof")
    elif kind in {"stale_token", "stale_url"}:
        conn = vault_app.store.get(CID)
        secrets_d = vault_app.store.get_secrets(CID)
        if kind == "stale_token":
            secrets_d["api_key"] = "isolated-new-token"
        else:
            conn["base_url"] = "https://household-other.test/mcp"
        vault_app.store.save(CID, service=conn["service"],
                             base_url=conn["base_url"], auth=conn["auth"],
                             secrets=secrets_d)
    elif kind == "changed_manifest":
        tools[0]["description"] = "Unsynced replacement"
        env.cache.set(f"vault:conn:{CID}:mcp_tools", json.dumps(tools))
    else:
        vault_app._cache_mcp_tools(CID, URL, UPSTREAM_TOKEN, tools)
    for name in (READ, WRITE):
        response = call(env, tool=name)
        assert response.status_code == 403, response.text
    assert env.calls == []


@pytest.mark.parametrize("field,value", [
    ("bearer_token", "isolated-rotated-household-token"),
    ("base_url", "https://new-household.test/mcp"),
])
def test_failed_credential_or_url_resync_removes_old_manifest(env, field, value):
    assert create(env, grants=["samantha"]).status_code == 200
    env.list_error = custom_mcp.CustomMCPError("Isolated mock sync failure")
    response = env.client.post(f"/api/admin/connections/{CID}/update",
                               headers=ADMIN, json={field: value})
    assert response.status_code == 200, response.text
    assert env.cache.get(f"vault:conn:{CID}:mcp_tools") is None
    assert env.cache.get(f"vault:conn:{CID}:household_manifest_proof") is None
    assert call(env, tool=READ).status_code == 403
    assert call(env).status_code == 403
    assert env.calls == []


def test_successful_token_rotation_changes_bound_identity_and_old_grants_do_not_help(env):
    assert create(env, grants=["samantha"]).status_code == 200
    env.tools = manifest("harmony")
    response = env.client.post(f"/api/admin/connections/{CID}/update",
                               headers=ADMIN, json={"bearer_token": "isolated-new-token"})
    assert response.status_code == 200, response.text
    assert call(env).status_code == 403
    assert set_grant(env, "harmony").status_code == 200
    assert call(env, "harmony").status_code == 200
    assert env.calls[-1][1] == "isolated-new-token"


def test_unchanged_url_and_blank_secret_update_keeps_valid_manifest(env):
    assert create(env, grants=["samantha"]).status_code == 200
    proof = env.cache.get(f"vault:conn:{CID}:household_manifest_proof")
    response = env.client.post(f"/api/admin/connections/{CID}/update",
                               headers=ADMIN, json={"base_url": URL, "api_key": ""})
    assert response.status_code == 200, response.text
    assert env.cache.get(f"vault:conn:{CID}:household_manifest_proof") == proof
    assert len(env.lists) == 1
    assert call(env).status_code == 200


def test_html_failed_rotation_invalidates_old_manifest(env, monkeypatch):
    assert create(env, grants=["samantha"]).status_code == 200
    monkeypatch.setattr(vault_app, "_admin_or_redirect", lambda request: None)
    env.list_error = custom_mcp.CustomMCPError("Isolated mock HTML sync failure")
    response = env.client.post("/services/update", data={
        "conn_id": CID, "mcp_token": "isolated-new-html-token",
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert env.cache.get(f"vault:conn:{CID}:mcp_tools") is None
    assert call(env).status_code == 403
    assert env.calls == []


def test_proof_uses_connection_store_canonical_url(env):
    assert create(env, grants=["samantha"]).status_code == 200
    vault_app._cache_mcp_tools(CID, URL + "/", UPSTREAM_TOKEN, env.tools)
    assert call(env).status_code == 200


def test_failed_recreation_sync_never_reuses_orphaned_old_manifest(env):
    assert create(env, grants=["samantha"]).status_code == 200
    assert env.client.post(f"/api/admin/connections/{CID}/delete",
                           headers=ADMIN).status_code == 200
    env.list_error = custom_mcp.CustomMCPError("Isolated failed replacement sync")
    response = create(env)
    assert response.status_code == 200, response.text
    assert env.cache.get(f"vault:conn:{CID}:mcp_tools") is None
    assert set_grant(env, "samantha").status_code == 200
    assert call(env, tool=READ).status_code == 403
    assert call(env).status_code == 403
    assert env.calls == []


def test_shared_legacy_readonly_tokens_remain_available_without_auto_grants(env):
    env.tools = manifest(None, names=(READ,))
    assert create(env).status_code == 200
    # Simulate a pre-rollout cached read-only connection, with no proof key.
    env.cache.delete(f"vault:conn:{CID}:household_manifest_proof")
    assert call(env, tool=READ).status_code == 403
    for agent in TOKENS:
        assert set_grant(env, agent).status_code == 200
        assert call(env, agent, READ).status_code == 200
    env.calls.clear()
    assert call(env).status_code == 403
    assert call(env, tool=UPLOAD).status_code == 403
    assert env.calls == []


def test_new_unbound_readonly_tools_do_not_accidentally_count_as_writes(env):
    env.tools = manifest(None, names=tuple(sorted(custom_mcp.HOUSEHOLD_READ_TOOLS)))
    assert create(env, grants=["samantha"]).status_code == 200
    for name in custom_mcp.HOUSEHOLD_READ_TOOLS:
        assert call(env, tool=name).status_code == 200


def test_html_grant_update_and_pending_request_approval_reject_mismatches(env, monkeypatch):
    assert create(env).status_code == 200
    monkeypatch.setattr(vault_app, "_admin_or_redirect", lambda request: None)
    response = env.client.post("/grants/update", data={
        f"grant_samantha_{CID}": "on", f"grant_harmony_{CID}": "on",
    })
    assert response.status_code == 403, response.text
    assert not any(vault_app.store.has_grant(agent, CID) for agent in TOKENS)
    request = vault_app.store.create_request("harmony", "grant", CID)
    response = env.client.post("/requests/approve-grant", data={"request_id": request["id"]})
    assert response.status_code == 403, response.text
    assert not vault_app.store.has_grant("harmony", CID)
    assert vault_app.store.get_request(request["id"]) is not None


def test_readonly_bound_token_still_enforces_agent_identity(env):
    env.tools = manifest("samantha", names=(READ,))
    assert create(env, grants=["samantha"]).status_code == 200
    vault_app.store.set_grant("harmony", CID, True)
    assert call(env, tool=READ).status_code == 200
    env.calls.clear()
    assert call(env, "harmony", READ).status_code == 403
    assert env.calls == []


def test_unadvertised_write_is_blocked_even_for_the_matching_bound_agent(env):
    assert create(env, grants=["samantha"]).status_code == 200
    assert call(env, tool="household_unadvertised_write").status_code == 403
    assert env.calls == []


def test_other_mcp_providers_are_not_subject_to_household_binding(env):
    assert create(env, service="mcp_bearer", grants=["harmony"]).status_code == 200
    env.cache.delete(f"vault:conn:{CID}:mcp_tools")
    assert call(env, "harmony").status_code == 200
    assert env.calls[-1][2] == WRITE


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-test-identity"}])
def test_supplied_identity_cannot_replace_vault_authentication(env, headers):
    assert create(env, grants=["samantha"]).status_code == 200
    response = env.client.post(f"/api/vault/mcp/{CID}", json={
        "tool": WRITE, "agent_id": "samantha", "arguments": {"agent_id": "samantha"},
    }, headers={**headers, "X-Agent-Name": "samantha"})
    assert response.status_code == 401
    assert env.calls == []