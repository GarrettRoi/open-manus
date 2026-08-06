"""Tests for the vault JSON update endpoint and its merge-semantics helper.

Covers:
  - _apply_connection_update: blank-keeps-existing for every secret field type
  - _apply_connection_update: header_name / prefix absent-vs-present semantics
  - _apply_connection_update: oauth2 URL validation plumbing
  - _apply_connection_update: email / apple credential merge
  - POST /api/admin/connections/{id}/update HTTP endpoint:
      auth guard, 404, 415, label/base_url/description merge, round-trip
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy vault dependencies BEFORE importing app.py
# ---------------------------------------------------------------------------

_VAULT_DIR = Path(__file__).resolve().parent.parent / "services" / "vault"
if str(_VAULT_DIR) not in sys.path:
    sys.path.insert(0, str(_VAULT_DIR))

def _ensure_stub(name: str) -> MagicMock:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()
    return sys.modules[name]  # type: ignore[return-value]

for _mod in ("redis", "uvicorn", "apple_ops", "email_ops", "google_ops",
             "oauth", "backup", "replit_mcp"):
    _ensure_stub(_mod)

# redis.from_url must return something that looks like a redis client
_redis_stub = sys.modules["redis"]
_redis_stub.from_url = MagicMock(return_value=MagicMock())

# oauth module needs OAuthError and a callable build_authorize_url
_oauth_stub = sys.modules["oauth"]
_oauth_stub.OAuthError = type("OAuthError", (Exception,), {})
_oauth_stub.redirect_uri = MagicMock(return_value="https://vault.test/oauth/callback")

# apple_ops needs AppleOpsError
_apple_stub = sys.modules["apple_ops"]
_apple_stub.AppleOpsError = type("AppleOpsError", (Exception,), {})
_apple_stub.run_apple_operation = MagicMock()

# replit_mcp needs ReplitMCPError and a ReplitMCP class
_mcp_stub = sys.modules["replit_mcp"]
_mcp_stub.ReplitMCPError = type("ReplitMCPError", (Exception,), {})
_mcp_stub.ReplitMCP = MagicMock()
_mcp_stub.DISPATCH_QUEUE = "devreq:dispatch"
_mcp_stub.dispatch_loop = MagicMock()

# backup stub
_backup_stub = sys.modules["backup"]
_backup_stub.backup_loop = MagicMock()
_backup_stub.backup_now = MagicMock()
_backup_stub.list_backups = MagicMock(return_value=[])
_backup_stub.restore_from_file = MagicMock()

# Set required env vars before importing app so it doesn't blow up
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("VAULT_ADMIN_PASSWORD", "test-admin-pw")
os.environ.setdefault("VAULT_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("VAULT_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LTMyYnl0ZXMhISE")  # base64

import app as _vault_app  # noqa: E402  (must come after stubs)
from app import _apply_connection_update  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _bearer_conn(conn_id: str = "OPENAI") -> dict:
    return {
        "id": conn_id,
        "service": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com",
        "auth": {"kind": "bearer"},
        "status": "ready",
        "description": "Main OpenAI key",
        "skill_description": "Use for all LLM calls",
    }


def _header_conn() -> dict:
    return {
        "id": "ELEVENLABS",
        "service": "elevenlabs",
        "label": "ElevenLabs",
        "base_url": "https://api.elevenlabs.io",
        "auth": {"kind": "header", "header_name": "xi-api-key", "prefix": ""},
        "status": "ready",
        "description": "",
        "skill_description": "",
    }


def _oauth_conn() -> dict:
    return {
        "id": "GOOGLE",
        "service": "google",
        "label": "Google Workspace",
        "base_url": "https://www.googleapis.com",
        "auth": {"kind": "oauth2"},
        "status": "needs_login",
        "description": "",
        "skill_description": "",
    }


def _custom_oauth_conn() -> dict:
    return {
        "id": "CUSTOM_OAUTH",
        "service": "custom_oauth",
        "label": "My Custom OAuth",
        "base_url": "https://api.example.com",
        "auth": {
            "kind": "oauth2",
            "oauth": {
                "authorize_url": "https://example.com/oauth/authorize",
                "token_url": "https://example.com/oauth/token",
                "scopes": ["read", "write"],
            },
        },
        "status": "needs_login",
        "description": "",
        "skill_description": "",
    }


def _apple_conn() -> dict:
    return {
        "id": "APPLE_ICLOUD",
        "service": "apple",
        "label": "Apple iCloud",
        "base_url": "https://caldav.icloud.com",
        "auth": {"kind": "apple"},
        "status": "ready",
        "description": "",
        "skill_description": "",
    }


# ---------------------------------------------------------------------------
# Tests: _apply_connection_update — merge semantics
# ---------------------------------------------------------------------------

class TestApplyConnectionUpdateBearer(unittest.TestCase):
    """Bearer-kind connections: api_key blank-keeps-existing."""

    def setUp(self):
        self.conn = _bearer_conn()
        self.secrets = {"api_key": "sk-OLD-KEY"}

    def test_blank_api_key_keeps_existing(self):
        conn_out, sec_out, err = _apply_connection_update(self.conn, self.secrets, {})
        self.assertIsNone(err)
        self.assertEqual(sec_out["api_key"], "sk-OLD-KEY",
                         "absent api_key must not clear existing key")

    def test_blank_string_api_key_keeps_existing(self):
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": ""}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["api_key"], "sk-OLD-KEY",
                         "empty-string api_key must not clear existing key")

    def test_whitespace_only_api_key_keeps_existing(self):
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": "   "}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["api_key"], "sk-OLD-KEY")

    def test_nonblank_api_key_replaces(self):
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": "sk-NEW-KEY"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["api_key"], "sk-NEW-KEY")

    def test_api_key_is_stripped(self):
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": "  sk-PADDED  "}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["api_key"], "sk-PADDED")

    def test_public_key_blank_keeps_existing(self):
        secrets = {"api_key": "sk-OLD", "public_key": "pk_live_old"}
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, secrets, {"api_key": "sk-NEW"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["public_key"], "pk_live_old",
                         "absent public_key must not clear existing")

    def test_public_key_replaces(self):
        secrets = {"api_key": "sk-OLD", "public_key": "pk_live_old"}
        conn_out, sec_out, err = _apply_connection_update(
            self.conn, secrets, {"public_key": "pk_live_new"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["public_key"], "pk_live_new")

    def test_inputs_not_mutated(self):
        original_secrets = {"api_key": "sk-ORIG"}
        _apply_connection_update(self.conn, original_secrets, {"api_key": "sk-NEW"})
        self.assertEqual(original_secrets["api_key"], "sk-ORIG",
                         "_apply_connection_update must not mutate inputs")


class TestApplyConnectionUpdateOAuth(unittest.TestCase):
    """OAuth2 connections: client_id / client_secret blank-keeps-existing."""

    def setUp(self):
        self.conn = _oauth_conn()
        self.secrets = {"client_id": "old_id", "client_secret": "old_secret"}

    def test_blank_client_id_keeps_existing(self):
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, {})
        self.assertIsNone(err)
        self.assertEqual(sec_out["client_id"], "old_id")

    def test_blank_client_secret_keeps_existing(self):
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, {})
        self.assertIsNone(err)
        self.assertEqual(sec_out["client_secret"], "old_secret")

    def test_nonblank_client_id_replaces(self):
        _, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"client_id": "new_id"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["client_id"], "new_id")
        # client_secret unchanged
        self.assertEqual(sec_out["client_secret"], "old_secret")

    def test_nonblank_client_secret_replaces(self):
        _, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"client_secret": "new_secret"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["client_secret"], "new_secret")
        self.assertEqual(sec_out["client_id"], "old_id")


class TestApplyConnectionUpdateHeaderKind(unittest.TestCase):
    """header-kind connections: header_name / prefix absent-vs-present semantics."""

    def setUp(self):
        self.conn = _header_conn()
        self.secrets = {"api_key": "el-OLD"}

    def test_absent_header_name_unchanged(self):
        # key not in body at all → unchanged
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": "el-NEW"}
        )
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["header_name"], "xi-api-key")

    def test_absent_prefix_unchanged(self):
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"api_key": "el-NEW"}
        )
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["prefix"], "")

    def test_present_header_name_applied(self):
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"header_name": "Authorization"}
        )
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["header_name"], "Authorization")

    def test_blank_header_name_defaults_to_authorization(self):
        # present but blank → "Authorization" (mirror HTML route semantics)
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"header_name": ""}
        )
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["header_name"], "Authorization")

    def test_present_prefix_applied(self):
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"prefix": "Bearer "}
        )
        self.assertIsNone(err)
        # Trailing space is intentional — do NOT strip
        self.assertEqual(conn_out["auth"]["prefix"], "Bearer ")

    def test_blank_prefix_stored_as_empty_string(self):
        # Present but blank → store "" (raw key / Alpaca style)
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"prefix": ""}
        )
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["prefix"], "")

    def test_header_name_not_applied_for_bearer_kind(self):
        # header_name in body but conn is bearer-kind → auth unchanged
        bearer = _bearer_conn()
        conn_out, _, err = _apply_connection_update(
            bearer, {"api_key": "sk-x"}, {"header_name": "X-Custom"}
        )
        self.assertIsNone(err)
        self.assertNotIn("header_name", conn_out.get("auth", {}))


class TestApplyConnectionUpdateCustomOAuth(unittest.TestCase):
    """Custom-oauth connections: authorize_url / token_url / scopes updates."""

    def setUp(self):
        self.conn = _custom_oauth_conn()
        self.secrets = {"client_id": "cid", "client_secret": "csecret"}

    def test_absent_oauth_urls_unchanged(self):
        conn_out, _, err = _apply_connection_update(
            self.conn, self.secrets, {"client_id": "new_cid"}
        )
        self.assertIsNone(err)
        oauth = conn_out["auth"]["oauth"]
        self.assertEqual(oauth["authorize_url"], "https://example.com/oauth/authorize")
        self.assertEqual(oauth["token_url"], "https://example.com/oauth/token")
        self.assertEqual(oauth["scopes"], ["read", "write"])

    @patch("app._validate_oauth_endpoint_url", return_value="")
    def test_authorize_url_replaces_when_present(self, mock_validate):
        body = {"authorize_url": "https://new.example.com/oauth/authorize"}
        conn_out, _, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNone(err)
        self.assertEqual(
            conn_out["auth"]["oauth"]["authorize_url"],
            "https://new.example.com/oauth/authorize",
        )
        # Existing token_url unchanged
        self.assertEqual(
            conn_out["auth"]["oauth"]["token_url"],
            "https://example.com/oauth/token",
        )

    @patch("app._validate_oauth_endpoint_url", return_value="Authorization URL must start with https://")
    def test_invalid_authorize_url_returns_error(self, mock_validate):
        body = {"authorize_url": "http://insecure.example.com/authorize"}
        _, _, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNotNone(err)
        self.assertIn("https://", err)

    @patch("app._validate_oauth_endpoint_url", return_value="")
    def test_scopes_replaced_when_present(self, mock_validate):
        body = {"authorize_url": "https://example.com/auth", "scopes": "read admin"}
        conn_out, _, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["oauth"]["scopes"], ["read", "admin"])

    @patch("app._validate_oauth_endpoint_url", return_value="")
    def test_scopes_split_on_commas(self, mock_validate):
        body = {"authorize_url": "https://example.com/auth", "scopes": "read,write,admin"}
        conn_out, _, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNone(err)
        self.assertEqual(conn_out["auth"]["oauth"]["scopes"], ["read", "write", "admin"])

    def test_oauth_url_update_ignored_when_conn_has_no_oauth_cfg(self):
        # Standard oauth2 (Google-style) has no conn["auth"]["oauth"] key
        standard_oauth_conn = _oauth_conn()  # auth = {"kind": "oauth2"}, no "oauth"
        body = {"authorize_url": "https://example.com/auth"}
        conn_out, _, err = _apply_connection_update(
            standard_oauth_conn, self.secrets, body
        )
        self.assertIsNone(err)
        # auth should be unchanged (no "oauth" key introduced)
        self.assertNotIn("oauth", conn_out.get("auth", {}))


class TestApplyConnectionUpdateApple(unittest.TestCase):
    """Apple-kind connections: apple_id and app_password merge."""

    def setUp(self):
        self.conn = _apple_conn()
        self.secrets = {"apple_id": "old@icloud.com", "app_password": "old-app-pw"}

    def test_absent_credentials_unchanged(self):
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, {})
        self.assertIsNone(err)
        self.assertEqual(sec_out["apple_id"], "old@icloud.com")
        self.assertEqual(sec_out["app_password"], "old-app-pw")

    def test_apple_id_replaces(self):
        _, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"apple_id": "new@icloud.com"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["apple_id"], "new@icloud.com")
        # app_password unchanged
        self.assertEqual(sec_out["app_password"], "old-app-pw")

    def test_app_password_via_apple_app_password_key(self):
        _, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"apple_app_password": "new-pw"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["app_password"], "new-pw")

    def test_app_password_via_app_password_key(self):
        _, sec_out, err = _apply_connection_update(
            self.conn, self.secrets, {"app_password": "new-pw-2"}
        )
        self.assertIsNone(err)
        self.assertEqual(sec_out["app_password"], "new-pw-2")


class TestApplyConnectionUpdateExtraHeaders(unittest.TestCase):
    """Extra static headers for custom connections."""

    def setUp(self):
        self.conn = _bearer_conn()
        self.secrets = {"api_key": "sk-x", "extra_headers": {"X-Custom": "old-value"}}

    def test_absent_extra_headers_unchanged(self):
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, {})
        self.assertIsNone(err)
        self.assertEqual(sec_out["extra_headers"], {"X-Custom": "old-value"})

    def test_new_extra_headers_replace_set(self):
        body = {"extra_headers": {"X-New": "new-value"}}
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNone(err)
        self.assertEqual(sec_out["extra_headers"], {"X-New": "new-value"})

    def test_clear_sentinel_removes_headers(self):
        body = {"extra_headers": {"-": ""}}
        _, sec_out, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNone(err)
        self.assertNotIn("extra_headers", sec_out)

    def test_invalid_header_name_returns_error(self):
        body = {"extra_headers": {"bad header!": "value"}}
        _, _, err = _apply_connection_update(self.conn, self.secrets, body)
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# Tests: HTTP endpoint via FastAPI TestClient
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    _HAS_TESTCLIENT = True
except ImportError:
    _HAS_TESTCLIENT = False


def _make_client() -> "TestClient":
    return TestClient(_vault_app.app, raise_server_exceptions=True)


def _admin_headers() -> dict:
    return {
        "X-Vault-Admin-Token": "test-admin-token",
        "Content-Type": "application/json",
    }


@unittest.skipUnless(_HAS_TESTCLIENT, "fastapi[testclient] not installed")
class TestAdminUpdateEndpointHTTP(unittest.TestCase):
    """HTTP-level tests for POST /api/admin/connections/{conn_id}/update."""

    def setUp(self):
        # Mock the module-level store and audit_log so we don't touch Redis.
        # Use patch.object because the module is registered as "app" in sys.modules
        # (not "_vault_app"), so patch("_vault_app.x") would fail.
        self._conn_data = _bearer_conn("OPENAI")
        self._secrets_data = {"api_key": "sk-OLD"}
        self._saved_calls: list = []

        def fake_get(cid):
            return dict(self._conn_data) if cid == "OPENAI" else None

        def fake_get_secrets(cid):
            return dict(self._secrets_data) if cid == "OPENAI" else {}

        def fake_save(conn_id, **kwargs):
            self._saved_calls.append({"conn_id": conn_id, **kwargs})

        self._patchers = [
            patch.object(_vault_app.store, "get", side_effect=fake_get),
            patch.object(_vault_app.store, "get_secrets", side_effect=fake_get_secrets),
            patch.object(_vault_app.store, "save", side_effect=fake_save),
            patch.object(_vault_app, "audit_log", MagicMock()),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

        self.client = _make_client()

    def test_missing_admin_token_returns_401(self):
        resp = self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"api_key": "sk-NEW"},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_admin_token_returns_401(self):
        resp = self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"api_key": "sk-NEW"},
            headers={
                "X-Vault-Admin-Token": "wrong-token",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(resp.status_code, 401)

    def test_non_json_content_type_returns_415(self):
        resp = self.client.post(
            "/api/admin/connections/OPENAI/update",
            content=b"api_key=sk-NEW",
            headers={
                "X-Vault-Admin-Token": "test-admin-token",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        self.assertEqual(resp.status_code, 415)

    def test_unknown_connection_returns_404(self):
        resp = self.client.post(
            "/api/admin/connections/NONEXISTENT/update",
            json={"api_key": "sk-NEW"},
            headers=_admin_headers(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_successful_update_returns_200_with_connection(self):
        # store.get is called twice (once to load, once for _conn_view return)
        # — make the second call also return data
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        resp = self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"label": "OpenAI Updated"},
            headers=_admin_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("connection", body)

    def test_store_save_called_with_merged_label(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"label": "New Label"},
            headers=_admin_headers(),
        )
        self.assertEqual(len(self._saved_calls), 1)
        self.assertEqual(self._saved_calls[0]["label"], "New Label")

    def test_blank_label_in_body_keeps_existing_label(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"label": ""},
            headers=_admin_headers(),
        )
        # blank label → falls back to conn's existing label
        self.assertEqual(self._saved_calls[0]["label"], "OpenAI")

    def test_blank_base_url_keeps_existing(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"base_url": ""},
            headers=_admin_headers(),
        )
        self.assertEqual(
            self._saved_calls[0]["base_url"],
            "https://api.openai.com",
        )

    def test_audit_log_called_on_success(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        with patch.object(_vault_app, "audit_log") as mock_audit:
            self.client.post(
                "/api/admin/connections/OPENAI/update",
                json={"label": "Updated"},
                headers=_admin_headers(),
            )
        mock_audit.assert_called_once_with("admin", "OPENAI", "connection_updated", "(via Discord)")

    def test_conn_id_is_normalized(self):
        """Lower-case or hyphenated IDs are normalized before lookup."""
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        resp = self.client.post(
            "/api/admin/connections/openai/update",   # lowercase in URL
            json={"label": "x"},
            headers=_admin_headers(),
        )
        # normalize_id("openai") → "OPENAI" → store.get should find it
        self.assertEqual(resp.status_code, 200)

    def test_description_absent_keeps_existing(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"api_key": "sk-NEW"},
            headers=_admin_headers(),
        )
        self.assertEqual(self._saved_calls[0]["description"], "Main OpenAI key")

    def test_description_present_and_blank_clears_it(self):
        """description key present with empty string = explicit clear (form semantics)."""
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"description": ""},
            headers=_admin_headers(),
        )
        self.assertEqual(self._saved_calls[0]["description"], "")

    def test_description_present_and_nonempty_updates_it(self):
        _vault_app.store.get.side_effect = lambda cid: dict(self._conn_data) if cid == "OPENAI" else None
        self.client.post(
            "/api/admin/connections/OPENAI/update",
            json={"description": "Updated description"},
            headers=_admin_headers(),
        )
        self.assertEqual(self._saved_calls[0]["description"], "Updated description")

    def test_non_dict_body_returns_400(self):
        resp = self.client.post(
            "/api/admin/connections/OPENAI/update",
            content=b'"just a string"',
            headers=_admin_headers(),
        )
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
