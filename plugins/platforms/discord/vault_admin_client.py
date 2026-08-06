"""Vault admin HTTP client for the Discord /vault command group.

Calls the vault's admin JSON API (X-Vault-Admin-Token auth) on behalf of
the Discord adapter.  Lives in a separate file (not adapter.py) so upstream
engine syncs cannot silently delete it.

Configuration (read at call time, not at import):
    VAULT_BASE_URL      Public URL of the vault service, e.g.
                        https://vault.up.railway.app
    VAULT_ADMIN_TOKEN   Admin token (same env var the vault reads as
                        VAULT_ADMIN_TOKEN).  Must match the vault's
                        configured secret.

Secret-safety contract:
    - Response bodies are never logged; only status codes and connection IDs.
    - Body fields named *key*, *secret*, *password*, *token* are never
      included in log output from this module.
    - Callers must not pass raw credentials to any logging call.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# How long (seconds) to cache /api/admin/overview before re-fetching.
# Autocomplete callbacks run inside Discord's 3-second window; cache prevents
# network I/O on every keystroke while keeping data reasonably fresh.
_OVERVIEW_CACHE_TTL = 30.0

# Per-request HTTP timeout for vault API calls (seconds).
_REQUEST_TIMEOUT = 20.0


class VaultClientError(Exception):
    """Raised when the vault admin API returns an error or is unreachable.

    The message is safe to show to the Discord owner (ephemeral only).
    It never contains raw credential values.
    """


class VaultAdminClient:
    """Thin async client for the vault's JSON admin API.

    Instantiate once per adapter lifetime (or per /vault invocation — it is
    stateless except for the overview cache).  All methods are coroutines.
    """

    def __init__(self) -> None:
        self._overview_cache: Optional[Dict[str, Any]] = None
        self._overview_cache_at: float = 0.0

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _base_url() -> str:
        url = os.getenv("VAULT_BASE_URL", "").strip().rstrip("/")
        if not url:
            raise VaultClientError(
                "VAULT_BASE_URL is not configured on this agent. "
                "Set it in Railway environment variables."
            )
        return url

    @staticmethod
    def _token() -> str:
        token = os.getenv("VAULT_ADMIN_TOKEN", "").strip()
        if not token:
            raise VaultClientError(
                "VAULT_ADMIN_TOKEN is not configured on this agent. "
                "Set it in Railway environment variables."
            )
        return token

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Vault-Admin-Token": self._token(),
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str) -> Any:
        """GET *path* from the vault; return parsed JSON. Raises VaultClientError."""
        url = f"{self._base_url()}{path}"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.RequestError as exc:
            raise VaultClientError(f"Could not reach vault: {exc}") from exc
        if not resp.is_success:
            _raise_for_status(resp, path)
        return resp.json()

    async def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        """POST *body* to *path*; return parsed JSON. Raises VaultClientError."""
        url = f"{self._base_url()}{path}"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    url, json=body or {}, headers=self._headers()
                )
        except httpx.RequestError as exc:
            raise VaultClientError(f"Could not reach vault: {exc}") from exc
        if not resp.is_success:
            _raise_for_status(resp, path)
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def overview(self, *, force: bool = False) -> Dict[str, Any]:
        """Return the vault overview (connections, agents, grants, catalog).

        Cached for _OVERVIEW_CACHE_TTL seconds.  Pass force=True to bypass
        the cache (e.g. after a mutation).
        """
        now = time.monotonic()
        if (
            not force
            and self._overview_cache is not None
            and now - self._overview_cache_at < _OVERVIEW_CACHE_TTL
        ):
            return self._overview_cache
        data = await self._get("/api/admin/overview")
        self._overview_cache = data
        self._overview_cache_at = now
        logger.debug("[vault-admin] overview fetched (%d connections)", len(data.get("connections", [])))
        return data

    def invalidate_cache(self) -> None:
        """Drop the cached overview so the next call fetches fresh data."""
        self._overview_cache = None
        self._overview_cache_at = 0.0

    async def connection_ids(self) -> List[str]:
        """Return sorted list of connection IDs for autocomplete."""
        data = await self.overview()
        return sorted(c["id"] for c in data.get("connections", []))

    async def agent_names(self) -> List[str]:
        """Return the list of known agent names for autocomplete."""
        data = await self.overview()
        return list(data.get("agents", []))

    async def add(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new connection. Returns the created connection view.

        Required body fields depend on auth kind:
          bearer/header: name, service, api_key, base_url
          oauth2:        name, service, client_id, client_secret
        """
        result = await self._post("/api/admin/connections", body)
        self.invalidate_cache()
        conn_id = (result.get("connection") or {}).get("id", "?")
        logger.debug("[vault-admin] connection created: %s", conn_id)
        return result

    async def update(self, conn_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Partial-update *conn_id*. Blank/absent fields keep existing values.

        Returns the updated connection view (secret-free).
        Never log the body — it may contain credentials.
        """
        result = await self._post(
            f"/api/admin/connections/{conn_id}/update", body
        )
        self.invalidate_cache()
        logger.debug("[vault-admin] connection updated: %s", conn_id)
        return result

    async def delete(self, conn_id: str) -> None:
        """Delete *conn_id* and all its grants."""
        await self._post(f"/api/admin/connections/{conn_id}/delete")
        self.invalidate_cache()
        logger.debug("[vault-admin] connection deleted: %s", conn_id)

    async def set_grant(self, agent: str, conn_id: str, granted: bool) -> None:
        """Grant or revoke *agent*'s access to *conn_id*."""
        await self._post(
            "/api/admin/grants",
            {"agent": agent, "conn_id": conn_id, "granted": granted},
        )
        self.invalidate_cache()
        logger.debug(
            "[vault-admin] grant %s for %s -> %s",
            "added" if granted else "removed", agent, conn_id,
        )

    async def connect_link(self, conn_id: str) -> str:
        """Return the OAuth authorize URL for *conn_id*.

        Raises VaultClientError for non-OAuth connections or missing config.
        """
        result = await self._post(
            f"/api/admin/connections/{conn_id}/connect-link"
        )
        url = result.get("url", "")
        if not url:
            raise VaultClientError(
                f"Vault returned no authorize URL for connection '{conn_id}'"
            )
        logger.debug("[vault-admin] connect-link obtained for: %s", conn_id)
        return url


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------

def _raise_for_status(resp: httpx.Response, path: str) -> None:
    """Convert a non-2xx vault response into a VaultClientError.

    The response body is read for the error ``detail`` field only and is
    never logged — it might contain partial secret material in edge cases.
    """
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        detail = ""
    status = resp.status_code
    if status == 401:
        raise VaultClientError(
            "Vault admin authentication failed — check VAULT_ADMIN_TOKEN."
        )
    if status == 404:
        raise VaultClientError(
            f"Vault returned 404 for {path}"
            + (f": {detail}" if detail else "")
        )
    if status == 409:
        raise VaultClientError(detail or f"Conflict on {path}")
    if status == 415:
        raise VaultClientError("Vault rejected the request content type (internal error).")
    raise VaultClientError(
        f"Vault returned HTTP {status} for {path}"
        + (f": {detail}" if detail else "")
    )
