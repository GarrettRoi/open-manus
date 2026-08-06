"""Unit tests for connections.build_auth.

These tests exercise the credential-injection logic in isolation — no Redis,
no HTTP, no Fernet.  Run from services/vault/:

    pytest tests/test_build_auth.py -v
"""
import sys
import os

# Allow importing vault modules without installing them as a package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from connections import build_auth, AuthInjectionError
import pytest


# ---------------------------------------------------------------------------
# Alpaca-style: custom header name, empty prefix, one extra secret header
# ---------------------------------------------------------------------------

class TestAlpacaStyle:
    """APCA-API-KEY-ID: <raw-key> + APCA-API-SECRET-KEY: <secret>
    No Authorization header must be emitted."""

    CONN = {
        "auth": {
            "kind": "header",
            "header_name": "APCA-API-KEY-ID",
            "prefix": "",          # empty string = raw key, no prefix
        }
    }
    SECRETS = {
        "api_key": "KEYID123",
        "extra_headers": {
            "APCA-API-SECRET-KEY": "SECRET456",
        },
    }

    def test_primary_header_name(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert "APCA-API-KEY-ID" in result["headers"]

    def test_primary_header_value_is_raw_key(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"]["APCA-API-KEY-ID"] == "KEYID123"

    def test_no_authorization_header(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert "Authorization" not in result["headers"], (
            "Authorization header must NOT be emitted for custom-header-name connections"
        )

    def test_extra_secret_header_present(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"].get("APCA-API-SECRET-KEY") == "SECRET456"

    def test_exact_header_set(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert set(result["headers"].keys()) == {"APCA-API-KEY-ID", "APCA-API-SECRET-KEY"}, (
            f"Unexpected headers: {result['headers']}"
        )

    def test_no_params(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["params"] == {}


# ---------------------------------------------------------------------------
# Alpaca-style: None prefix stored (legacy records that pre-date explicit
# storage) — should behave identically to empty-string prefix.
# ---------------------------------------------------------------------------

class TestAlpacaStyleNonePrefix:
    """A record where prefix key is absent from auth dict (legacy)."""

    CONN = {
        "auth": {
            "kind": "header",
            "header_name": "APCA-API-KEY-ID",
            # prefix key intentionally absent
        }
    }
    SECRETS = {"api_key": "KEYID999"}

    def test_raw_key_no_prefix(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"]["APCA-API-KEY-ID"] == "KEYID999"

    def test_no_authorization_header(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert "Authorization" not in result["headers"]


# ---------------------------------------------------------------------------
# Bearer-style regression: existing standard connections must keep working
# ---------------------------------------------------------------------------

class TestBearerStyle:
    """Standard bearer: Authorization: Bearer <key>"""

    CONN = {
        "auth": {
            "kind": "header",
            "header_name": "Authorization",
            "prefix": "Bearer ",   # trailing space is intentional
        }
    }
    SECRETS = {"api_key": "sk-abc123"}

    def test_authorization_header_present(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert "Authorization" in result["headers"]

    def test_bearer_prefix(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"]["Authorization"] == "Bearer sk-abc123"

    def test_exact_header_set(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert set(result["headers"].keys()) == {"Authorization"}

    def test_no_params(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["params"] == {}


# ---------------------------------------------------------------------------
# Bearer-style with extra headers
# ---------------------------------------------------------------------------

class TestBearerWithExtraHeaders:
    CONN = {
        "auth": {
            "kind": "header",
            "header_name": "Authorization",
            "prefix": "Bearer ",
        }
    }
    SECRETS = {
        "api_key": "sk-xyz",
        "extra_headers": {"X-Custom-Client": "manus"},
    }

    def test_bearer_plus_extra(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"]["Authorization"] == "Bearer sk-xyz"
        assert result["headers"]["X-Custom-Client"] == "manus"

    def test_extra_cannot_override_auth(self):
        """Extra header named 'Authorization' must be silently dropped."""
        conn = {
            "auth": {
                "kind": "header",
                "header_name": "Authorization",
                "prefix": "Bearer ",
            }
        }
        secrets = {
            "api_key": "real-key",
            "extra_headers": {"Authorization": "malicious-override"},
        }
        result = build_auth(conn, secrets)
        assert result["headers"]["Authorization"] == "Bearer real-key"


# ---------------------------------------------------------------------------
# Fallback: header_name stored as "" (legacy record, pre-explicit-storage)
# ---------------------------------------------------------------------------

class TestHeaderNameFallback:
    """Empty stored header_name falls back to Authorization."""

    CONN = {"auth": {"kind": "header", "header_name": "", "prefix": "Token "}}
    SECRETS = {"api_key": "tok-999"}

    def test_falls_back_to_authorization(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert "Authorization" in result["headers"]
        assert result["headers"]["Authorization"] == "Token tok-999"


# ---------------------------------------------------------------------------
# Builtin bearer kind (not header kind) — unchanged
# ---------------------------------------------------------------------------

class TestBuiltinBearer:
    CONN = {"auth": {"kind": "bearer"}}
    SECRETS = {"api_key": "builtin-key"}

    def test_bearer(self):
        result = build_auth(self.CONN, self.SECRETS)
        assert result["headers"]["Authorization"] == "Bearer builtin-key"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrors:
    def test_missing_api_key_header_kind(self):
        with pytest.raises(AuthInjectionError, match="No API key"):
            build_auth({"auth": {"kind": "header", "header_name": "X-Key", "prefix": ""}}, {})

    def test_missing_api_key_bearer_kind(self):
        with pytest.raises(AuthInjectionError, match="No API key"):
            build_auth({"auth": {"kind": "bearer"}}, {})

    def test_unknown_kind(self):
        with pytest.raises(AuthInjectionError, match="Unknown auth kind"):
            build_auth({"auth": {"kind": "magic"}}, {"api_key": "x"})
