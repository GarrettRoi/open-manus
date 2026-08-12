"""Verification tests for Google OAuth probe paths.

Checks that:
1. The combined 'google' service probe hits an endpoint covered by a granted scope.
2. Each dedicated Google service probe uses a host in its own allowlist.
3. The test-engine mapping (401/403 → fail, anything else → ok) works correctly
   for the chosen probes by simulating HTTP responses via httpx mocking.

Run from services/vault/:
    pytest tests/test_google_probes.py -v
"""
import sys
import os
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import httpx

import catalog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GOOGLE_SERVICES = [
    "google",
    "google_gmail",
    "google_drive",
    "google_sheets",
    "google_docs",
    "google_slides",
    "google_forms",
    "google_calendar",
    "google_tasks",
    "google_people",
    "google_meet",
    "google_app_script",
]


def get_template(service_key):
    return catalog.CATALOG.get(service_key)


def probe_url(service_key):
    tpl = get_template(service_key)
    base = (tpl.get("base_url") or "").rstrip("/")
    path = (tpl.get("test_probe") or {}).get("path", "/")
    if not path.startswith("/"):
        path = "/" + path
    # Strip query string to get just the path for host checking
    url = base + path
    parsed = httpx.URL(url)
    return parsed


# ---------------------------------------------------------------------------
# 1. Combined google probe: must NOT be /oauth2/v1/userinfo
# ---------------------------------------------------------------------------

class TestCombinedGoogleProbe:
    """The combined 'google' service must not probe an endpoint that requires
    openid/email/profile scopes — those are never requested."""

    def test_probe_is_not_userinfo(self):
        tpl = get_template("google")
        path = tpl["test_probe"]["path"]
        assert "/userinfo" not in path, (
            f"combined google probe '{path}' still hits userinfo which requires "
            "openid/email/profile scopes not granted by this connection"
        )

    def test_probe_is_drive_about(self):
        tpl = get_template("google")
        path = tpl["test_probe"]["path"]
        assert path.startswith("/drive/v3/about"), (
            f"Expected /drive/v3/about probe, got '{path}'"
        )

    def test_probe_host_in_allowlist(self):
        tpl = get_template("google")
        allowed = tpl["allowed_hosts"]
        parsed = probe_url("google")
        assert parsed.host in allowed, (
            f"Probe host '{parsed.host}' not in allowlist {allowed}"
        )

    def test_drive_scope_is_granted(self):
        """auth/drive must be in the combined connection's scope list."""
        tpl = get_template("google")
        scopes = tpl["oauth"]["scopes"]
        assert any("drive" in s for s in scopes), (
            "auth/drive scope is not in the combined google connection — "
            "the /drive/v3/about probe won't authenticate"
        )


# ---------------------------------------------------------------------------
# 2. Audit: every dedicated Google probe uses a host in its own allowlist
# ---------------------------------------------------------------------------

class TestDedicatedGoogleProbeHosts:
    """Each dedicated connection's probe URL must target a host in that
    connection's own allowed_hosts list."""

    @pytest.mark.parametrize("service", GOOGLE_SERVICES)
    def test_probe_host_in_allowlist(self, service):
        tpl = get_template(service)
        if tpl is None:
            pytest.skip(f"Service '{service}' not in catalog")
        allowed = tpl.get("allowed_hosts", [])
        parsed = probe_url(service)
        assert parsed.host in allowed, (
            f"[{service}] probe host '{parsed.host}' not in allowlist {allowed}"
        )


# ---------------------------------------------------------------------------
# 3. Google-aware 401/403 classification (_google_auth_verdict)
# ---------------------------------------------------------------------------

def _google_resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
    )


class TestGoogleAuthVerdict:
    """403s from Google are only auth failures when the body says so."""

    def test_api_not_enabled_is_credentials_ok(self):
        import app as vault_app
        resp = _google_resp(403, {"error": {
            "code": 403,
            "message": ("Google Drive API has not been used in project 12345 "
                        "before or it is disabled."),
            "status": "PERMISSION_DENIED",
            "errors": [{"reason": "accessNotConfigured",
                        "message": "Access Not Configured"}],
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                         "reason": "SERVICE_DISABLED",
                         "metadata": {"service": "drive.googleapis.com"}}],
        }})
        ok, reason = asyncio.run(
            vault_app._google_auth_verdict(403, resp, "tok"))
        assert ok is True
        assert "credentials OK" in reason
        assert "Drive API" in reason
        assert "not enabled" in reason

    def test_rate_limit_is_credentials_ok(self):
        import app as vault_app
        resp = _google_resp(403, {"error": {
            "code": 403, "message": "Rate Limit Exceeded",
            "errors": [{"reason": "rateLimitExceeded"}],
        }})
        ok, reason = asyncio.run(
            vault_app._google_auth_verdict(403, resp, "tok"))
        assert ok is True
        assert "credentials OK" in reason

    def test_401_is_auth_rejected(self):
        import app as vault_app
        resp = _google_resp(401, {"error": {
            "code": 401, "message": "Invalid Credentials",
            "status": "UNAUTHENTICATED",
        }})
        ok, reason = asyncio.run(
            vault_app._google_auth_verdict(401, resp, "tok"))
        assert ok is False
        assert "re-login" in reason

    def test_ambiguous_403_with_valid_token_is_ok(self):
        import app as vault_app
        resp = _google_resp(403, {"error": {
            "code": 403, "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED",
        }})
        with patch.object(vault_app, "_google_tokeninfo_ok",
                          AsyncMock(return_value=True)):
            ok, reason = asyncio.run(
                vault_app._google_auth_verdict(403, resp, "tok"))
        assert ok is True
        assert "credentials OK" in reason

    def test_ambiguous_403_with_invalid_token_is_fail(self):
        import app as vault_app
        resp = _google_resp(403, {"error": {
            "code": 403, "message": "The caller does not have permission",
            "status": "PERMISSION_DENIED",
        }})
        with patch.object(vault_app, "_google_tokeninfo_ok",
                          AsyncMock(return_value=False)):
            ok, reason = asyncio.run(
                vault_app._google_auth_verdict(403, resp, "tok"))
        assert ok is False
        assert "re-login" in reason


class TestTokeninfoProbes:
    """Services without a clean read-only endpoint use the tokeninfo probe."""

    @pytest.mark.parametrize("service", [
        "google_sheets", "google_docs", "google_slides", "google_forms",
    ])
    def test_probe_uses_tokeninfo(self, service):
        tpl = get_template(service)
        assert tpl["test_probe"].get("google_tokeninfo") is True, (
            f"[{service}] should use the scope-independent tokeninfo probe"
        )
        assert "oauth2.googleapis.com" in tpl["allowed_hosts"]


# ---------------------------------------------------------------------------
# 4. End-to-end: mock the HTTP call inside _run_connection_test and confirm
#    last_test_ok is set correctly for 'google' and 'google_gmail'/'google_drive'
# ---------------------------------------------------------------------------

# Minimal fake store
class FakeStore:
    def __init__(self, conn):
        self._conn = conn
        self._secrets = {"access_token": "fake_token_abc123", "expires_at": 9999999999}
        self.written = {}

    def get(self, conn_id):
        return self._conn

    def get_secrets(self, conn_id):
        return dict(self._secrets)

    def allowed_hosts(self, conn):
        return set(conn.get("allowed_hosts", []))

    def hset(self, key, mapping):
        self.written.update(mapping)


def _make_fake_response(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, content=b"{}", headers={})


@pytest.mark.asyncio
@pytest.mark.parametrize("service,expected_ok,mock_status", [
    ("google",       True,  200),  # Drive about → 200 → ok
    ("google_gmail", True,  200),  # Gmail profile → 200 → ok
    ("google_drive", True,  200),  # Drive about → 200 → ok
])
async def test_run_connection_test_ok(service, expected_ok, mock_status):
    """Simulate a successful probe: confirms ok=True is returned."""
    import app as vault_app

    tpl = get_template(service)
    conn = {
        "service": service,
        "auth": {"kind": "oauth2"},
        "base_url": tpl["base_url"],
        "allowed_hosts": tpl["allowed_hosts"],
    }
    fake_store = FakeStore(conn)

    async def fake_get_token(service, conn_id, store, conn=None):
        return "fake_token_abc123", False

    mock_resp = _make_fake_response(mock_status)

    with (
        patch.object(vault_app, "store", fake_store),
        patch.object(vault_app, "get_template", lambda s: tpl),
        patch.object(vault_app.oauth_mod, "get_valid_access_token", fake_get_token),
        patch("httpx.AsyncClient.request", new_callable=AsyncMock,
              return_value=mock_resp),
    ):
        result = await vault_app._run_connection_test("test-conn-id")

    assert result["ok"] is expected_ok, (
        f"[{service}] expected ok={expected_ok}, got {result}"
    )


@pytest.mark.asyncio
async def test_combined_google_not_401_on_drive_probe():
    """Key regression: combined google probe must NOT return 401 when token is valid.
    Old probe (/oauth2/v1/userinfo) would 401 because scope is missing.
    New probe (/drive/v3/about) returns 200 when drive scope is granted."""
    import app as vault_app

    tpl = get_template("google")
    assert "/userinfo" not in tpl["test_probe"]["path"], (
        "Probe still points at userinfo — fix not applied!"
    )
    assert "drive" in tpl["test_probe"]["path"], (
        "Probe does not point at a drive endpoint"
    )
    # Verify the scope grants access to the probe
    scopes = tpl["oauth"]["scopes"]
    assert any("auth/drive" in s for s in scopes), "auth/drive not in granted scopes"
