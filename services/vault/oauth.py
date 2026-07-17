"""OAuth 2.0 (authorization-code) flows for the Open Manus Key Vault.

The vault is the OAuth client: the admin clicks "Connect", logs into the
provider, and tokens are stored encrypted in Redis. The proxy refreshes
access tokens automatically; agents only ever see upstream API responses.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from catalog import get_template

logger = logging.getLogger("vault.oauth")

# Refresh a little early so in-flight requests don't race expiry.
_EXPIRY_SLACK_SECONDS = 90


class OAuthError(Exception):
    pass


def redirect_uri(public_url: str) -> str:
    return f"{public_url.rstrip('/')}/oauth/callback"


def build_authorize_url(service: str, conn_id: str, client_id: str,
                        public_url: str, store) -> str:
    tpl = get_template(service)
    if not tpl or tpl["auth"]["kind"] != "oauth2":
        raise OAuthError(f"{service} is not an OAuth service")
    oauth = tpl["oauth"]
    state = pysecrets.token_urlsafe(24)
    store.put_oauth_state(state, {"service": service, "conn_id": conn_id})
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(public_url),
        "response_type": "code",
        "scope": " ".join(oauth.get("scopes") or []),
        "state": state,
    }
    params.update(oauth.get("extra_authorize_params") or {})
    return f"{oauth['authorize_url']}?{urlencode(params)}"


async def exchange_code(service: str, code: str, client_id: str,
                        client_secret: str, public_url: str) -> Dict[str, Any]:
    """Exchange an authorization code for tokens. Returns the token payload."""
    tpl = get_template(service)
    oauth = (tpl or {}).get("oauth") or {}
    token_url = oauth.get("token_url")
    if not token_url:
        raise OAuthError(f"No token URL for service {service}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(public_url),
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise OAuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if "error" in payload:
        raise OAuthError(f"Token exchange failed: {payload.get('error_description') or payload['error']}")
    if not payload.get("access_token"):
        raise OAuthError("Token exchange returned no access_token")
    return _normalize_token_payload(payload)


async def refresh_tokens(service: str, secrets: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh an access token. Returns updated token fields (keeps the old
    refresh_token if the provider doesn't rotate it)."""
    tpl = get_template(service)
    oauth = (tpl or {}).get("oauth") or {}
    refresh_token = secrets.get("refresh_token")
    if not refresh_token:
        raise OAuthError("No refresh token stored — reconnect this service")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            oauth["token_url"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": secrets.get("client_id", ""),
                "client_secret": secrets.get("client_secret", ""),
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code >= 400:
        raise OAuthError(f"Token refresh failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if "error" in payload:
        raise OAuthError(f"Token refresh failed: {payload.get('error_description') or payload['error']}")
    updated = _normalize_token_payload(payload)
    if not updated.get("refresh_token"):
        updated["refresh_token"] = refresh_token
    return updated


def _normalize_token_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "access_token": payload.get("access_token", ""),
        "token_type": payload.get("token_type", "Bearer"),
    }
    if payload.get("refresh_token"):
        out["refresh_token"] = payload["refresh_token"]
    expires_in = payload.get("expires_in")
    if expires_in:
        try:
            out["expires_at"] = time.time() + float(expires_in)
        except (TypeError, ValueError):
            pass
    if payload.get("scope"):
        out["scope"] = payload["scope"]
    return out


def token_expired(secrets: Dict[str, Any]) -> bool:
    expires_at = secrets.get("expires_at")
    if not expires_at:
        return False  # non-expiring token (e.g. classic GitHub OAuth)
    return time.time() >= float(expires_at) - _EXPIRY_SLACK_SECONDS


async def get_valid_access_token(service: str, conn_id: str, store) -> Tuple[str, bool]:
    """Return (access_token, was_refreshed); persists refreshed tokens."""
    secrets = store.get_secrets(conn_id)
    token = secrets.get("access_token")
    if not token:
        raise OAuthError("Service not connected — complete the OAuth login in the vault dashboard")
    if not token_expired(secrets):
        return token, False
    updated = await refresh_tokens(service, secrets)
    secrets.update(updated)
    store.set_secrets(conn_id, secrets)
    logger.info("Refreshed OAuth token for connection %s", conn_id)
    return secrets["access_token"], True
