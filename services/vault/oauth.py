"""OAuth 2.0 (authorization-code) flows for the Open Manus Key Vault.

The vault is the OAuth client: the admin clicks "Connect", logs into the
provider, and tokens are stored encrypted in Redis. The proxy refreshes
access tokens automatically; agents only ever see upstream API responses.
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import logging
import math
import secrets as pysecrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import httpx

from catalog import get_template

logger = logging.getLogger("vault.oauth")

# Some providers (Reddit) reject requests without a descriptive User-Agent.
_DEFAULT_UA = "open-manus-vault/1.0 (+https://github.com/open-manus)"

# Refresh a little early so in-flight requests don't race expiry.
_EXPIRY_SLACK_SECONDS = 90
_PROACTIVE_WINDOW_SECONDS = 10 * 60
_REFRESH_LOCK_TTL_SECONDS = 120
_REFRESH_LOCK_WAIT_SECONDS = 5
_REFRESH_LOCK_POLL_SECONDS = .1
_TRANSIENT_REFRESH_ERROR_CODES = {
    "temporarily_unavailable", "server_error", "rate_limit", "slow_down",
}


class OAuthError(Exception):
    pass


class OAuthRefreshError(OAuthError):
    """Sanitized refresh failure with a machine-readable retry disposition."""

    def __init__(self, message: str, *, transient: bool = False,
                 reason: str = "refresh_failed"):
        super().__init__(message)
        self.transient = transient
        self.reason = reason


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
            f"Meta long-lived token exchange was rejected ({resp.status_code})")
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
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
        raise OAuthError(f"Token exchange was rejected ({resp.status_code})")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise OAuthError("Token exchange returned an invalid response")
    if "error" in payload:
        raise OAuthError("Token exchange was rejected by the provider")
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
        raise OAuthRefreshError(
            "No refresh token stored — reconnect this service",
            reason="missing_refresh_token")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": secrets.get("client_id", ""),
        "client_secret": secrets.get("client_secret", ""),
    }
    kwargs = _token_request_kwargs(
        oauth, data, secrets.get("client_id", ""), secrets.get("client_secret", ""))
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(oauth["token_url"], **kwargs)
    except httpx.RequestError as exc:
        raise OAuthRefreshError(
            "OAuth token refresh is temporarily unavailable",
            transient=True, reason="transport_error") from exc
    if resp.status_code >= 400:
        # Only inspect the standardized error code. Provider bodies can echo
        # request credentials and must never be put in exceptions or logs.
        try:
            error_code = str(resp.json().get("error") or "").lower()
        except (ValueError, TypeError, AttributeError):
            error_code = ""
        if error_code == "invalid_grant":
            raise OAuthRefreshError(
                "OAuth refresh authorization is no longer valid — reconnect this service",
                reason="invalid_grant")
        transient = (
            error_code in _TRANSIENT_REFRESH_ERROR_CODES
            or resp.status_code in (408, 425, 429)
            or resp.status_code >= 500)
        raise OAuthRefreshError(
            ("OAuth token refresh is temporarily unavailable" if transient else
             "OAuth refresh authorization was rejected — reconnect this service"),
            transient=transient,
            reason="provider_unavailable" if transient else "refresh_rejected")
    try:
        payload = resp.json()
    except (ValueError, TypeError) as exc:
        raise OAuthRefreshError(
            "OAuth token refresh returned an invalid response",
            transient=True, reason="invalid_response") from exc
    if not isinstance(payload, dict):
        raise OAuthRefreshError(
            "OAuth token refresh returned an invalid response",
            transient=True, reason="invalid_response")
    if "error" in payload:
        error_code = str(payload.get("error") or "").lower()
        if error_code == "invalid_grant":
            raise OAuthRefreshError(
                "OAuth refresh authorization is no longer valid — reconnect this service",
                reason="invalid_grant")
        if error_code in _TRANSIENT_REFRESH_ERROR_CODES:
            raise OAuthRefreshError(
                "OAuth token refresh is temporarily unavailable",
                transient=True, reason="provider_unavailable")
        raise OAuthRefreshError(
            "OAuth token refresh was rejected — reconnect this service",
            reason="refresh_rejected")
    updated = _normalize_token_payload(payload, refresh_response=True)
    if not updated.get("access_token"):
        raise OAuthRefreshError(
            "OAuth token refresh returned no access token",
            transient=True, reason="invalid_response")
    if not updated.get("refresh_token"):
        updated["refresh_token"] = refresh_token
    return updated


def _normalize_token_payload(payload: Dict[str, Any], *,
                             refresh_response: bool = False) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        if refresh_response:
            raise OAuthRefreshError(
                "OAuth token refresh returned an invalid response",
                transient=True, reason="invalid_response")
        raise OAuthError("Token exchange returned an invalid response")
    out: Dict[str, Any] = {
        "access_token": payload.get("access_token", ""),
        "token_type": payload.get("token_type", "Bearer"),
    }
    if payload.get("refresh_token"):
        out["refresh_token"] = payload["refresh_token"]
    if "expires_in" in payload:
        expires_in = payload.get("expires_in")
        try:
            seconds = float(expires_in)
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError
            out["expires_at"] = time.time() + seconds
        except (TypeError, ValueError):
            if refresh_response:
                raise OAuthRefreshError(
                    "OAuth token refresh returned an invalid expiry",
                    transient=True, reason="invalid_response")
            raise OAuthError("Token exchange returned an invalid expiry")
    if payload.get("scope"):
        out["scope"] = payload["scope"]
    return out


def token_expired(secrets: Dict[str, Any]) -> bool:
    return token_expiring(secrets, _EXPIRY_SLACK_SECONDS)


def token_expiring(secrets: Dict[str, Any], within_seconds: float) -> bool:
    expires_at = secrets.get("expires_at")
    if not expires_at:
        return False  # non-expiring token (e.g. classic GitHub OAuth)
    try:
        return time.time() >= float(expires_at) - within_seconds
    except (TypeError, ValueError):
        return True


async def _release_refresh_lock(redis_client, key: str, owner: str) -> None:
    try:
        redis_client.eval(
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "return redis.call('DEL', KEYS[1]) else return 0 end",
            1, key, owner)
    except Exception:
        # Fail closed. A non-atomic GET/DEL fallback could delete a successor's
        # lease if this one expires between those commands; TTL performs cleanup.
        logger.warning("Could not atomically release OAuth refresh lock")


def _still_owns_lock(redis_client, key: str, owner: str) -> bool:
    try:
        return redis_client.get(key) == owner
    except Exception:
        return False


def _persist_if_lock_owner(store, conn_id: str, lock_key: str, owner: str,
                           secrets: Dict[str, Any],
                           metadata: Optional[Dict[str, Any]] = None) -> bool:
    """Atomically persist encrypted secrets only while the record and lease exist."""
    conn_key = f"vault:conn:{conn_id}"
    # Production ConnectionStore exposes its encryption function. Keep the
    # fallback for narrow unit-test stores, while retaining ownership/existence
    # checks there too.
    if hasattr(store, "encrypt"):
        encrypted = store.encrypt(json.dumps(secrets))
        updated_at = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata or {})
        result = store.r.eval(
            "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end "
            "if redis.call('EXISTS', KEYS[2]) == 0 then return -1 end "
            "redis.call('HSET', KEYS[2], 'secret_enc', ARGV[2], "
            "'updated_at', ARGV[3]) "
            "local m = cjson.decode(ARGV[4]) "
            "for k,v in pairs(m) do redis.call('HSET', KEYS[2], k, v) end "
            "return 1",
            2, lock_key, conn_key, owner, encrypted, updated_at, metadata_json)
        return int(result or 0) == 1
    if not _still_owns_lock(store.r, lock_key, owner) or not store.get(conn_id):
        return False
    store.set_secrets(conn_id, secrets)
    if metadata:
        store.r.hset(conn_key, mapping=metadata)
    return True


def _update_metadata_if_secret_unchanged(
        store, conn_id: str, expected_secrets: Dict[str, Any],
        expected_encrypted: Optional[str], metadata: Dict[str, Any]) -> bool:
    """CAS metadata without recreating a deleted or reauthorized connection."""
    conn_key = f"vault:conn:{conn_id}"
    if hasattr(store, "encrypt"):
        if expected_encrypted is None:
            return False
        result = store.r.eval(
            "if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end "
            "if redis.call('HGET', KEYS[1], 'secret_enc') ~= ARGV[1] "
            "then return 0 end "
            "local m = cjson.decode(ARGV[2]) "
            "for k,v in pairs(m) do redis.call('HSET', KEYS[1], k, v) end "
            "return 1",
            1, conn_key, expected_encrypted, json.dumps(metadata))
        return int(result or 0) == 1
    if not store.get(conn_id) or store.get_secrets(conn_id) != expected_secrets:
        return False
    store.r.hset(conn_key, mapping=metadata)
    return True


def _authorization_secret_merge(current: Dict[str, Any],
                                tokens: Dict[str, Any]) -> Dict[str, Any]:
    """Merge an explicit login without retaining tokens from another account."""
    merged = dict(current)
    for field in ("access_token", "refresh_token", "expires_at",
                  "token_type", "scope"):
        merged.pop(field, None)
    merged.update(tokens)
    return merged


async def exchange_and_store_code(service: str, conn_id: str, code: str,
                                  public_url: str, store,
                                  conn: Optional[Dict[str, Any]] = None,
                                  code_verifier: Optional[str] = None) -> Dict[str, Any]:
    """Complete explicit authorization while superseding in-flight refresh.

    Explicit owner authorization takes priority: replacing the lock lease makes
    any older refresh response fail its ownership check. An omitted refresh
    token is intentionally not borrowed from the previous login, which may
    belong to a different Google account.
    """
    lock_key = f"vault:oauth_refresh_lock:{conn_id}"
    owner = pysecrets.token_urlsafe(18)
    store.r.set(lock_key, owner, ex=_REFRESH_LOCK_TTL_SECONDS)
    try:
        if not store.get(conn_id):
            raise OAuthError("OAuth connection no longer exists")
        current = store.get_secrets(conn_id)
        tokens = await exchange_code(
            service, code, current.get("client_id", ""),
            current.get("client_secret", ""), public_url, conn=conn,
            code_verifier=code_verifier)
        merged = _authorization_secret_merge(current, tokens)
        refresh_missing = (
            is_google_service(service) and not merged.get("refresh_token"))
        metadata = {
            "status": "needs_reauth" if refresh_missing else "ready",
            "oauth_refresh_status": (
                "reauthorization_required" if refresh_missing else "ok"),
            "oauth_refresh_at": "",
            "oauth_last_attempt_at": str(time.time()),
            "oauth_next_retry_at": "",
        }
        if not _persist_if_lock_owner(
                store, conn_id, lock_key, owner, merged, metadata):
            raise OAuthRefreshError(
                "OAuth authorization was superseded; reconnect this service",
                transient=True, reason="authorization_superseded")
        return merged
    finally:
        await _release_refresh_lock(store.r, lock_key, owner)


async def refresh_connection(service: str, conn_id: str, store,
                             conn: Optional[Dict[str, Any]] = None, *,
                             within_seconds: float = _EXPIRY_SLACK_SECONDS,
                             retries: int = 0) -> Tuple[str, bool]:
    """Refresh under a per-connection Redis lock and merge into a fresh read.

    Both lazy request refresh and the proactive worker use this path. Rereading
    after lock acquisition prevents an older refresh response from overwriting
    another worker's rotated refresh token.
    """
    key = f"vault:oauth_refresh_lock:{conn_id}"
    owner = pysecrets.token_urlsafe(18)
    deadline = time.monotonic() + _REFRESH_LOCK_WAIT_SECONDS
    acquired = False
    while time.monotonic() < deadline:
        acquired = bool(store.r.set(
            key, owner, nx=True, ex=_REFRESH_LOCK_TTL_SECONDS))
        if acquired:
            break
        await asyncio.sleep(_REFRESH_LOCK_POLL_SECONDS)
    if not acquired:
        # A winning worker may have completed while we waited.
        latest = store.get_secrets(conn_id)
        if latest.get("access_token") and not token_expiring(latest, within_seconds):
            return latest["access_token"], False
        raise OAuthRefreshError(
            "OAuth token refresh is already in progress",
            transient=True, reason="refresh_busy")

    try:
        current = store.get_secrets(conn_id)
        token = current.get("access_token")
        if not token:
            raise OAuthError(
                "Service not connected — complete the OAuth login in the vault dashboard")
        if not token_expiring(current, within_seconds):
            return token, False

        # At most three 30-second endpoint attempts plus short backoff fit
        # within the 120-second lease. Callers cannot extend work past it.
        max_retries = max(0, min(int(retries), 2))
        attempt = 0
        while True:
            try:
                updated = await refresh_tokens(service, current, conn=conn)
                break
            except OAuthRefreshError as exc:
                if not exc.transient or attempt >= max_retries:
                    raise
                await asyncio.sleep(min(2 ** attempt, 4))
                attempt += 1

        # Recheck ownership after network I/O. Explicit reauthorization can
        # supersede this lease, and deletion must never be undone by HSET.
        if not _still_owns_lock(store.r, key, owner):
            latest = store.get_secrets(conn_id)
            if latest.get("access_token") and not token_expiring(
                    latest, within_seconds):
                return latest["access_token"], False
            raise OAuthRefreshError(
                "OAuth token refresh was superseded",
                transient=True, reason="refresh_superseded")
        latest = store.get_secrets(conn_id)
        if (latest.get("refresh_token") != current.get("refresh_token")
                or latest.get("client_id") != current.get("client_id")):
            if latest.get("access_token") and not token_expiring(
                    latest, within_seconds):
                return latest["access_token"], False
            raise OAuthRefreshError(
                "OAuth credentials changed during refresh",
                transient=True, reason="credentials_changed")
        # Preserve credentials and provider-omitted fields from the latest
        # encrypted record, not the pre-request snapshot.
        latest.update(updated)
        # An omitted expiry means the replacement token did not declare one;
        # never retain the previous access token's already-expired timestamp.
        if "expires_at" not in updated:
            latest.pop("expires_at", None)
        refreshed_at = str(time.time())
        if not _persist_if_lock_owner(store, conn_id, key, owner, latest, {
                "oauth_refresh_status": "ok",
                "oauth_refresh_at": refreshed_at,
                "oauth_last_attempt_at": refreshed_at,
                "oauth_next_retry_at": "",
        }):
            raise OAuthRefreshError(
                "OAuth token refresh was superseded",
                transient=True, reason="refresh_superseded")
        logger.info("Refreshed OAuth token for connection %s", conn_id)
        return latest["access_token"], True
    finally:
        await _release_refresh_lock(store.r, key, owner)


async def get_valid_access_token(service: str, conn_id: str, store,
                                 conn: Optional[Dict[str, Any]] = None) -> Tuple[str, bool]:
    """Return (access_token, was_refreshed); persists refreshed tokens."""
    secrets = store.get_secrets(conn_id)
    token = secrets.get("access_token")
    if not token:
        raise OAuthError("Service not connected — complete the OAuth login in the vault dashboard")
    if not token_expired(secrets):
        return token, False
    return await refresh_connection(service, conn_id, store, conn=conn)


def is_google_service(service: str) -> bool:
    return service == "google" or service.startswith("google_")


async def refresh_ready_google_connections(store, *,
                                           within_seconds: float = _PROACTIVE_WINDOW_SECONDS,
                                           retries: int = 2) -> Dict[str, int]:
    """Refresh expiring ready combined/dedicated Google OAuth connections."""
    result = {"refreshed": 0, "transient": 0, "needs_reauth": 0}
    for conn_id in store.list_ids():
        conn = store.get(conn_id)
        if not conn or conn.get("status") != "ready":
            continue
        if not is_google_service(conn.get("service") or ""):
            continue
        if (conn.get("auth") or {}).get("kind") != "oauth2":
            continue
        current = store.get_secrets(conn_id)
        if not current.get("access_token") or not token_expiring(current, within_seconds):
            continue
        expected_encrypted = None
        try:
            expected_encrypted = store.r.hget(
                f"vault:conn:{conn_id}", "secret_enc")
        except (AttributeError, TypeError):
            pass
        try:
            _, changed = await refresh_connection(
                conn["service"], conn_id, store, conn=conn,
                within_seconds=within_seconds, retries=retries)
            if changed:
                result["refreshed"] += 1
        except OAuthRefreshError as exc:
            now = time.time()
            if exc.transient:
                # Keep status ready: a later sweep/lazy request can retry.
                changed = _update_metadata_if_secret_unchanged(
                    store, conn_id, current, expected_encrypted, {
                    "oauth_refresh_status": "retrying",
                    "oauth_last_attempt_at": str(now),
                    "oauth_next_retry_at": str(now + 300),
                })
                if not changed:
                    continue
                result["transient"] += 1
                logger.warning(
                    "Transient OAuth refresh failure for connection %s (%s)",
                    conn_id, exc.reason)
            else:
                changed = _update_metadata_if_secret_unchanged(
                    store, conn_id, current, expected_encrypted, {
                    "status": "needs_reauth",
                    "oauth_refresh_status": "reauthorization_required",
                    "oauth_last_attempt_at": str(now),
                    "oauth_next_retry_at": "",
                })
                if not changed:
                    continue
                result["needs_reauth"] += 1
                logger.warning(
                    "OAuth reauthorization required for connection %s (%s)",
                    conn_id, exc.reason)
        except OAuthError:
            # Missing access tokens require explicit authorization too.
            changed = _update_metadata_if_secret_unchanged(
                store, conn_id, current, expected_encrypted, {
                "status": "needs_reauth",
                "oauth_refresh_status": "reauthorization_required",
                "oauth_last_attempt_at": str(time.time()),
                "oauth_next_retry_at": "",
            })
            if changed:
                result["needs_reauth"] += 1
    return result


async def google_refresh_loop(store, interval_seconds: float = 300) -> None:
    """Periodic proactive refresh; first sweep waits to keep startup bounded."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await refresh_ready_google_connections(store)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Avoid exception text: third-party diagnostics can reflect request
            # values. Per-connection expected failures are handled above.
            logger.error("Google OAuth proactive refresh sweep failed")
