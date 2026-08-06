"""End-to-end verification of auth-field clearing semantics.

Exercises the actual add_service / update_service handlers via Starlette's
synchronous TestClient (real Redis, real Fernet encryption, no asyncio plugin
required).  Uses isolated connection IDs and cleans up after itself.

Run from services/vault/:
    pytest tests/test_e2e_auth_fields.py -v
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
