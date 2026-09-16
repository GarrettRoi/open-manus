"""Replit MCP bridge for the Open Manus Key Vault.

When Garrett approves a dev modification request in Discord
(``/devrequests``), the bot pushes the request id onto the Redis list
``devreq:dispatch``. A background task here pops that queue and calls
Replit's MCP server (``update_app_using_prompt``) so a Replit Agent
session starts on the Open Manus project automatically — approve once
in Discord, work begins on Replit.

OAuth 2.1 + PKCE with dynamic client registration, per the MCP
authorization spec. The vault is the OAuth client; Garrett authorizes
once in his browser via /admin (tokens stored Fernet-encrypted in
Redis). Agents never see any of this.

Redis keys:
  replitmcp:client        registration JSON (client_id etc.)
  replitmcp:tokens        encrypted token JSON
  replitmcp:pkce:{state}  pending PKCE verifier (10 min TTL)
  replitmcp:projects      JSON object of project name → replId
  replitmcp:target_repl   replId of the Open Manus Replit project
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets as pysecrets
import time
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger("vault.replit_mcp")

# The vault service runs this module both as ``replit_mcp`` (its Docker
# entrypoint is in this directory) and as ``services.vault.replit_mcp`` in
# tests/tools.  Keep both import forms working without making the standalone
# vault depend on the repository package layout.
try:
    from services.vault.dev_projects import (
        DEFAULT_PROJECT,
        K_PROJECTS,
        normalize_project,
        resolve_project,
    )
except ImportError:  # pragma: no cover - exercised by the standalone service
    from dev_projects import (
        DEFAULT_PROJECT, K_PROJECTS, normalize_project, resolve_project)

MCP_URL = "https://replit-mcp.com/server/mcp"
MCP_ORIGIN = "https://replit-mcp.com"
MCP_PROTOCOL_VERSION = "2025-03-26"

K_CLIENT = "replitmcp:client"
K_TOKENS = "replitmcp:tokens"
K_PKCE = "replitmcp:pkce:"
K_TARGET = "replitmcp:target_repl"
K_HEARTBEAT = "replitmcp:loop_heartbeat"
K_CLAIM = "replitmcp:claim:"
K_LEASE = "replitmcp:lease:"
DISPATCH_QUEUE = "devreq:dispatch"

_EXPIRY_SLACK = 90
PKCE_TTL = 600
CLAIM_TTL = 300       # seconds — enqueue guard; long enough to cover LEASE_TTL
LEASE_TTL = 60        # seconds — durable dispatch lease; renewed every LEASE_RENEW_INTERVAL
LEASE_RENEW_INTERVAL = 20  # seconds between lease renewals in the worker
_SWEEP_IDLE_TICKS = 24  # BRPOP timeouts between periodic sweeps (≈ 2 min at 5 s/tick)

# ---------------------------------------------------------------------------
# Lua scripts — all atomically executed by Redis.
#
# _ENQUEUE_SCRIPT: SETNX claim + LPUSH in one transaction.
#   KEYS[1]=claim key  KEYS[2]=queue  ARGV[1]=CLAIM_TTL  ARGV[2]=req_id
#   Returns 1 if enqueued (claim acquired), 0 if already claimed.
#
# _ACQUIRE_LEASE_SCRIPT: set lease key NX with a unique token.
#   KEYS[1]=lease key  ARGV[1]=token  ARGV[2]=LEASE_TTL
#   Returns 1 if lease acquired, 0 if another worker holds it.
#
# _RENEW_LEASE_SCRIPT: extend TTL only if we still hold the token (CAS).
#   KEYS[1]=lease key  ARGV[1]=token  ARGV[2]=LEASE_TTL
#   Returns 1 if renewed (token matched), 0 if lease was superseded.
#
# _RELEASE_LEASE_SCRIPT: delete the lease only if we still hold the token.
#   KEYS[1]=lease key  ARGV[1]=token
#   Returns 1 if released (token matched), 0 if lease was superseded.
#
# _FINALIZE_LEASE_SCRIPT: CAS write of final item state + release lease.
#   KEYS[1]=lease key  KEYS[2]=item key
#   ARGV[1]=token  ARGV[2]=item JSON  ARGV[3]=item TTL (0 = no expiry)
#   Returns 1 if written (token matched), 0 if lease was superseded.
#
# _ADMIN_ENQUEUE_SCRIPT: atomic check-lease / check-claim / enqueue for /dispatch.
#   All guard checks and the enqueue happen inside one Lua eval so no two
#   concurrent calls can race each other.
#
#   KEYS[1]=lease key  KEYS[2]=claim key  KEYS[3]=dispatch queue
#   ARGV[1]="1" if force=true, "0" if force=false
#   ARGV[2]=claim TTL  ARGV[3]=req_id
#
#   Decision table:
#   ┌──────────────┬───────────┬──────────────────────────────────────────┐
#   │ lease live?  │ force?    │ result                                   │
#   ├──────────────┼───────────┼──────────────────────────────────────────┤
#   │ yes          │ no        │ 0  — 409 "dispatch in progress"          │
#   │ yes          │ yes       │ 2  — supersede: DEL both, SETNX, LPUSH   │
#   │ no           │ no        │ 1  — won claim (SETNX), LPUSH            │
#   │              │           │ 3  — lost claim (already queued/cooling) │
#   │ no           │ yes       │ 1  — force-clear claim, SETNX, LPUSH     │
#   └──────────────┴───────────┴──────────────────────────────────────────┘
#   Returns 0 = live lease, force not set (caller → 409)
#           1 = enqueued (won claim or force without live lease)
#           2 = enqueued, live lease superseded (force=true, lease was live)
#           3 = claim already held by earlier caller, not forced (caller → 409)
# ---------------------------------------------------------------------------
_ENQUEUE_SCRIPT = """
local claimed = redis.call("SET", KEYS[1], "1", "NX", "EX", tonumber(ARGV[1]))
if claimed then
    redis.call("LPUSH", KEYS[2], ARGV[2])
    return 1
end
return 0
"""

_ACQUIRE_LEASE_SCRIPT = """
local ok = redis.call("SET", KEYS[1], ARGV[1], "NX", "EX", tonumber(ARGV[2]))
if ok then return 1 end
return 0
"""

_RENEW_LEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
    return 1
end
return 0
"""

# _RELEASE_LEASE_SCRIPT: release only if the caller still owns the token.
# A worker must never delete a lease that a force-dispatch has superseded.
_RELEASE_LEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    redis.call("DEL", KEYS[1])
    return 1
end
return 0
"""

_FINALIZE_LEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
local ttl = tonumber(ARGV[3])
if ttl > 0 then
    redis.call("SET", KEYS[2], ARGV[2], "EX", ttl)
else
    redis.call("SET", KEYS[2], ARGV[2])
end
redis.call("DEL", KEYS[1])
return 1
"""

# Route selection is persisted before making the provider call.  The expected
# item JSON is an optimistic-CAS guard in addition to the durable lease: a
# force dispatch (or an administrative edit) cannot be overwritten by the
# worker that observed the old item.
_PIN_ROUTE_SCRIPT = """
if redis.call("GET", KEYS[1]) ~= ARGV[1] then
    return 0
end
if redis.call("GET", KEYS[2]) ~= ARGV[2] then
    return 0
end
local ttl = tonumber(ARGV[4])
if ttl > 0 then
    redis.call("SET", KEYS[2], ARGV[3], "EX", ttl)
else
    redis.call("SET", KEYS[2], ARGV[3])
end
return 1
"""

_ADMIN_ENQUEUE_SCRIPT = """
local lease_live = redis.call("EXISTS", KEYS[1])
if lease_live == 1 then
    if ARGV[1] == "0" then
        return 0
    end
    -- force=true with live lease: supersede (DEL both, SETNX, LPUSH)
    redis.call("DEL", KEYS[1], KEYS[2])
    redis.call("SET", KEYS[2], "1", "EX", tonumber(ARGV[2]))
    redis.call("LPUSH", KEYS[3], ARGV[3])
    return 2
end
-- No live lease.
if ARGV[1] == "1" then
    -- force=true without live lease: clear stale claim and re-enqueue.
    redis.call("DEL", KEYS[2])
    redis.call("SET", KEYS[2], "1", "EX", tonumber(ARGV[2]))
    redis.call("LPUSH", KEYS[3], ARGV[3])
    return 1
end
-- no force: only enqueue if we win the claim (SETNX).
local won = redis.call("SET", KEYS[2], "1", "NX", "EX", tonumber(ARGV[2]))
if won then
    redis.call("LPUSH", KEYS[3], ARGV[3])
    return 1
end
return 3
"""

_REDACT_PATTERNS = (
    # Provider diagnostics commonly echo these values in either JSON or
    # header-like text.  Keep the key/label while removing the value.
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(
        r"(?i)((?:access|refresh)_token\s*[:=]\s*[\"']?)[^\"'\s,}]+"),
     r"\1[redacted]"),
    (re.compile(
        r"(?i)((?:client_secret|api[_-]?key|authorization)\s*[:=]\s*[\"']?)[^\"'\s,}]+"),
     r"\1[redacted]"),
)

# Provider responses are not safe diagnostic data.  In particular, Replit may
# echo request headers, project metadata, or arbitrary tool output in an MCP
# error.  Keep the messages below fixed so they are safe to persist in the
# dev-request record and to show in the dashboard.
PROVIDER_AUTH_FAILURE = (
    "Replit authorization failed — reconnect Replit in the vault dashboard")
PROVIDER_ACCESS_FAILURE = (
    "Replit access was denied — verify the selected project ID and existing "
    "OAuth access")
PROVIDER_RETRY_FAILURE = (
    "Replit MCP request failed — retry later and check Replit status")


def _sanitize_text(value: Any, secret_values=()) -> str:
    """Return bounded diagnostic text with credentials removed.

    Provider error bodies and MCP results are outside our control and have
    occasionally reflected request headers.  Redaction is intentionally
    conservative and is applied before anything is logged or written to the
    request item.
    """
    text = str(value)
    for secret in secret_values or ():
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sanitize_dispatch_value(mcp: Any, value: Any) -> str:
    """Return a fixed provider failure message, never provider diagnostics.

    This helper is retained for callers that used the old sanitizer name.
    Credential redaction is not sufficient here: arbitrary provider bodies
    can contain project data or credentials that are not in our local token
    store.  Dispatch persistence must therefore never include *any* value
    derived from an exception or provider result.
    """
    return _provider_failure_message(value)


class ReplitMCPError(Exception):
    pass


class ReplitMCPRoutingError(ReplitMCPError):
    """A safe, actionable error resolving a request's configured route."""


class ReplitMCPProviderError(ReplitMCPError):
    """A provider failure whose public message is one of the fixed messages."""

    _SAFE_MESSAGES = {
        PROVIDER_AUTH_FAILURE,
        PROVIDER_ACCESS_FAILURE,
        PROVIDER_RETRY_FAILURE,
    }

    def __init__(self, message: str = PROVIDER_RETRY_FAILURE):
        # Keep even accidentally constructed provider exceptions safe.  This
        # matters for mocked clients and for future call sites that may pass a
        # response-derived message here.
        super().__init__(
            message if message in self._SAFE_MESSAGES
            else PROVIDER_RETRY_FAILURE)


def _provider_failure_for_status(status_code: Any) -> str:
    """Map an HTTP status to a fixed, actionable provider message."""
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return PROVIDER_RETRY_FAILURE
    if status == 401:
        return PROVIDER_AUTH_FAILURE
    if status == 403:
        return PROVIDER_ACCESS_FAILURE
    return PROVIDER_RETRY_FAILURE


def _provider_failure_for_rpc_error(error: Any) -> str:
    """Classify an RPC error without exposing its payload."""
    code = error.get("code") if isinstance(error, dict) else None
    if code in (401, "401", "unauthorized", "UNAUTHORIZED"):
        return PROVIDER_AUTH_FAILURE
    if code in (403, "403", "forbidden", "FORBIDDEN",
                "access_denied", "ACCESS_DENIED"):
        return PROVIDER_ACCESS_FAILURE
    return PROVIDER_RETRY_FAILURE


def _provider_failure_message(exc: Any) -> str:
    """Return a safe fixed message for an arbitrary provider-side failure."""
    if isinstance(exc, ReplitMCPProviderError):
        # Provider errors are only constructed with the constants above.
        return str(exc)
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return _provider_failure_for_status(status_code)
    if isinstance(exc, PermissionError):
        return PROVIDER_ACCESS_FAILURE
    return PROVIDER_RETRY_FAILURE


def _safe_dispatch_result(result: Any) -> Dict[str, Any]:
    """Keep only a boolean and fixed summary from a successful MCP call."""
    accepted = True
    if isinstance(result, dict) and "accepted" in result:
        # MCP JSON values are normally primitive, but identity avoids invoking
        # arbitrary truthiness hooks if a test/client supplies a custom value.
        accepted = result.get("accepted") is True
    return {
        "accepted": accepted,
        "summary": (
            "Replit Agent accepted the request"
            if accepted else "Replit Agent request completed"),
    }


# ---------------------------------------------------------------------------
# Lease / claim helper functions (sync — call via asyncio.to_thread)
# ---------------------------------------------------------------------------

def enqueue_if_unclaimed(r, req_id: str) -> bool:
    """Atomically claim + enqueue a dev request for dispatch.

    Uses a single Lua script so SETNX and LPUSH are one Redis operation.
    Returns True if enqueued (claim acquired), False if already claimed.
    """
    result = r.eval(
        _ENQUEUE_SCRIPT, 2,
        K_CLAIM + req_id,   # KEYS[1]
        DISPATCH_QUEUE,     # KEYS[2]
        str(CLAIM_TTL),     # ARGV[1]
        req_id,             # ARGV[2]
    )
    return bool(result)


def acquire_lease(r, req_id: str, token: str) -> bool:
    """Acquire the dispatch lease for req_id with a unique token.

    Returns True if this worker owns the lease, False if another does.
    The lease expires in LEASE_TTL seconds unless renewed.
    """
    result = r.eval(
        _ACQUIRE_LEASE_SCRIPT, 1,
        K_LEASE + req_id,   # KEYS[1]
        token,              # ARGV[1]
        str(LEASE_TTL),     # ARGV[2]
    )
    return bool(result)


def renew_lease(r, req_id: str, token: str) -> bool:
    """Extend the lease TTL only if we still hold the token (CAS).

    Returns True if renewed, False if the lease was superseded or expired.
    """
    result = r.eval(
        _RENEW_LEASE_SCRIPT, 1,
        K_LEASE + req_id,   # KEYS[1]
        token,              # ARGV[1]
        str(LEASE_TTL),     # ARGV[2]
    )
    return bool(result)


def release_lease(r, req_id: str, token: str) -> bool:
    """Release a dispatch lease only when its token still matches.

    This is used for fail-closed skips (for example, a request deleted or
    revoked after it was queued).  A superseding force-dispatch must retain
    its lease, so a naked ``DELETE`` is unsafe here.
    """
    result = r.eval(
        _RELEASE_LEASE_SCRIPT, 1,
        K_LEASE + req_id,  # KEYS[1]
        token,              # ARGV[1]
    )
    return bool(result)


def finalize_lease(r, req_id: str, token: str, item: dict, item_ttl: int) -> bool:
    """CAS-write the final item state and release the lease atomically.

    Verifies that this worker's token still matches the lease key before
    writing. Returns True if written, False if the lease was superseded
    (token mismatch — another worker or a force-dispatch took over).
    """
    result = r.eval(
        _FINALIZE_LEASE_SCRIPT, 2,
        K_LEASE + req_id,               # KEYS[1]
        f"devreq:item:{req_id}",        # KEYS[2]
        token,                          # ARGV[1]
        json.dumps(item),               # ARGV[2]
        str(max(0, item_ttl or 0)),     # ARGV[3]
    )
    return bool(result)


def pin_dispatch_route(r, req_id: str, token: str, item: dict,
                       project: str, repl_id: str) -> bool:
    """Persist a resolved route while *token* owns the request lease.

    Returns ``False`` when the lease or the item changed before the write.
    A route already present in *item* is never replaced; this makes retries
    use the original destination even if an administrator changes the
    registry between attempts.
    """
    raw = r.get(f"devreq:item:{req_id}")
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not raw:
        return False
    try:
        current = json.loads(raw)
    except (TypeError, ValueError):
        return False

    old_project = current.get("dispatch_project")
    old_repl_id = current.get("dispatch_repl_id")
    if old_repl_id:
        # A complete pin is immutable.  A legacy/partially pinned item can
        # receive its missing project label, but its explicit ID wins.
        existing_repl_id = str(old_repl_id)
        if existing_repl_id != str(repl_id):
            return False
        if old_project and str(old_project) != str(project):
            return False
        repl_id = existing_repl_id
        project = str(old_project or project)
    else:
        current["dispatch_project"] = project
        current["dispatch_repl_id"] = repl_id
    if old_repl_id:
        current["dispatch_project"] = project
        current["dispatch_repl_id"] = repl_id

    item_ttl = r.ttl(f"devreq:item:{req_id}")
    result = r.eval(
        _PIN_ROUTE_SCRIPT, 2,
        K_LEASE + req_id,
        f"devreq:item:{req_id}",
        token,
        raw,
        json.dumps(current, ensure_ascii=False),
        str(max(0, item_ttl or 0)),
    )
    return bool(result)


def has_live_lease(r, req_id: str) -> bool:
    """Return True if a dispatch lease is currently held for req_id."""
    return bool(r.exists(K_LEASE + req_id))


def admin_enqueue(r, req_id: str, force: bool = False) -> int:
    """Atomically check-lease / check-claim / enqueue for the /dispatch admin endpoint.

    Everything executes inside one Lua eval — concurrent admin calls cannot
    race each other.  See _ADMIN_ENQUEUE_SCRIPT for the full decision table.

    Returns:
      0 — live lease, force=False → caller should 409 "dispatch in progress"
      1 — enqueued: won the SETNX claim (no-force) or force-cleared stale claim
      2 — enqueued: live lease superseded by force=True
      3 — claim already held (another caller already queued this item, or the
          post-failure cooldown TTL is still live) → caller should 409 "already
          queued or in cooldown, use ?force=true to override"
    """
    result = r.eval(
        _ADMIN_ENQUEUE_SCRIPT, 3,
        K_LEASE + req_id,       # KEYS[1]
        K_CLAIM + req_id,       # KEYS[2]
        DISPATCH_QUEUE,         # KEYS[3]
        "1" if force else "0",  # ARGV[1]
        str(CLAIM_TTL),         # ARGV[2]
        req_id,                 # ARGV[3]
    )
    return int(result)


class ReplitMCP:
    """OAuth client + minimal Streamable-HTTP MCP caller."""

    def __init__(self, redis_client, encrypt: Callable[[str], str],
                 decrypt: Callable[[str], str], public_url: str):
        self.r = redis_client
        self.encrypt = encrypt
        self.decrypt = decrypt
        self.public_url = (public_url or "").rstrip("/")
        self._meta: Optional[Dict[str, Any]] = None

    # -- storage helpers ----------------------------------------------------
    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/oauth/replit-mcp/callback"

    def _get_json(self, key: str, encrypted: bool = False) -> Optional[Dict[str, Any]]:
        raw = self.r.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return json.loads(self.decrypt(raw) if encrypted else raw)
        except Exception:
            logger.warning("Could not decode %s — treating as unset", key)
            return None

    def _set_json(self, key: str, value: Dict[str, Any], encrypted: bool = False) -> None:
        raw = json.dumps(value)
        self.r.set(key, self.encrypt(raw) if encrypted else raw)

    def connected(self) -> bool:
        toks = self._get_json(K_TOKENS, encrypted=True)
        return bool(toks and toks.get("access_token"))

    def sanitize_text(self, value: Any) -> str:
        """Sanitize provider diagnostics using the locally stored credentials."""
        secrets = []
        for key, encrypted in ((K_TOKENS, True), (K_CLIENT, True)):
            try:
                payload = self._get_json(key, encrypted=encrypted) or {}
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                secrets.extend(v for v in payload.values()
                               if isinstance(v, str))
        return _sanitize_text(value, secrets)

    def target_repl(self) -> str:
        """Return the default project's repl ID for legacy callers.

        Dispatches use an explicit, pre-pinned ID instead.  This method
        remains for existing dashboard/admin callers and is intentionally
        scoped to the ``open-manus`` compatibility destination.
        """
        try:
            _name, repl_id = resolve_project(self.r, DEFAULT_PROJECT)
            return repl_id
        except (TypeError, ValueError):
            return ""

    # -- OAuth discovery / registration --------------------------------------
    async def _metadata(self) -> Dict[str, Any]:
        """Resolve the authorization server metadata (cached in memory)."""
        if self._meta:
            return self._meta
        async with httpx.AsyncClient(timeout=30) as client:
            issuer = MCP_ORIGIN
            scopes = None
            for url in (f"{MCP_ORIGIN}/.well-known/oauth-protected-resource/server/mcp",
                        f"{MCP_ORIGIN}/.well-known/oauth-protected-resource"):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        prm = resp.json()
                        servers = prm.get("authorization_servers") or []
                        if servers:
                            issuer = servers[0].rstrip("/")
                        scopes = prm.get("scopes_supported")
                        break
                except Exception:
                    continue
            meta = None
            from urllib.parse import urlsplit
            parts = urlsplit(issuer)
            base = f"{parts.scheme}://{parts.netloc}"
            path = parts.path.rstrip("/")
            for url in (f"{base}/.well-known/oauth-authorization-server{path}",
                        f"{issuer}/.well-known/oauth-authorization-server",
                        f"{issuer}/.well-known/openid-configuration"):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        meta = resp.json()
                        break
                except Exception:
                    continue
            if not meta or not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
                raise ReplitMCPError(
                    "Could not discover Replit MCP authorization endpoints")
            if scopes and not meta.get("_prm_scopes"):
                meta["_prm_scopes"] = scopes
            self._meta = meta
            return meta

    async def _client_registration(self) -> Dict[str, Any]:
        # Encrypted at rest: dynamic registration may include a client_secret.
        reg = self._get_json(K_CLIENT, encrypted=True)
        if reg and reg.get("client_id") and reg.get("redirect_uri") == self.redirect_uri:
            return reg
        meta = await self._metadata()
        reg_endpoint = meta.get("registration_endpoint")
        if not reg_endpoint:
            raise ReplitMCPError("Replit MCP auth server does not offer "
                                 "dynamic client registration")
        payload = {
            "client_name": "Open Manus Vault (dev request bridge)",
            "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(reg_endpoint, json=payload)
        if resp.status_code not in (200, 201):
            raise ReplitMCPError(
                "Replit MCP client registration failed — retry Connect Replit")
        try:
            reg = resp.json()
        except Exception:
            raise ReplitMCPError(
                "Replit MCP returned an invalid client registration — "
                "retry Connect Replit") from None
        if not isinstance(reg, dict):
            raise ReplitMCPError(
                "Replit MCP returned an invalid client registration — "
                "retry Connect Replit")
        reg["redirect_uri"] = self.redirect_uri
        self._set_json(K_CLIENT, reg, encrypted=True)
        return reg

    # -- OAuth flow -----------------------------------------------------------
    async def build_authorize_url(self) -> str:
        if not self.public_url:
            raise ReplitMCPError("VAULT_PUBLIC_URL is not configured")
        meta = await self._metadata()
        reg = await self._client_registration()
        verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = pysecrets.token_urlsafe(24)
        self.r.set(K_PKCE + state, verifier, ex=PKCE_TTL)
        from urllib.parse import urlencode
        params = {
            "client_id": reg["client_id"],
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": MCP_URL,
        }
        scopes = meta.get("_prm_scopes") or meta.get("scopes_supported")
        if scopes:
            params["scope"] = " ".join(scopes)
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    def _consume_pkce_state(self, state: str) -> Optional[str]:
        """Atomically fetch-and-delete the PKCE verifier (single-use state)."""
        key = K_PKCE + state
        try:
            verifier = self.r.getdel(key)  # Redis >= 6.2
        except Exception:
            pipe = self.r.pipeline(transaction=True)
            pipe.get(key)
            pipe.delete(key)
            verifier = pipe.execute()[0]
        if isinstance(verifier, bytes):
            verifier = verifier.decode()
        return verifier or None

    async def handle_callback(self, code: str, state: str) -> None:
        verifier = self._consume_pkce_state(state)
        if not verifier:
            raise ReplitMCPError("Login link expired or already used — start again")
        meta = await self._metadata()
        reg = await self._client_registration()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": reg["client_id"],
            "code_verifier": verifier,
            "resource": MCP_URL,
        }
        if reg.get("client_secret"):
            data["client_secret"] = reg["client_secret"]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(meta["token_endpoint"], data=data,
                                     headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise ReplitMCPError(
                "Replit authorization failed — restart Connect Replit")
        try:
            token_payload = resp.json()
        except Exception:
            raise ReplitMCPError(
                "Replit MCP returned an invalid token response — "
                "restart Connect Replit") from None
        if not isinstance(token_payload, dict):
            raise ReplitMCPError(
                "Replit MCP returned an invalid token response — "
                "restart Connect Replit")
        self._store_tokens(token_payload)
        logger.info("Replit MCP connected (tokens stored)")

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        if not payload.get("access_token"):
            raise ReplitMCPError(
                "Replit MCP returned no access token — restart Connect Replit")
        old = self._get_json(K_TOKENS, encrypted=True) or {}
        toks = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token") or old.get("refresh_token"),
            "token_type": payload.get("token_type", "Bearer"),
        }
        if payload.get("expires_in"):
            try:
                toks["expires_at"] = time.time() + float(payload["expires_in"])
            except (TypeError, ValueError):
                pass
        self._set_json(K_TOKENS, toks, encrypted=True)

    async def _access_token(self) -> str:
        toks = self._get_json(K_TOKENS, encrypted=True)
        if not toks or not toks.get("access_token"):
            raise ReplitMCPProviderError(PROVIDER_AUTH_FAILURE)
        expires_at = toks.get("expires_at")
        if expires_at and time.time() >= float(expires_at) - _EXPIRY_SLACK:
            if not toks.get("refresh_token"):
                raise ReplitMCPProviderError(PROVIDER_AUTH_FAILURE)
            meta = await self._metadata()
            reg = await self._client_registration()
            data = {
                "grant_type": "refresh_token",
                "refresh_token": toks["refresh_token"],
                "client_id": reg["client_id"],
                "resource": MCP_URL,
            }
            if reg.get("client_secret"):
                data["client_secret"] = reg["client_secret"]
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(meta["token_endpoint"], data=data,
                                         headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                raise ReplitMCPProviderError(
                    _provider_failure_for_status(resp.status_code)
                    if resp.status_code == 401
                    else PROVIDER_AUTH_FAILURE)
            try:
                token_payload = resp.json()
            except Exception:
                raise ReplitMCPProviderError(PROVIDER_AUTH_FAILURE) from None
            if not isinstance(token_payload, dict):
                raise ReplitMCPProviderError(PROVIDER_AUTH_FAILURE)
            self._store_tokens(token_payload)
            toks = self._get_json(K_TOKENS, encrypted=True)
        return toks["access_token"]

    # -- MCP calls -------------------------------------------------------------
    @staticmethod
    def _parse_mcp_response(resp: httpx.Response) -> Optional[Dict[str, Any]]:
        """Parse a Streamable HTTP response (JSON or SSE) into the JSON-RPC msg."""
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
        if resp.status_code == 202 or not resp.content:
            return None
        if ctype == "application/json":
            try:
                msg = resp.json()
            except Exception:
                raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE) from None
            if not isinstance(msg, dict):
                raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE)
            return msg
        if ctype == "text/event-stream":
            last = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                        last = msg
            return last
        raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE)

    async def _mcp_call_tool(self, tool: str, arguments: Dict[str, Any],
                             timeout: float = 120.0) -> Dict[str, Any]:
        try:
            token = await self._access_token()
        except ReplitMCPProviderError:
            raise
        except Exception:
            # Do not let a token-provider exception (which may contain an
            # echoed response body) cross this boundary.
            raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE) from None
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                init = await client.post(MCP_URL, headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "open-manus-vault", "version": "1.0"},
                    },
                })
                if init.status_code >= 400:
                    raise ReplitMCPProviderError(
                        _provider_failure_for_status(init.status_code))
                init_msg = self._parse_mcp_response(init)
                if not init_msg:
                    raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE)
                if init_msg.get("error"):
                    raise ReplitMCPProviderError(
                        _provider_failure_for_rpc_error(init_msg["error"]))
                session_id = init.headers.get("mcp-session-id")
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
                await client.post(MCP_URL, headers=headers, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized"})
                resp = await client.post(MCP_URL, headers=headers, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                })
                if resp.status_code >= 400:
                    raise ReplitMCPProviderError(
                        _provider_failure_for_status(resp.status_code))
                msg = self._parse_mcp_response(resp)
        except ReplitMCPProviderError:
            raise
        except Exception:
            # HTTP client and parser failures must not reflect arbitrary
            # provider response text to the caller or dispatch record.
            raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE) from None
        if not msg:
            raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE)
        if msg.get("error"):
            err = msg["error"]
            # -32001 timeout means Agent is still working — treat as accepted.
            if isinstance(err, dict) and err.get("code") == -32001:
                return {"accepted": True, "note": "MCP timeout — Agent run continues in background"}
            raise ReplitMCPProviderError(_provider_failure_for_rpc_error(err))
        result = msg.get("result") or {}
        if not isinstance(result, dict):
            raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE)
        if result.get("isError"):
            raise ReplitMCPProviderError(PROVIDER_ACCESS_FAILURE)
        return result

    async def start_agent_run(
            self, change_description: str,
            user_quotes: Optional[str] = None,
            repl_id: Optional[str] = None) -> Dict[str, Any]:
        """Start an Agent run on an explicit repl ID.

        ``repl_id`` is supplied by the dispatcher after its durable route pin.
        Omitting it preserves the old default-project API for dashboard callers;
        it must never be used by the multi-project dispatch path.
        """
        repl_id = (self.target_repl() if repl_id is None
                   else str(repl_id).strip())
        if not repl_id:
            raise ReplitMCPError(
                "No target Replit project configured for 'open-manus'")
        if (len(repl_id) > 128
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", repl_id)):
            raise ReplitMCPError("Configured Replit project ID is invalid")
        args: Dict[str, Any] = {"replId": repl_id,
                                "changeDescription": change_description}
        if user_quotes:
            args["userQuotes"] = user_quotes
        try:
            return await self._mcp_call_tool("update_app_using_prompt", args)
        except ReplitMCPProviderError:
            raise
        except PermissionError:
            raise ReplitMCPProviderError(PROVIDER_ACCESS_FAILURE) from None
        except Exception:
            # A provider-side exception must not cross the public dispatch
            # boundary with arbitrary response text attached.
            raise ReplitMCPProviderError(PROVIDER_RETRY_FAILURE) from None


# ---------------------------------------------------------------------------
# Dispatcher support: sweep + prompt builder + async lease renewal
# ---------------------------------------------------------------------------

def sweep_dispatch_backlog(r) -> list:
    """Re-queue approved dev requests that were never dispatched or failed.

    Skip guards (in order):
    1. dispatch_status == "started" — Agent run already underway.
    2. Live lease key (K_LEASE + req_id) — worker is actively dispatching.
    3. Claim key exists (K_CLAIM + req_id) — item is enqueued or in the
       post-failure cooldown window; enqueue_if_unclaimed() will return False.

    Returns the list of req_ids pushed onto devreq:dispatch.
    """
    approved_ids = r.lrange("devreq:approved", 0, -1)
    requeued = []
    for req_id in approved_ids:
        raw = r.get(f"devreq:item:{req_id}")
        if not raw:
            continue
        try:
            item = json.loads(raw if isinstance(raw, str) else raw.decode())
        except (ValueError, AttributeError):
            continue
        if item.get("dispatch_status") == "started":
            logger.debug("Sweep: request %s already started, skipping", req_id)
            continue
        # A live lease means a worker is mid-MCP-call (possibly past CLAIM_TTL
        # due to token refresh) — never re-queue while the lease is live.
        if has_live_lease(r, req_id):
            logger.debug("Sweep: request %s has active lease, skipping", req_id)
            continue
        if not enqueue_if_unclaimed(r, req_id):
            logger.debug("Sweep: request %s already claimed/queued, skipping", req_id)
            continue
        requeued.append(req_id)
        logger.info("Sweep: re-queued dev request %s (prior dispatch_status=%s)",
                    req_id, item.get("dispatch_status") or "never dispatched")
    return requeued


def _pinned_route(item: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """Read a previously persisted route without consulting current config."""
    repl_id = item.get("dispatch_repl_id")
    if not repl_id:
        return None
    try:
        project = normalize_project(
            item.get("dispatch_project") or item.get("project"))
    except ValueError as exc:
        raise ReplitMCPRoutingError("Stored dispatch project is invalid") from exc
    return project, str(repl_id)


async def _resolve_and_pin_route(mcp: ReplitMCP, req_id: str,
                                 lease_token: str,
                                 item: Dict[str, Any]) -> tuple[str, str]:
    """Resolve a request destination and durably pin it before the MCP call."""
    pinned = _pinned_route(item)
    if pinned:
        project, repl_id = pinned
        # Older/partially written items may have an ID but no display name.
        # Fill the label without ever resolving the ID through live config.
        if item.get("dispatch_project") != project:
            if not await asyncio.to_thread(
                    pin_dispatch_route, mcp.r, req_id, lease_token, item,
                    project, repl_id):
                raise ReplitMCPRoutingError(
                    "Dispatch route changed before it could be pinned; retrying")
            item["dispatch_project"] = project
        item["dispatch_repl_id"] = repl_id
        return project, repl_id

    requested = item.get("project") or DEFAULT_PROJECT
    try:
        project, repl_id = await asyncio.to_thread(
            resolve_project, mcp.r, requested)
    except ValueError as exc:
        # Registry/configuration details are safe and actionable for the
        # operator.  Keep them distinct from opaque provider failures so the
        # dispatcher can persist the useful routing explanation.
        raise ReplitMCPRoutingError(str(exc)) from exc
    if not await asyncio.to_thread(
            pin_dispatch_route, mcp.r, req_id, lease_token, item,
            project, repl_id):
        # A concurrent worker may have pinned the route just before our CAS.
        # If so, use that durable value.  Never resolve the current registry
        # again, since it may now point to a different project.
        raw = await asyncio.to_thread(mcp.r.get, f"devreq:item:{req_id}")
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            current = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            current = {}
        pinned = _pinned_route(current)
        if pinned:
            item.clear()
            item.update(current)
            return pinned
        raise ReplitMCPRoutingError(
            "Dispatch route could not be pinned; retry after the request is "
            "re-queued")
    item["dispatch_project"] = project
    item["dispatch_repl_id"] = repl_id
    return project, repl_id


def _build_prompt(item: Dict[str, Any]) -> str:
    try:
        project = normalize_project(
            item.get("dispatch_project") or item.get("project"))
    except ValueError as exc:
        raise ReplitMCPRoutingError("Stored dispatch project is invalid") from exc
    if project == DEFAULT_PROJECT:
        destination = (
            "Please implement this request in the Open Manus project only. "
            "Follow that project's existing repository, test, and release "
            "conventions.")
    else:
        destination = (
            f"Please implement this request in the '{project}' project only. "
            "Follow that project's own repository, test, and release "
            "conventions; do not modify or deploy unrelated projects.")
    return (
        f"Approved dev modification request #{item.get('id')} for project "
        f"'{project}', submitted by agent '{item.get('agent', 'unknown')}' "
        "(approved by the owner in Discord).\n\n"
        f"Title: {item.get('title', '')}\n\n"
        f"Details:\n{(item.get('description') or '')[:6000]}\n\n"
        f"{destination}\n"
        "Do not assume access to another project's files, deployment branch, "
        "or infrastructure."
    )


async def _renew_lease_loop(r, req_id: str, token: str) -> None:
    """Background coroutine: renew the dispatch lease every LEASE_RENEW_INTERVAL
    seconds until cancelled. Logs a warning if the lease is lost (superseded by
    a force-dispatch), so the caller's finalize_lease() CAS will return False.
    """
    try:
        while True:
            await asyncio.sleep(LEASE_RENEW_INTERVAL)
            still_held = await asyncio.to_thread(renew_lease, r, req_id, token)
            if not still_held:
                logger.warning(
                    "Dispatch: lease for request %s lost (superseded) — "
                    "final CAS write will be skipped", req_id)
                return
            logger.debug("Dispatch: lease renewed for request %s", req_id)
    except asyncio.CancelledError:
        pass


async def dispatch_loop(mcp: ReplitMCP) -> None:
    """Forever: pop approved request ids from devreq:dispatch and start
    Replit Agent runs via the Replit MCP bridge.

    Durable lease protocol (prevents duplicate Agent runs even when an MCP
    call outlives the enqueue claim TTL):
      1. Pop req_id from queue.
      2. Acquire a unique-token dispatch lease (replitmcp:lease:{id}, NX, 60 s).
         If another worker holds it, skip — the item is already being worked.
      3. Start a background lease-renewal coroutine (renews every 20 s) so the
         lease survives the full MCP call (up to 120 s + token refresh).
     4. Re-read the current item under the lease; if it is missing or no
        longer approved, fail closed and release via token CAS.  Also skip if
        already "started".
      5. Call start_agent_run.
      6. Finalize via Lua CAS: verify the token still matches, write the item,
         delete the lease. If the token no longer matches (force-dispatch
         superseded us), skip the write — the superseding worker owns the run.
      7. Cancel the renewal task.

    Claim key (replitmcp:claim:{id}) is the enqueue-dedup guard; it is set at
    LPUSH time and left to expire via CLAIM_TTL. The lease is the in-flight
    guard. Both must be absent before a new enqueue is possible.
    """
    import os as _os
    import redis as _redis_mod
    import uuid as _uuid

    _redis_url = _os.getenv("REDIS_URL", "").strip()
    if not _redis_url:
        # Do NOT fall back to localhost — a silent localhost connection would
        # drain a different (empty) Redis than the one agents write to, causing
        # approved requests to be permanently lost in the dispatch queue.
        # Instead idle in a long sleep loop so the vault supervisor does not
        # thrash restarts, and log loudly so the operator notices.
        logger.error(
            "Replit MCP dispatcher: REDIS_URL is not set — cannot connect to "
            "Redis.  Approved dev requests will NOT be dispatched until "
            "REDIS_URL is configured.  Idling (check every 60 s)."
        )
        while True:
            await asyncio.sleep(60)
            _redis_url = _os.getenv("REDIS_URL", "").strip()
            if _redis_url:
                logger.info(
                    "Replit MCP dispatcher: REDIS_URL is now set — restarting "
                    "dispatch loop."
                )
                break
            logger.error(
                "Replit MCP dispatcher: REDIS_URL still not set — still idling."
            )

    _brpop_r = _redis_mod.from_url(
        _redis_url,
        decode_responses=True,
        socket_timeout=8,
        socket_keepalive=True,
    )

    logger.info("Replit MCP dispatcher started (queue: %s)", DISPATCH_QUEUE)
    idle_ticks = 0
    while True:
        try:
            await asyncio.to_thread(
                mcp.r.set, K_HEARTBEAT, str(int(time.time())), ex=60)

            popped = await asyncio.to_thread(_brpop_r.brpop, DISPATCH_QUEUE, 5)
            if not popped:
                idle_ticks += 1
                if idle_ticks >= _SWEEP_IDLE_TICKS:
                    idle_ticks = 0
                    requeued = await asyncio.to_thread(
                        sweep_dispatch_backlog, mcp.r)
                    if requeued:
                        logger.info("Periodic sweep re-queued %d request(s): %s",
                                    len(requeued), requeued)
                continue

            idle_ticks = 0
            req_id = popped[1]
            if isinstance(req_id, bytes):
                req_id = req_id.decode()
            req_id = req_id.strip()

            # --- Load item --------------------------------------------------
            raw = await asyncio.to_thread(mcp.r.get, f"devreq:item:{req_id}")
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw:
                logger.warning("Dispatch: request %s not found in Redis", req_id)
                continue
            item = json.loads(raw)

            # --- Acquire durable dispatch lease -----------------------------
            lease_token = str(_uuid.uuid4())
            lease_ok = await asyncio.to_thread(
                acquire_lease, mcp.r, req_id, lease_token)
            if not lease_ok:
                logger.info(
                    "Dispatch: request %s already has an active lease — "
                    "skipping this queue entry", req_id)
                continue

            # --- Lease acquired: start renewal and do the work --------------
            renew_task = asyncio.create_task(
                _renew_lease_loop(mcp.r, req_id, lease_token))
            try:
                # Re-read dispatch_status under the lease; another worker may
                # have written "started" between enqueue and now.
                raw2 = await asyncio.to_thread(mcp.r.get, f"devreq:item:{req_id}")
                if not raw2:
                    logger.warning(
                        "Dispatch: request %s disappeared under its lease — "
                        "releasing without dispatch", req_id)
                    await asyncio.to_thread(
                        release_lease, mcp.r, req_id, lease_token)
                    continue
                try:
                    current = json.loads(
                        raw2 if isinstance(raw2, str) else raw2.decode())
                except (TypeError, ValueError, UnicodeDecodeError):
                    current = None
                if not isinstance(current, dict):
                    logger.warning(
                        "Dispatch: request %s has an invalid current record — "
                        "releasing without dispatch", req_id)
                    await asyncio.to_thread(
                        release_lease, mcp.r, req_id, lease_token)
                    continue
                item = current
                if item.get("status") != "approved":
                    logger.info(
                        "Dispatch: request %s is no longer approved (%s) — "
                        "releasing without dispatch",
                        req_id, item.get("status") or "missing status")
                    await asyncio.to_thread(
                        release_lease, mcp.r, req_id, lease_token)
                    continue
                if item.get("dispatch_status") == "started":
                    logger.info(
                        "Dispatch: request %s already started — releasing lease",
                        req_id)
                    await asyncio.to_thread(
                        release_lease, mcp.r, req_id, lease_token)
                    continue

                try:
                    # Resolve and persist the destination while the lease is
                    # held, before any provider call.  A later retry must
                    # honor this route even if the administrator edits the
                    # registry.
                    _project, dispatch_repl_id = await _resolve_and_pin_route(
                        mcp, req_id, lease_token, item)

                    # A force-dispatch may supersede this worker after route
                    # resolution (especially on a pinned retry or when route
                    # pinning failed over to a concurrently written item).
                    # Renew is a token-CAS ownership check; fail closed and
                    # never start a provider run without the current lease.
                    if not await asyncio.to_thread(
                            renew_lease, mcp.r, req_id, lease_token):
                        raise ReplitMCPError(
                            "Dispatch lease was lost before the provider call")

                    # Call the MCP (may take up to 120 s + token refresh)
                    result = await mcp.start_agent_run(
                        _build_prompt(item), repl_id=dispatch_repl_id)
                    item["dispatch_status"] = "started"
                    item["dispatched_at"] = int(time.time())
                    item["dispatch_result"] = _safe_dispatch_result(result)
                    # A successful retry supersedes any prior provider
                    # failure.  Do not leave stale error text in the record.
                    item.pop("dispatch_error", None)
                    logger.info("Dispatched dev request %s to Replit Agent", req_id)
                except ReplitMCPRoutingError as exc:
                    item["dispatch_status"] = "failed"
                    # Routing details are generated locally from the
                    # configured registry and are intentionally kept separate
                    # from opaque provider diagnostics.
                    item["dispatch_error"] = str(exc)[:500]
                except Exception as exc:
                    item["dispatch_status"] = "failed"
                    item["dispatch_error"] = _provider_failure_message(exc)
                    logger.error("Dispatch of dev request %s failed: %s",
                                 req_id, item["dispatch_error"])

                # CAS finalize: write item + release lease atomically.
                # Returns False if a force-dispatch superseded our token.
                item_ttl = await asyncio.to_thread(
                    mcp.r.ttl, f"devreq:item:{req_id}")
                written = await asyncio.to_thread(
                    finalize_lease, mcp.r, req_id, lease_token, item, item_ttl)
                if not written:
                    logger.warning(
                        "Dispatch: lease for request %s was superseded — "
                        "final status NOT written by this worker", req_id)
            finally:
                renew_task.cancel()
                try:
                    await renew_task
                except asyncio.CancelledError:
                    pass

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Replit MCP dispatcher iteration failed")
            await asyncio.sleep(5)
