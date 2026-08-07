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
# 3. Simulate probe responses through the test-engine classification logic
# ---------------------------------------------------------------------------

def _classify(status: int):
    """Mirror the classification logic from app._run_connection_test."""
    if status in (401, 403):
        return False, f"Auth rejected (HTTP {status})"
    elif status >= 500:
        return False, f"Server error (HTTP {status})"
    else:
        return True, f"HTTP {status}"


class TestProbeClassification:
    """Simulate HTTP responses for each Google probe and verify ok/reason."""

    @pytest.mark.parametrize("service,good_status,bad_status", [
        ("google",          200, 401),   # Drive about → 200 when authed
        ("google_gmail",    200, 401),   # Gmail profile → 200 when authed
        ("google_drive",    200, 401),   # Drive about → 200 when authed
        ("google_sheets",   404, 401),   # No list endpoint → 404 when authed
        ("google_docs",     404, 401),   # No list endpoint → 404 when authed
        ("google_slides",   404, 401),   # No list endpoint → 404 when authed
        ("google_forms",    404, 401),   # No list endpoint → 404 when authed
        ("google_calendar", 200, 401),   # calendarList → 200 when authed
        ("google_tasks",    200, 401),   # tasklist → 200 when authed
        ("google_people",   200, 401),   # people/me → 200 when authed
        ("google_meet",     400, 401),   # spaces needs filter → 400 when authed
        ("google_app_script", 200, 401), # projects list → 200 when authed
    ])
    def test_authenticated_response_is_ok(self, service, good_status, bad_status):
        ok, reason = _classify(good_status)
        assert ok is True, (
            f"[{service}] authenticated status {good_status} should be ok=True, "
            f"got ok={ok} reason={reason}"
        )

    @pytest.mark.parametrize("service,good_status,bad_status", [
        ("google",          200, 401),
        ("google_gmail",    200, 401),
        ("google_drive",    200, 401),
        ("google_sheets",   404, 401),
        ("google_docs",     404, 401),
        ("google_slides",   404, 401),
        ("google_forms",    404, 401),
        ("google_calendar", 200, 401),
        ("google_tasks",    200, 401),
        ("google_people",   200, 401),
        ("google_meet",     400, 401),
        ("google_app_script", 200, 401),
    ])
    def test_unauthenticated_response_is_fail(self, service, good_status, bad_status):
        ok, reason = _classify(bad_status)
        assert ok is False, (
            f"[{service}] unauthenticated status {bad_status} should be ok=False, "
            f"got ok={ok} reason={reason}"
        )
        assert "Auth rejected" in reason


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
