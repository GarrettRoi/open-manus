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
  replitmcp:target_repl   replId of the Open Manus Replit project
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets as pysecrets
import time
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger("vault.replit_mcp")

MCP_URL = "https://replit-mcp.com/server/mcp"
MCP_ORIGIN = "https://replit-mcp.com"
MCP_PROTOCOL_VERSION = "2025-03-26"

K_CLIENT = "replitmcp:client"
K_TOKENS = "replitmcp:tokens"
K_PKCE = "replitmcp:pkce:"
K_TARGET = "replitmcp:target_repl"
K_HEARTBEAT = "replitmcp:loop_heartbeat"
K_CLAIM = "replitmcp:claim:"
DISPATCH_QUEUE = "devreq:dispatch"

_EXPIRY_SLACK = 90
PKCE_TTL = 600
CLAIM_TTL = 300       # seconds — long enough to cover a full MCP round-trip
_SWEEP_IDLE_TICKS = 24  # BRPOP timeouts between periodic sweeps (≈ 2 min at 5 s/tick)

# ---------------------------------------------------------------------------
# Atomic enqueue: SETNX claim + LPUSH in a single Lua transaction.
# Shared by ALL three producers (approval, sweep, manual /dispatch endpoint)
# so no two of them can double-queue the same request regardless of timing.
#
# KEYS[1] = claim key   (replitmcp:claim:{id})
# KEYS[2] = queue name  (devreq:dispatch)
# ARGV[1] = claim TTL   (seconds, int)
# ARGV[2] = req_id      (value pushed onto the queue)
#
# Returns 1 if the claim was acquired and the item enqueued, 0 if already
# claimed (already in queue or currently being dispatched).
# ---------------------------------------------------------------------------
_ENQUEUE_SCRIPT = """
local claimed = redis.call("SET", KEYS[1], "1", "NX", "EX", tonumber(ARGV[1]))
if claimed then
    redis.call("LPUSH", KEYS[2], ARGV[2])
    return 1
end
return 0
"""


class ReplitMCPError(Exception):
    pass


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

    def target_repl(self) -> str:
        v = self.r.get(K_TARGET)
        if isinstance(v, bytes):
            v = v.decode()
        return (v or "").strip()

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
                f"Client registration failed ({resp.status_code}): {resp.text[:300]}")
        reg = resp.json()
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
                f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
        self._store_tokens(resp.json())
        logger.info("Replit MCP connected (tokens stored)")

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        if not payload.get("access_token"):
            raise ReplitMCPError("Token response had no access_token")
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
            raise ReplitMCPError(
                "Replit is not connected yet — open the vault dashboard and "
                "click Connect Replit")
        expires_at = toks.get("expires_at")
        if expires_at and time.time() >= float(expires_at) - _EXPIRY_SLACK:
            if not toks.get("refresh_token"):
                raise ReplitMCPError("Replit login expired — reconnect in the dashboard")
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
                raise ReplitMCPError(
                    f"Token refresh failed ({resp.status_code}) — reconnect "
                    f"Replit in the dashboard: {resp.text[:200]}")
            self._store_tokens(resp.json())
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
            return resp.json()
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
        raise ReplitMCPError(f"Unexpected MCP response type {ctype!r} "
                             f"(HTTP {resp.status_code}): {resp.text[:200]}")

    async def _mcp_call_tool(self, tool: str, arguments: Dict[str, Any],
                             timeout: float = 120.0) -> Dict[str, Any]:
        token = await self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            init = await client.post(MCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "open-manus-vault", "version": "1.0"},
                },
            })
            if init.status_code == 401:
                raise ReplitMCPError("Replit rejected the login (401) — "
                                     "reconnect Replit in the vault dashboard")
            init_msg = self._parse_mcp_response(init)
            if not init_msg or init_msg.get("error"):
                raise ReplitMCPError(f"MCP initialize failed: "
                                     f"{json.dumps(init_msg)[:300] if init_msg else init.status_code}")
            session_id = init.headers.get("mcp-session-id")
            if session_id:
                headers["Mcp-Session-Id"] = session_id
            await client.post(MCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "method": "notifications/initialized"})
            resp = await client.post(MCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            })
            msg = self._parse_mcp_response(resp)
        if not msg:
            raise ReplitMCPError(f"Empty MCP response (HTTP {resp.status_code})")
        if msg.get("error"):
            err = msg["error"]
            # -32001 timeout means Agent is still working — treat as accepted.
            if err.get("code") == -32001:
                return {"accepted": True, "note": "MCP timeout — Agent run continues in background"}
            raise ReplitMCPError(f"MCP tool error: {json.dumps(err)[:300]}")
        result = msg.get("result") or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", [])
                     if isinstance(c, dict)]
            raise ReplitMCPError(f"Replit reported an error: {' '.join(texts)[:300]}")
        return result

    async def start_agent_run(self, change_description: str,
                              user_quotes: Optional[str] = None) -> Dict[str, Any]:
        repl_id = self.target_repl()
        if not repl_id:
            raise ReplitMCPError("No target Replit project configured "
                                 "(replitmcp:target_repl)")
        args: Dict[str, Any] = {"replId": repl_id,
                                "changeDescription": change_description}
        if user_quotes:
            args["userQuotes"] = user_quotes
        return await self._mcp_call_tool("update_app_using_prompt", args)


# ---------------------------------------------------------------------------
# Dispatcher: devreq:dispatch queue -> Replit Agent run
# ---------------------------------------------------------------------------

def enqueue_if_unclaimed(r, req_id: str) -> bool:
    """Atomically claim + enqueue a dev request for dispatch.

    Uses a single Lua script so the SETNX and LPUSH are one Redis operation —
    no window exists between them for a concurrent producer to duplicate the
    entry. Returns True if the request was claimed and pushed, False if it was
    already claimed (already in queue or actively being dispatched).
    """
    result = r.eval(
        _ENQUEUE_SCRIPT,
        2,                        # numkeys
        K_CLAIM + req_id,         # KEYS[1] — claim key
        DISPATCH_QUEUE,           # KEYS[2] — dispatch queue
        str(CLAIM_TTL),           # ARGV[1] — claim TTL in seconds
        req_id,                   # ARGV[2] — value pushed onto queue
    )
    return bool(result)


def sweep_dispatch_backlog(r) -> list:
    """Re-queue approved dev requests that were never dispatched or failed.

    Dedup guards:
    - Skips items whose dispatch_status is "started" (run already underway).
    - Delegates the claim+enqueue step to enqueue_if_unclaimed(), which uses
      a Lua script to make SETNX+LPUSH atomic — safe to call concurrently
      with approval and manual /dispatch without producing duplicates.

    Returns the list of req_ids that were pushed onto devreq:dispatch.
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
        # dispatch_status=="started" means an Agent run is already underway —
        # never re-queue, even if the claim key has since expired.
        if item.get("dispatch_status") == "started":
            logger.debug("Sweep: request %s already started, skipping", req_id)
            continue
        # Atomic claim+enqueue via Lua; returns False if already claimed.
        if not enqueue_if_unclaimed(r, req_id):
            logger.debug("Sweep: request %s already claimed/queued, skipping", req_id)
            continue
        requeued.append(req_id)
        logger.info("Sweep: re-queued dev request %s (prior dispatch_status=%s)",
                    req_id, item.get("dispatch_status") or "never dispatched")
    return requeued


def _build_prompt(item: Dict[str, Any]) -> str:
    return (
        f"Approved dev modification request #{item.get('id')} from Open Manus "
        f"agent '{item.get('agent', 'unknown')}' (approved by the owner in "
        f"Discord).\n\nTitle: {item.get('title', '')}\n\n"
        f"Details:\n{(item.get('description') or '')[:6000]}\n\n"
        "Please implement this request in the Open Manus fleet codebase, "
        "test it, and deploy by pushing to the deploy branch as usual. When "
        "done, update the request status in Redis (devreq:item:"
        f"{item.get('id')}) to 'done'."
    )


async def dispatch_loop(mcp: ReplitMCP) -> None:
    """Forever: pop approved request ids from devreq:dispatch and start
    Replit Agent runs. Failures are recorded on the request item so the
    owner can see why nothing started.

    Also writes a heartbeat key every iteration (K_HEARTBEAT, EX 60) so the
    status endpoint can confirm the loop is alive, and runs a periodic backlog
    sweep every _SWEEP_IDLE_TICKS BRPOP timeouts to catch items that slipped
    through (e.g. approved before the current deploy was live).

    Uses a dedicated Redis client for BRPOP with socket_keepalive=True so
    Railway's TCP idle-timeout cannot kill the blocking connection, and
    socket_timeout=8 > BRPOP timeout=5 so the server-side nil return always
    arrives before the socket gives up — preventing the crash-loop that
    occurred when the shared mcp.r client had no keepalive and the 10 s BRPOP
    raced against Railway's ~10 s network idle timeout.
    """
    import os as _os
    import redis as _redis_mod

    _redis_url = _os.getenv("REDIS_URL", "redis://localhost:6379")
    _brpop_r = _redis_mod.from_url(
        _redis_url,
        decode_responses=True,
        socket_timeout=8,        # must exceed BRPOP timeout below
        socket_keepalive=True,   # keeps the connection alive during the wait
    )

    logger.info("Replit MCP dispatcher started (queue: %s)", DISPATCH_QUEUE)
    idle_ticks = 0
    while True:
        try:
            # Heartbeat: let the status endpoint know the loop is running.
            await asyncio.to_thread(
                mcp.r.set, K_HEARTBEAT, str(int(time.time())), ex=60)

            popped = await asyncio.to_thread(_brpop_r.brpop, DISPATCH_QUEUE, 5)
            if not popped:
                # BRPOP timed out — count idle ticks and maybe sweep backlog.
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
            raw = await asyncio.to_thread(mcp.r.get, f"devreq:item:{req_id}")
            if isinstance(raw, bytes):
                raw = raw.decode()
            if not raw:
                logger.warning("Dispatch: request %s not found", req_id)
                await asyncio.to_thread(mcp.r.delete, K_CLAIM + req_id)
                continue
            item = json.loads(raw)
            # Final guard: re-read from Redis just before calling the MCP so
            # a duplicate queue entry (e.g. from a stale manual re-queue before
            # the atomic enqueue migration) cannot start a second Agent run.
            if item.get("dispatch_status") == "started":
                logger.info(
                    "Dispatch: request %s already started (concurrent entry) — skipping",
                    req_id)
                await asyncio.to_thread(mcp.r.delete, K_CLAIM + req_id)
                continue
            try:
                result = await mcp.start_agent_run(_build_prompt(item))
                item["dispatch_status"] = "started"
                item["dispatched_at"] = int(time.time())
                item["dispatch_result"] = str(result)[:500]
                logger.info("Dispatched dev request %s to Replit Agent", req_id)
            except Exception as exc:
                item["dispatch_status"] = "failed"
                item["dispatch_error"] = str(exc)[:500]
                logger.error("Dispatch of dev request %s failed: %s", req_id, exc)
            ttl = await asyncio.to_thread(mcp.r.ttl, f"devreq:item:{req_id}")
            kwargs = {"ex": ttl} if ttl and ttl > 0 else {}
            await asyncio.to_thread(
                mcp.r.set, f"devreq:item:{req_id}", json.dumps(item), **kwargs)
            # Claim handling after dispatch attempt:
            #
            # SUCCESS (started): leave the claim to expire via TTL. The primary
            # guard is dispatch_status=="started"; the claim is redundant but
            # harmless extra protection.
            #
            # FAILURE: do NOT delete the claim — let it expire via CLAIM_TTL
            # (300 s). Deleting immediately would open a window for a concurrent
            # sweep that is mid-iteration to re-queue the same item (confirmed
            # duplicate in production parallel-sweep test). After CLAIM_TTL the
            # item becomes retry-able again naturally. Manual /dispatch bypasses
            # this by force-clearing the claim before enqueuing.
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Replit MCP dispatcher iteration failed")
            await asyncio.sleep(5)
