"""OAuth 2.0 (authorization-code) flows for the Open Manus Key Vault.

The vault is the OAuth client: the admin clicks "Connect", logs into the
provider, and tokens are stored encrypted in Redis. The proxy refreshes
access tokens automatically; agents only ever see upstream API responses.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets as pysecrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from catalog import get_template

logger = logging.getLogger("vault.oauth")

# Some providers (Reddit) reject requests without a descriptive User-Agent.
_DEFAULT_UA = "open-manus-vault/1.0 (+https://github.com/open-manus)"

# Refresh a little early so in-flight requests don't race expiry.
_EXPIRY_SLACK_SECONDS = 90


class OAuthError(Exception):
    pass


def redirect_uri(public_url: str) -> str:
    return f"{public_url.rstrip('/')}/oauth/callback"


def oauth_config(service: str, conn: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """OAuth endpoints/scopes for a connection: per-connection config (custom
    OAuth apps store it in auth_json) wins over the static catalog template."""
    auth = (conn or {}).get("auth") or {}
    if isinstance(auth.get("oauth"), dict) and auth["oauth"].get("token_url"):
        return auth["oauth"]
    tpl = get_template(service)
    return ((tpl or {}).get("oauth")) or {}


def build_authorize_url(service: str, conn_id: str, client_id: str,
                        public_url: str, store,
                        conn: Optional[Dict[str, Any]] = None) -> str:
    oauth = oauth_config(service, conn)
    if not oauth.get("authorize_url"):
        raise OAuthError(f"{service} is not an OAuth service (no authorize URL configured)")
    state = pysecrets.token_urlsafe(24)
    state_payload: Dict[str, Any] = {"service": service, "conn_id": conn_id}
    # Provider quirk: some (TikTok) name the client-id param differently.
    client_id_param = oauth.get("client_id_param") or "client_id"
    params = {
        client_id_param: client_id,
        "redirect_uri": redirect_uri(public_url),
        "response_type": "code",
        "scope": (oauth.get("scope_separator") or " ").join(oauth.get("scopes") or []),
        "state": state,
    }
    # PKCE (required by X/Twitter OAuth 2.0): S256 challenge, verifier kept
    # in the state payload server-side until the callback.
    if oauth.get("pkce"):
        verifier = pysecrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
        state_payload["code_verifier"] = verifier
    params.update(oauth.get("extra_authorize_params") or {})
    store.put_oauth_state(state, state_payload)
    return f"{oauth['authorize_url']}?{urlencode(params)}"


def _token_request_kwargs(oauth: Dict[str, Any], data: Dict[str, str],
                          client_id: str, client_secret: str) -> Dict[str, Any]:
    """Apply provider token-endpoint quirks to a token/refresh POST."""
    headers = {"Accept": "application/json", "User-Agent": _DEFAULT_UA}
    client_id_param = oauth.get("client_id_param") or "client_id"
    auth = None
    if oauth.get("token_auth") == "basic":
        # Reddit / X / Pinterest style: client creds via HTTP Basic only.
        auth = (client_id, client_secret)
        data.pop("client_secret", None)
        data.pop(client_id_param, None)
    elif client_id_param != "client_id":
        data[client_id_param] = data.pop("client_id", client_id)
    data.update(oauth.get("extra_token_params") or {})
    return {"data": data, "headers": headers, "auth": auth}


async def _facebook_long_lived(oauth: Dict[str, Any], short_token: str,
                               client_id: str, client_secret: str) -> Dict[str, Any]:
    """Swap a short-lived Meta user token for a ~60-day long-lived one."""
    token_url = oauth["token_url"]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(token_url, params={
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": short_token,
        })
    if resp.status_code >= 400:
        raise OAuthError(
            f"Meta long-lived token exchange failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("access_token"):
        raise OAuthError("Meta long-lived token exchange returned no access_token")
    return payload


async def exchange_code(service: str, code: str, client_id: str,
                        client_secret: str, public_url: str,
                        conn: Optional[Dict[str, Any]] = None,
                        code_verifier: Optional[str] = None) -> Dict[str, Any]:
    """Exchange an authorization code for tokens. Returns the token payload.

    Provider quirks are handled here (never agent-side): PKCE code_verifier
    (X/Twitter), HTTP-Basic token auth (Reddit/X/Pinterest), renamed
    client-id params (TikTok's client_key), and Meta's long-lived-token
    exchange after the initial code exchange.
    """
    oauth = oauth_config(service, conn)
    token_url = oauth.get("token_url")
    if not token_url:
        raise OAuthError(f"No token URL for service {service}")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri(public_url),
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    kwargs = _token_request_kwargs(oauth, data, client_id, client_secret)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(token_url, **kwargs)
    if resp.status_code >= 400:
        raise OAuthError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    if "error" in payload:
        raise OAuthError(f"Token exchange failed: {payload.get('error_description') or payload['error']}")
    if not payload.get("access_token"):
        raise OAuthError("Token exchange returned no access_token")
    if oauth.get("long_lived_exchange") == "facebook":
        # Meta short-lived (~1h) user tokens must be swapped immediately for
        # a long-lived (~60 day) token; there is no refresh_token flow.
        payload = await _facebook_long_lived(
            oauth, payload["access_token"], client_id, client_secret)
    return _normalize_token_payload(payload)


async def refresh_tokens(service: str, secrets: Dict[str, Any],
                         conn: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Refresh an access token. Returns updated token fields (keeps the old
    refresh_token if the provider doesn't rotate it)."""
    oauth = oauth_config(service, conn)
    if not oauth.get("token_url"):
        raise OAuthError(f"No token URL configured for {service}")
    refresh_token = secrets.get("refresh_token")
    if not refresh_token:
        raise OAuthError("No refresh token stored — reconnect this service")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": secrets.get("client_id", ""),
        "client_secret": secrets.get("client_secret", ""),
    }
    kwargs = _token_request_kwargs(
        oauth, data, secrets.get("client_id", ""), secrets.get("client_secret", ""))
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(oauth["token_url"], **kwargs)
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


async def get_valid_access_token(service: str, conn_id: str, store,
                                 conn: Optional[Dict[str, Any]] = None) -> Tuple[str, bool]:
    """Return (access_token, was_refreshed); persists refreshed tokens."""
    secrets = store.get_secrets(conn_id)
    token = secrets.get("access_token")
    if not token:
        raise OAuthError("Service not connected — complete the OAuth login in the vault dashboard")
    if not token_expired(secrets):
        return token, False
    updated = await refresh_tokens(service, secrets, conn=conn)
    secrets.update(updated)
    store.set_secrets(conn_id, secrets)
    logger.info("Refreshed OAuth token for connection %s", conn_id)
    return secrets["access_token"], True
