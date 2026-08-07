"""End-to-end verification of auth-field clearing semantics.

Exercises the actual add_service / update_service handlers via Starlette's
synchronous TestClient (real Redis, real Fernet encryption, no asyncio plugin
required).  Uses isolated connection IDs and cleans up after itself.

Run from services/vault/:
    pytest tests/test_e2e_auth_fields.py -v

Tests 7–11 are regression tests for the api_key→mcp_token rename and the
Alpaca Paper/Live environment selector.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Bootstrap env before importing the vault app module.
os.environ.setdefault("VAULT_ADMIN_PASSWORD", "e2e-test-pass")
os.environ.setdefault("VAULT_MASTER_KEY", "")  # triggers auto-generate path in app

import app as vault_app  # noqa: E402
from connections import build_auth  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Module-scoped client fixture — inject session directly.
# The login endpoint sets secure=True on the cookie so it is never sent back
# by TestClient over http://testserver.  We bypass the HTTP login entirely:
# write a synthetic session token into the app's in-memory SESSION_TOKENS dict
# and plant the cookie directly on the client.
# ---------------------------------------------------------------------------

_TEST_SESSION = "e2e-test-session-token"


@pytest.fixture(scope="module")
def client():
    import time
    import httpx
    # Plant the session directly into the app's in-memory token store so we
    # don't need a real HTTPS login flow (the login endpoint sets secure=True
    # which httpx's TestClient won't send back over http://testserver).
    vault_app.SESSION_TOKENS[_TEST_SESSION] = time.time() + 86400
    jar = httpx.Cookies()
    jar.set("vault_session", _TEST_SESSION)  # no domain → sent to all URLs
    with TestClient(vault_app.app, raise_server_exceptions=True, cookies=jar) as c:
        yield c
    vault_app.SESSION_TOKENS.pop(_TEST_SESSION, None)


# ---------------------------------------------------------------------------
# Per-test cleanup
# ---------------------------------------------------------------------------

CLEANUP_IDS: list = []


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for cid in list(CLEANUP_IDS):
        try:
            vault_app.store.delete(cid, [])
        except Exception:
            pass
    CLEANUP_IDS.clear()


# ===========================================================================
# Test 1 — Alpaca-style add: custom header name, empty prefix, extra header
# ===========================================================================

def test_add_alpaca_style(client):
    """
    Add custom connection:
      header_name  = APCA-API-KEY-ID
      prefix       = "" (blank → raw key, no prefix)
      api_key      = KEYID123
      extra header = APCA-API-SECRET-KEY: SECRET456

    Stored state must have prefix="" (not "Bearer ").
    Outgoing headers: APCA-API-KEY-ID: KEYID123, APCA-API-SECRET-KEY: SECRET456.
    No Authorization header.
    """
    conn_id = "E2E_ALPACA_ADD"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":            "custom",
            "name":               conn_id,
            "base_url":           "https://paper-api.alpaca.markets/v2",
            "header_name":        "APCA-API-KEY-ID",
            "prefix":             "",
            "api_key":            "KEYID123",
            "extra_header_name":  "APCA-API-SECRET-KEY",
            "extra_header_value": "SECRET456",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"Add failed: {r.status_code}\n{r.text[:400]}"

    # --- stored auth dict ---
    conn = vault_app.store.get(conn_id)
    assert conn is not None, "Connection not saved to Redis"
    auth = conn["auth"]
    assert auth["header_name"] == "APCA-API-KEY-ID", f"header_name wrong: {auth}"
    assert auth["prefix"] == "", (
        f"prefix should be empty string (raw key), got: {repr(auth.get('prefix'))}"
    )

    # --- stored secrets ---
    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets["api_key"] == "KEYID123"
    assert secrets["extra_headers"]["APCA-API-SECRET-KEY"] == "SECRET456"

    # --- outgoing header set ---
    result = build_auth(conn, secrets)
    hdrs = result["headers"]
    assert hdrs.get("APCA-API-KEY-ID") == "KEYID123", f"Primary header wrong: {hdrs}"
    assert hdrs.get("APCA-API-SECRET-KEY") == "SECRET456", f"Secret header missing: {hdrs}"
    assert "Authorization" not in hdrs, (
        f"Authorization MUST NOT be emitted for custom-header-name conn: {hdrs}"
    )
    assert result["params"] == {}


# ===========================================================================
# Test 2 — Edit: clear Bearer prefix on an existing connection
# ===========================================================================

def test_edit_clear_bearer_prefix(client):
    """
    Start: header_name=Authorization, prefix="Bearer ".
    Edit:  header_name=APCA-API-KEY-ID, prefix="" (blank).

    After: auth.prefix="" stored; outgoing emits APCA-API-KEY-ID only.
    """
    conn_id = "E2E_EDIT_CLEAR_BEARER"
    CLEANUP_IDS.append(conn_id)

    # Seed with a standard Bearer connection.
    vault_app.store.save(
        conn_id,
        service="custom",
        label="Test Bearer",
        base_url="https://api.example.com",
        auth={"kind": "header", "header_name": "Authorization", "prefix": "Bearer "},
        secrets={"api_key": "BEARER_KEY"},
    )

    r = client.post(
        "/services/update",
        data={
            "conn_id":     conn_id,
            "header_name": "APCA-API-KEY-ID",
            "prefix":      "",            # explicit blank = clear the prefix
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"Update failed: {r.status_code}\n{r.text[:400]}"

    conn = vault_app.store.get(conn_id)
    auth = conn["auth"]
    assert auth["header_name"] == "APCA-API-KEY-ID", f"header_name not updated: {auth}"
    assert auth["prefix"] == "", f"prefix not cleared: {repr(auth.get('prefix'))}"

    secrets = vault_app.store.get_secrets(conn_id)
    result = build_auth(conn, secrets)
    hdrs = result["headers"]
    assert hdrs.get("APCA-API-KEY-ID") == "BEARER_KEY", f"Wrong primary header: {hdrs}"
    assert "Authorization" not in hdrs, f"Authorization must not appear after clear: {hdrs}"


# ===========================================================================
# Test 3 — Edit: blank header_name stores "Authorization" explicitly
# ===========================================================================

def test_edit_blank_header_name_stores_authorization(client):
    """
    If header_name is blanked on edit, the server stores "Authorization"
    explicitly — not an empty string relying on the builder's `or` fallback.
    """
    conn_id = "E2E_EDIT_BLANK_NAME"
    CLEANUP_IDS.append(conn_id)

    vault_app.store.save(
        conn_id,
        service="custom",
        label="Test",
        base_url="https://api.example.com",
        auth={"kind": "header", "header_name": "X-Custom-Key", "prefix": "Token "},
        secrets={"api_key": "tok-abc"},
    )

    r = client.post(
        "/services/update",
        data={"conn_id": conn_id, "header_name": "", "prefix": "Bearer "},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    conn = vault_app.store.get(conn_id)
    auth = conn["auth"]
    assert auth["header_name"] == "Authorization", (
        f"Blank header_name should store 'Authorization' explicitly, got: {repr(auth.get('header_name'))}"
    )
    assert auth["prefix"] == "Bearer "

    secrets = vault_app.store.get_secrets(conn_id)
    result = build_auth(conn, secrets)
    assert result["headers"]["Authorization"] == "Bearer tok-abc"


# ===========================================================================
# Test 4 — Regression: Bearer connection with pre-filled fields re-saved
# ===========================================================================

def test_bearer_regression_prefilled_resave(client):
    """
    Simulate the pre-fill modal: header_name and prefix fields are populated
    with their current stored values and re-submitted unchanged.
    Auth dict and outgoing headers must be identical before and after.
    Trailing space in "Bearer " must be preserved (no strip).
    """
    conn_id = "E2E_BEARER_REGRESSION"
    CLEANUP_IDS.append(conn_id)

    vault_app.store.save(
        conn_id,
        service="custom",
        label="OpenAI-style",
        base_url="https://api.openai.com/v1",
        auth={"kind": "header", "header_name": "Authorization", "prefix": "Bearer "},
        secrets={"api_key": "sk-regression-key"},
    )

    # Simulate the edit modal pre-filling both fields and submitting unchanged.
    r = client.post(
        "/services/update",
        data={
            "conn_id":     conn_id,
            "description": "Updated description only",
            "header_name": "Authorization",    # pre-filled, unchanged
            "prefix":      "Bearer ",          # pre-filled, trailing space preserved
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"Update failed: {r.status_code}\n{r.text[:400]}"

    conn = vault_app.store.get(conn_id)
    auth = conn["auth"]
    assert auth["header_name"] == "Authorization"
    assert auth["prefix"] == "Bearer ", (
        f"Trailing space in 'Bearer ' must be preserved, got: {repr(auth['prefix'])}"
    )

    secrets = vault_app.store.get_secrets(conn_id)
    result = build_auth(conn, secrets)
    assert result["headers"]["Authorization"] == "Bearer sk-regression-key"
    assert set(result["headers"].keys()) == {"Authorization"}


# ===========================================================================
# Test 5 — Add path: Bearer trailing space preserved
# ===========================================================================

def test_add_bearer_trailing_space_preserved(client):
    """
    Add a custom connection with prefix="Bearer " (with trailing space).
    Server must NOT strip it.  build_auth must emit "Bearer <key>".
    """
    conn_id = "E2E_BEARER_ADD"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":     "custom",
            "name":        conn_id,
            "base_url":    "https://api.example.com/v1",
            "header_name": "Authorization",
            "prefix":      "Bearer ",
            "api_key":     "sk-abc",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    conn = vault_app.store.get(conn_id)
    auth = conn["auth"]
    assert auth["prefix"] == "Bearer ", (
        f"Trailing space stripped — must be preserved: {repr(auth.get('prefix'))}"
    )

    secrets = vault_app.store.get_secrets(conn_id)
    result = build_auth(conn, secrets)
    assert result["headers"]["Authorization"] == "Bearer sk-abc"


# ===========================================================================
# Test 6 — Add path: blank header_name stores "Authorization" explicitly
# ===========================================================================

def test_add_blank_header_name_stores_authorization(client):
    """
    If header_name is submitted blank on add, server must store "Authorization"
    explicitly (not leave whatever the catalog default happened to be).
    """
    conn_id = "E2E_ADD_BLANK_NAME"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":     "custom",
            "name":        conn_id,
            "base_url":    "https://api.example.com/v1",
            "header_name": "",          # blank → should store "Authorization"
            "prefix":      "Token ",
            "api_key":     "mytoken",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    conn = vault_app.store.get(conn_id)
    auth = conn["auth"]
    assert auth["header_name"] == "Authorization", (
        f"Blank header_name on add should store 'Authorization', got: {repr(auth.get('header_name'))}"
    )
    assert auth["prefix"] == "Token "

    secrets = vault_app.store.get_secrets(conn_id)
    result = build_auth(conn, secrets)
    assert result["headers"]["Authorization"] == "Token mytoken"


# ===========================================================================
# Test 7 — MCP edit: token rotation via edit modal (api_key field) updates
#           the stored secret and is detected by resync logic.
#
# Root cause being guarded: update_service used to read only form["mcp_token"]
# for MCP token rotation; the edit modal submits name="api_key" (shared
# credential field), so the new token was silently dropped and resync never
# fired.  Fix: accept mcp_token OR api_key for MCP in update_service.
# ===========================================================================

def test_mcp_edit_token_rotation_via_api_key_field(client, monkeypatch):
    """
    Edit-modal path: POSTing api_key=NEW_TOKEN for an MCP connection must
    update the stored secret and set _tok_changed=True (triggering resync).

    We verify the stored secret directly.  Resync is non-fatal (it calls
    custom_mcp.list_tools which may fail in CI); we monkeypatch it to a no-op
    and assert it was called — proving detection fired.
    """
    conn_id = "E2E_MCP_TOKEN_ROTATE"
    CLEANUP_IDS.append(conn_id)

    # Seed an MCP bearer connection.
    vault_app.store.save(
        conn_id,
        service="mcp_bearer",
        label="Test MCP",
        base_url="https://mcp.example.com/api/mcp",
        auth={"kind": "mcp_bearer"},
        secrets={"api_key": "OLD_TOKEN"},
    )

    # Track whether list_tools was called (resync detection).
    resync_calls: list = []

    async def fake_list_tools(base_url, token):
        resync_calls.append((base_url, token))
        return []

    monkeypatch.setattr(vault_app.custom_mcp, "list_tools", fake_list_tools)

    # Simulate the edit modal submitting api_key (not mcp_token).
    r = client.post(
        "/services/update",
        data={
            "conn_id": conn_id,
            "api_key": "NEW_TOKEN",   # edit modal sends api_key
            # mcp_token is NOT submitted — must fall back to api_key
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"Update failed: {r.status_code}\n{r.text[:400]}"

    # Stored secret must be the new token.
    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets.get("api_key") == "NEW_TOKEN", (
        f"Token not rotated — stored: {secrets.get('api_key')!r} (expected 'NEW_TOKEN'). "
        "update_service is reading mcp_token only and ignoring the edit modal's api_key field."
    )

    # Resync must have been triggered.
    assert resync_calls, (
        "list_tools was never called — resync detection did not fire after token rotation. "
        "_tok_changed was False, meaning the form value wasn't read."
    )
    assert resync_calls[0][1] == "NEW_TOKEN", (
        f"list_tools called with wrong token: {resync_calls[0][1]!r}"
    )


# ===========================================================================
# Test 8 — MCP add: mcp_token field (add form) stores the token correctly
#           even when api_key="" is submitted simultaneously (hidden Alpaca
#           field present in add form DOM).
# ===========================================================================

def test_mcp_add_via_mcp_token_field_no_api_key_collision(client, monkeypatch):
    """
    Add-form path: add an MCP connection by submitting mcp_token=TOKEN with
    api_key="" present (the hidden Alpaca key field is always in the DOM).
    The stored secret must contain the mcp_token value.
    """
    conn_id = "E2E_MCP_ADD_TOKEN"
    CLEANUP_IDS.append(conn_id)

    monkeypatch.setattr(
        vault_app.custom_mcp, "list_tools",
        lambda base_url, token: [],
    )

    r = client.post(
        "/services/add",
        data={
            "service":   "mcp_bearer",
            "name":      conn_id,
            "base_url":  "https://mcp.example.com/api/mcp",
            "mcp_token": "MCP_SECRET_TOKEN",
            "api_key":   "",            # hidden Alpaca field — must NOT overwrite
        },
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert r.status_code in (302, 303), f"Add failed: {r.status_code}\n{r.text[:400]}"
    assert "error" not in loc.lower(), f"Add returned error: {loc}"

    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets.get("api_key") == "MCP_SECRET_TOKEN", (
        f"MCP token not stored correctly. Got: {secrets.get('api_key')!r}. "
        "api_key='' from the hidden Alpaca field may have overwritten mcp_token."
    )


# ===========================================================================
# Test 9 — Alpaca add: paper env sets paper base_url; live env sets live
#           base_url. Both succeed even with mcp_token="" in form (hidden
#           field coexistence).
# ===========================================================================

def test_alpaca_add_paper_env(client):
    """alpaca_env=paper → base_url must be paper-api.alpaca.markets."""
    conn_id = "E2E_ALPACA_PAPER_ENV"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":    "alpaca",
            "name":       conn_id,
            "api_key":    "PK_PAPER_TEST",
            "api_secret": "SK_PAPER_TEST",
            "alpaca_env": "paper",
            "mcp_token":  "",   # hidden field in add form — must not interfere
        },
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert r.status_code in (302, 303), f"Add failed: {r.status_code}\n{r.text[:400]}"
    assert "error" not in loc.lower(), f"Add returned error: {loc}"

    conn = vault_app.store.get(conn_id)
    assert conn is not None
    assert conn["base_url"] == "https://paper-api.alpaca.markets/v2", (
        f"Paper env: wrong base_url: {conn['base_url']}"
    )
    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets.get("api_key") == "PK_PAPER_TEST", (
        f"api_key not stored. Got: {secrets.get('api_key')!r}. "
        "Hidden mcp_token field may still be colliding with api_key."
    )
    assert secrets.get("extra_headers", {}).get("APCA-API-SECRET-KEY") == "SK_PAPER_TEST"


def test_alpaca_add_live_env(client):
    """alpaca_env=live → base_url must be api.alpaca.markets."""
    conn_id = "E2E_ALPACA_LIVE_ENV"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":    "alpaca",
            "name":       conn_id,
            "api_key":    "PK_LIVE_TEST",
            "api_secret": "SK_LIVE_TEST",
            "alpaca_env": "live",
            "mcp_token":  "",
        },
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert r.status_code in (302, 303)
    assert "error" not in loc.lower(), f"Add returned error: {loc}"

    conn = vault_app.store.get(conn_id)
    assert conn["base_url"] == "https://api.alpaca.markets/v2", (
        f"Live env: wrong base_url: {conn['base_url']}"
    )
    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets.get("api_key") == "PK_LIVE_TEST"
    assert secrets.get("extra_headers", {}).get("APCA-API-SECRET-KEY") == "SK_LIVE_TEST"


# ===========================================================================
# Test 10 — Alpaca update: alpaca_env switches base_url; absent env keeps
#            existing base_url (pre-existing paper connections unaffected).
# ===========================================================================

def test_alpaca_update_env_switch_paper_to_live(client):
    """Editing alpaca_env=live on a paper connection switches base_url."""
    conn_id = "E2E_ALPACA_UPDATE_SWITCH"
    CLEANUP_IDS.append(conn_id)

    vault_app.store.save(
        conn_id,
        service="alpaca",
        label="Alpaca",
        base_url="https://paper-api.alpaca.markets/v2",
        auth={"kind": "header", "header_name": "APCA-API-KEY-ID", "prefix": ""},
        secrets={"api_key": "PK_OLD", "extra_headers": {"APCA-API-SECRET-KEY": "SK_OLD"}},
    )

    r = client.post(
        "/services/update",
        data={"conn_id": conn_id, "alpaca_env": "live"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), f"Update failed: {r.status_code}\n{r.text[:400]}"

    conn = vault_app.store.get(conn_id)
    assert conn["base_url"] == "https://api.alpaca.markets/v2", (
        f"alpaca_env=live did not update base_url: {conn['base_url']}"
    )


def test_alpaca_update_no_env_keeps_existing(client):
    """
    When alpaca_env is absent from the edit form (pre-existing paper connection),
    base_url must stay unchanged.
    """
    conn_id = "E2E_ALPACA_UPDATE_KEEP"
    CLEANUP_IDS.append(conn_id)

    vault_app.store.save(
        conn_id,
        service="alpaca",
        label="Alpaca",
        base_url="https://paper-api.alpaca.markets/v2",
        auth={"kind": "header", "header_name": "APCA-API-KEY-ID", "prefix": ""},
        secrets={"api_key": "PK_OLD", "extra_headers": {"APCA-API-SECRET-KEY": "SK_OLD"}},
    )

    # Simulate editing only the description — no alpaca_env in POST body.
    r = client.post(
        "/services/update",
        data={"conn_id": conn_id, "description": "updated description only"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    conn = vault_app.store.get(conn_id)
    assert conn["base_url"] == "https://paper-api.alpaca.markets/v2", (
        f"Pre-existing paper base_url changed unexpectedly: {conn['base_url']}"
    )


# ===========================================================================
# Test 11 — Regression: Alpaca add with both api_key and mcp_token="" present
#            (full add-form field set) must not produce an "API key required"
#            error.  This is the original save bug from the api_key rename.
# ===========================================================================

def test_alpaca_add_no_api_key_required_error_with_hidden_mcp_token(client):
    """
    The add form always has both api_key (grp_api_key) and mcp_token
    (grp_mcp_bearer, renamed from api_key) in the DOM.  When Alpaca is
    selected, mcp_token="" is submitted alongside the real api_key.
    The handler must read api_key correctly (not be overwritten by mcp_token).
    """
    conn_id = "E2E_ALPACA_NO_ERROR"
    CLEANUP_IDS.append(conn_id)

    r = client.post(
        "/services/add",
        data={
            "service":    "alpaca",
            "name":       conn_id,
            "api_key":    "PK_REAL_KEY",     # the actual Alpaca key ID
            "api_secret": "SK_REAL_SECRET",
            "alpaca_env": "paper",
            "mcp_token":  "",                # hidden MCP field — must not clobber api_key
        },
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")
    assert "api+key+required" not in loc.lower(), (
        f"Got 'API key required' error — mcp_token=''/api_key collision not fixed: {loc}"
    )
    assert "error" not in loc.lower(), f"Unexpected error: {loc}"
    assert r.status_code in (302, 303)

    secrets = vault_app.store.get_secrets(conn_id)
    assert secrets.get("api_key") == "PK_REAL_KEY", (
        f"api_key not stored correctly: {secrets.get('api_key')!r}"
    )
