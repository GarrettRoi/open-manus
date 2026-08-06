"""Tests for the VaultAdminClient used by the Discord /vault command group.

Covers:
  - Config: VaultClientError raised when VAULT_BASE_URL or VAULT_ADMIN_TOKEN missing
  - Headers: X-Vault-Admin-Token sent on every request; Content-Type: application/json on POST
  - HTTP errors: 401, 404, 409, 415, 5xx all raise VaultClientError with clean messages
  - Network errors: httpx.RequestError raises VaultClientError
  - overview: result is cached for _OVERVIEW_CACHE_TTL seconds; second call uses cache
  - overview force=True bypasses cache
  - invalidate_cache: forces next overview() to fetch fresh data
  - connection_ids / agent_names: derived from overview cache
  - add: POST /api/admin/connections; cache invalidated; returns parsed body
  - update: POST /api/admin/connections/{id}/update; cache invalidated
  - delete: POST /api/admin/connections/{id}/delete; cache invalidated
  - set_grant: POST /api/admin/grants with correct payload
  - connect_link: POST /api/admin/connections/{id}/connect-link; returns url field
  - connect_link: raises VaultClientError when vault returns empty url
  - Secret safety: credentials never appear in log output from the client
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so plugin module is importable
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# httpx must be importable (it's a core dep — always present)
import httpx

from plugins.platforms.discord.vault_admin_client import (
    VaultAdminClient,
    VaultClientError,
    _OVERVIEW_CACHE_TTL,
    _raise_for_status,
)


# ---------------------------------------------------------------------------
# Async test helper
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine synchronously in a fresh event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# HTTP response stub
# ---------------------------------------------------------------------------

def _fake_response(
    status_code: int,
    body: Any = None,
    *,
    headers: Optional[Dict[str, str]] = None,
) -> MagicMock:
    """Build a minimal httpx.Response-like mock."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=body or {})
    resp.headers = headers or {}
    return resp


def _async_client_context(responses: list) -> MagicMock:
    """Return a mock that works as `async with httpx.AsyncClient(...) as client`.

    *responses* is a list of (method, response) pairs consumed in order.
    """
    client_mock = MagicMock()
    responses_iter = iter(responses)

    async def _request(method, url, **kwargs):
        resp = next(responses_iter)
        return resp

    client_mock.get = AsyncMock(side_effect=lambda url, **kw: next(iter(responses)))
    client_mock.post = AsyncMock(side_effect=lambda url, **kw: next(iter(responses)))

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client_mock)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client_mock


# ---------------------------------------------------------------------------
# More flexible stub: queue multiple responses
# ---------------------------------------------------------------------------

class _AsyncClientStub:
    """Stub for httpx.AsyncClient that returns pre-queued responses."""

    def __init__(self, responses: list):
        # Each item is an httpx.Response-like mock
        self._queue = list(responses)
        self.requests: list = []   # (method, url, kwargs) tuples recorded

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return self._queue.pop(0)

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return self._queue.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _patch_client(*responses):
    """Context manager that patches httpx.AsyncClient with _AsyncClientStub."""
    stub = _AsyncClientStub(list(responses))
    return patch(
        "plugins.platforms.discord.vault_admin_client.httpx.AsyncClient",
        return_value=stub,
    ), stub


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_OVERVIEW_BODY = {
    "connections": [
        {"id": "OPENAI", "service": "openai", "label": "OpenAI",
         "auth_kind": "bearer", "status": "ready",
         "base_url": "https://api.openai.com",
         "description": "", "skill_description": "",
         "created_at": "", "updated_at": ""},
        {"id": "ELEVENLABS", "service": "elevenlabs", "label": "ElevenLabs",
         "auth_kind": "header", "status": "ready",
         "base_url": "https://api.elevenlabs.io",
         "description": "", "skill_description": "",
         "created_at": "", "updated_at": ""},
    ],
    "agents": ["harmony", "samantha", "addison"],
    "grants": {"harmony": ["OPENAI"], "samantha": [], "addison": []},
    "catalog": {},
    "redirect_uri": "https://vault.test/oauth/callback",
    "public_url_missing": False,
}


def _env_with_vault(base_url: str = "https://vault.test",
                    token: str = "test-token") -> dict:
    return {"VAULT_BASE_URL": base_url, "VAULT_ADMIN_TOKEN": token}


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestVaultClientConfig(unittest.TestCase):

    def test_missing_base_url_raises(self):
        client = VaultAdminClient()
        with patch.dict(os.environ, {"VAULT_BASE_URL": "", "VAULT_ADMIN_TOKEN": "tok"}):
            with self.assertRaises(VaultClientError) as ctx:
                client._base_url()
        self.assertIn("VAULT_BASE_URL", str(ctx.exception))

    def test_missing_token_raises(self):
        client = VaultAdminClient()
        with patch.dict(os.environ, {"VAULT_BASE_URL": "https://v.test", "VAULT_ADMIN_TOKEN": ""}):
            with self.assertRaises(VaultClientError) as ctx:
                client._token()
        self.assertIn("VAULT_ADMIN_TOKEN", str(ctx.exception))

    def test_base_url_trailing_slash_stripped(self):
        client = VaultAdminClient()
        with patch.dict(os.environ, {"VAULT_BASE_URL": "https://v.test/"}):
            self.assertEqual(client._base_url(), "https://v.test")

    def test_headers_include_admin_token(self):
        client = VaultAdminClient()
        with patch.dict(os.environ, _env_with_vault(token="my-secret-token")):
            headers = client._headers()
        self.assertEqual(headers["X-Vault-Admin-Token"], "my-secret-token")
        self.assertEqual(headers["Content-Type"], "application/json")


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------

class TestRaiseForStatus(unittest.TestCase):

    def _resp(self, status: int, detail: str = "") -> MagicMock:
        r = MagicMock()
        r.status_code = status
        r.json = MagicMock(return_value={"detail": detail} if detail else {})
        return r

    def test_401_raises_auth_error(self):
        with self.assertRaises(VaultClientError) as ctx:
            _raise_for_status(self._resp(401), "/test")
        self.assertIn("authentication failed", str(ctx.exception).lower())

    def test_404_includes_path_in_message(self):
        with self.assertRaises(VaultClientError) as ctx:
            _raise_for_status(self._resp(404, "Connection not found"), "/api/admin/connections/X")
        msg = str(ctx.exception)
        self.assertIn("404", msg)
        self.assertIn("Connection not found", msg)

    def test_409_uses_detail_from_body(self):
        with self.assertRaises(VaultClientError) as ctx:
            _raise_for_status(self._resp(409, "A connection named 'X' already exists"), "/test")
        self.assertIn("already exists", str(ctx.exception))

    def test_415_raises_content_type_error(self):
        with self.assertRaises(VaultClientError) as ctx:
            _raise_for_status(self._resp(415), "/test")
        self.assertIn("content type", str(ctx.exception).lower())

    def test_500_includes_status_code(self):
        with self.assertRaises(VaultClientError) as ctx:
            _raise_for_status(self._resp(500, "Internal error"), "/test")
        self.assertIn("500", str(ctx.exception))


# ---------------------------------------------------------------------------
# overview: caching and cache invalidation
# ---------------------------------------------------------------------------

class TestOverviewCaching(unittest.IsolatedAsyncioTestCase):

    async def _client_with_overview(self, n_responses: int = 2) -> tuple:
        """Return (VaultAdminClient, stub) pre-loaded with n overview responses."""
        responses = [
            _fake_response(200, _OVERVIEW_BODY)
            for _ in range(n_responses)
        ]
        return VaultAdminClient(), responses

    async def test_overview_fetches_on_first_call(self):
        client = VaultAdminClient()
        ok_resp = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub([ok_resp])
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                result = await client.overview()
        self.assertEqual(len(stub.requests), 1)
        self.assertIn("connections", result)

    async def test_overview_cached_on_second_call(self):
        client = VaultAdminClient()
        ok_resp = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub([ok_resp])
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                await client.overview()
                await client.overview()   # second call — should use cache
        # Only ONE real HTTP request should have been made
        self.assertEqual(len(stub.requests), 1, "second overview() must use cached result")

    async def test_overview_force_bypasses_cache(self):
        client = VaultAdminClient()
        resp1 = _fake_response(200, _OVERVIEW_BODY)
        resp2 = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub([resp1, resp2])
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                await client.overview()
                await client.overview(force=True)  # force re-fetch
        self.assertEqual(len(stub.requests), 2, "force=True must bypass the cache")

    async def test_overview_cache_expires_after_ttl(self):
        client = VaultAdminClient()
        resp1 = _fake_response(200, _OVERVIEW_BODY)
        resp2 = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub([resp1, resp2])
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                await client.overview()
                # Simulate TTL expiry by back-dating the cache timestamp
                client._overview_cache_at = time.monotonic() - (_OVERVIEW_CACHE_TTL + 1)
                await client.overview()
        self.assertEqual(len(stub.requests), 2, "expired cache must trigger a fresh fetch")

    async def test_invalidate_cache_forces_refetch(self):
        client = VaultAdminClient()
        resp1 = _fake_response(200, _OVERVIEW_BODY)
        resp2 = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub([resp1, resp2])
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                await client.overview()
                client.invalidate_cache()
                await client.overview()
        self.assertEqual(len(stub.requests), 2)


# ---------------------------------------------------------------------------
# connection_ids and agent_names
# ---------------------------------------------------------------------------

class TestDerivedLists(unittest.IsolatedAsyncioTestCase):

    async def test_connection_ids_sorted(self):
        client = VaultAdminClient()
        resp = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub([resp])
            with patch.dict(os.environ, _env_with_vault()):
                ids = await client.connection_ids()
        self.assertEqual(ids, sorted(ids))
        self.assertIn("OPENAI", ids)
        self.assertIn("ELEVENLABS", ids)

    async def test_agent_names_matches_overview(self):
        client = VaultAdminClient()
        resp = _fake_response(200, _OVERVIEW_BODY)
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub([resp])
            with patch.dict(os.environ, _env_with_vault()):
                names = await client.agent_names()
        self.assertEqual(names, _OVERVIEW_BODY["agents"])


# ---------------------------------------------------------------------------
# add / update / delete / set_grant / connect_link
# ---------------------------------------------------------------------------

class TestMutationMethods(unittest.IsolatedAsyncioTestCase):

    async def _call(self, method_name: str, *args, responses=None, **kwargs):
        """Call a VaultAdminClient method with a pre-seeded stub."""
        client = VaultAdminClient()
        if responses is None:
            responses = [_fake_response(200, {"ok": True})]
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = _AsyncClientStub(responses)
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                result = await getattr(client, method_name)(*args, **kwargs)
        return result, stub

    # ── add ──────────────────────────────────────────────────────────────────

    async def test_add_posts_to_correct_endpoint(self):
        conn_resp = {"connection": {"id": "OPENAI"}, "needs_login": False}
        _, stub = await self._call(
            "add",
            {"service": "openai", "name": "openai", "api_key": "sk-x",
             "base_url": "https://api.openai.com"},
            responses=[_fake_response(200, conn_resp)],
        )
        method, url, kwargs = stub.requests[0]
        self.assertEqual(method, "POST")
        self.assertIn("/api/admin/connections", url)
        self.assertNotIn("update", url)

    async def test_add_sends_admin_token_header(self):
        conn_resp = {"connection": {"id": "X"}, "needs_login": False}
        _, stub = await self._call(
            "add",
            {"service": "openai", "name": "x", "api_key": "sk-x",
             "base_url": "https://api.openai.com"},
            responses=[_fake_response(200, conn_resp)],
        )
        _, _, kwargs = stub.requests[0]
        self.assertEqual(kwargs["headers"]["X-Vault-Admin-Token"], "test-token")

    async def test_add_invalidates_cache(self):
        client = VaultAdminClient()
        # Prime the cache
        client._overview_cache = _OVERVIEW_BODY
        client._overview_cache_at = time.monotonic()
        conn_resp = {"connection": {"id": "NEW"}, "needs_login": False}
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub([_fake_response(200, conn_resp)])
            with patch.dict(os.environ, _env_with_vault()):
                await client.add({"name": "new", "service": "openai", "api_key": "x",
                                   "base_url": "https://api.openai.com"})
        self.assertIsNone(client._overview_cache, "add() must invalidate the overview cache")

    # ── update ───────────────────────────────────────────────────────────────

    async def test_update_posts_to_correct_endpoint(self):
        _, stub = await self._call(
            "update", "OPENAI", {"label": "Updated"},
            responses=[_fake_response(200, {"connection": {"id": "OPENAI"}})],
        )
        _, url, _ = stub.requests[0]
        self.assertIn("/api/admin/connections/OPENAI/update", url)

    async def test_update_sends_body_as_json(self):
        _, stub = await self._call(
            "update", "OPENAI", {"label": "New Label"},
            responses=[_fake_response(200, {"connection": {"id": "OPENAI"}})],
        )
        _, _, kwargs = stub.requests[0]
        self.assertEqual(kwargs["json"]["label"], "New Label")

    async def test_update_invalidates_cache(self):
        client = VaultAdminClient()
        client._overview_cache = _OVERVIEW_BODY
        client._overview_cache_at = time.monotonic()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub(
                [_fake_response(200, {"connection": {"id": "OPENAI"}})]
            )
            with patch.dict(os.environ, _env_with_vault()):
                await client.update("OPENAI", {"label": "x"})
        self.assertIsNone(client._overview_cache)

    async def test_update_raises_on_404(self):
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub(
                [_fake_response(404, {"detail": "Connection not found"})]
            )
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError):
                    await client.update("NONEXISTENT", {"label": "x"})

    # ── delete ───────────────────────────────────────────────────────────────

    async def test_delete_posts_to_correct_endpoint(self):
        _, stub = await self._call(
            "delete", "OPENAI",
            responses=[_fake_response(200, {"ok": True})],
        )
        _, url, _ = stub.requests[0]
        self.assertIn("/api/admin/connections/OPENAI/delete", url)

    async def test_delete_invalidates_cache(self):
        client = VaultAdminClient()
        client._overview_cache = _OVERVIEW_BODY
        client._overview_cache_at = time.monotonic()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub([_fake_response(200, {"ok": True})])
            with patch.dict(os.environ, _env_with_vault()):
                await client.delete("OPENAI")
        self.assertIsNone(client._overview_cache)

    # ── set_grant ────────────────────────────────────────────────────────────

    async def test_set_grant_posts_correct_payload_grant(self):
        _, stub = await self._call(
            "set_grant", "harmony", "OPENAI", True,
            responses=[_fake_response(200, {"ok": True})],
        )
        _, url, kwargs = stub.requests[0]
        self.assertIn("/api/admin/grants", url)
        self.assertEqual(kwargs["json"]["agent"], "harmony")
        self.assertEqual(kwargs["json"]["conn_id"], "OPENAI")
        self.assertTrue(kwargs["json"]["granted"])

    async def test_set_grant_posts_correct_payload_revoke(self):
        _, stub = await self._call(
            "set_grant", "harmony", "OPENAI", False,
            responses=[_fake_response(200, {"ok": True})],
        )
        _, _, kwargs = stub.requests[0]
        self.assertFalse(kwargs["json"]["granted"])

    async def test_set_grant_raises_on_bad_agent(self):
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub(
                [_fake_response(400, {"detail": "Unknown agent"})]
            )
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError) as ctx:
                    await client.set_grant("unknown-agent", "OPENAI", True)
        # 400 maps to the generic "HTTP 400" branch
        self.assertIn("400", str(ctx.exception))

    # ── connect_link ─────────────────────────────────────────────────────────

    async def test_connect_link_returns_url(self):
        oauth_url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"
        result, stub = await self._call(
            "connect_link", "GOOGLE",
            responses=[_fake_response(200, {"url": oauth_url, "redirect_uri": "https://v.test/cb"})],
        )
        self.assertEqual(result, oauth_url)

    async def test_connect_link_posts_to_correct_endpoint(self):
        _, stub = await self._call(
            "connect_link", "GOOGLE",
            responses=[_fake_response(200, {"url": "https://example.com/auth"})],
        )
        _, url, _ = stub.requests[0]
        self.assertIn("/api/admin/connections/GOOGLE/connect-link", url)

    async def test_connect_link_raises_when_url_missing(self):
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub(
                [_fake_response(200, {"url": "", "redirect_uri": ""})]
            )
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError):
                    await client.connect_link("GOOGLE")

    async def test_connect_link_raises_on_non_oauth_connection(self):
        """Vault returns 400 for non-OAuth connections."""
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = _AsyncClientStub(
                [_fake_response(400, {"detail": "Not an OAuth service"})]
            )
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError) as ctx:
                    await client.connect_link("OPENAI")
        self.assertIn("400", str(ctx.exception))


# ---------------------------------------------------------------------------
# Network error handling
# ---------------------------------------------------------------------------

class TestNetworkErrors(unittest.IsolatedAsyncioTestCase):

    async def test_get_raises_on_connect_error(self):
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = MagicMock()
            stub.__aenter__ = AsyncMock(return_value=stub)
            stub.__aexit__ = AsyncMock(return_value=False)
            stub.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError) as ctx:
                    await client.overview()
        self.assertIn("Could not reach vault", str(ctx.exception))

    async def test_post_raises_on_timeout(self):
        client = VaultAdminClient()
        with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
            stub = MagicMock()
            stub.__aenter__ = AsyncMock(return_value=stub)
            stub.__aexit__ = AsyncMock(return_value=False)
            stub.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_cls.return_value = stub
            with patch.dict(os.environ, _env_with_vault()):
                with self.assertRaises(VaultClientError) as ctx:
                    await client.add({"name": "x", "service": "openai",
                                      "api_key": "sk-x", "base_url": "https://api.openai.com"})
        self.assertIn("Could not reach vault", str(ctx.exception))


# ---------------------------------------------------------------------------
# Secret safety: credentials must not appear in log output
# ---------------------------------------------------------------------------

class TestSecretSafety(unittest.IsolatedAsyncioTestCase):
    """Verify no credential values leak through the client's own log calls."""

    async def test_no_credential_values_in_logs(self):
        sensitive_token = "SUPER_SECRET_ADMIN_TOKEN_12345"
        client = VaultAdminClient()
        conn_resp = {"connection": {"id": "OPENAI"}, "needs_login": False}

        log_records: list = []
        handler = logging.handlers_list = []

        class _Capture(logging.Handler):
            def emit(self, record):
                log_records.append(self.format(record))

        capture = _Capture()
        vault_logger = logging.getLogger("plugins.platforms.discord.vault_admin_client")
        vault_logger.addHandler(capture)
        vault_logger.setLevel(logging.DEBUG)

        try:
            with patch("plugins.platforms.discord.vault_admin_client.httpx.AsyncClient") as mock_cls:
                mock_cls.return_value = _AsyncClientStub([_fake_response(200, conn_resp)])
                with patch.dict(os.environ,
                                {"VAULT_BASE_URL": "https://vault.test",
                                 "VAULT_ADMIN_TOKEN": sensitive_token}):
                    await client.add({"name": "openai", "service": "openai",
                                      "api_key": "sk-ACTUAL-KEY",
                                      "base_url": "https://api.openai.com"})
        finally:
            vault_logger.removeHandler(capture)

        for record_text in log_records:
            self.assertNotIn(
                sensitive_token, record_text,
                f"Admin token leaked into log: {record_text!r}",
            )
            # api_key is in the body dict passed by caller, not by the client;
            # the client itself must not echo body contents in its logs.
            self.assertNotIn(
                "sk-ACTUAL-KEY", record_text,
                f"API key leaked into log: {record_text!r}",
            )


if __name__ == "__main__":
    unittest.main()
