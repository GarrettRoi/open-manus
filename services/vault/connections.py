"""Connection storage + credential injection for the Open Manus Key Vault.

A *connection* is a configured instance of a catalog service (e.g. "GOOGLE",
"QUO", "N8N_MAIN"). Secrets live only here, Fernet-encrypted in Redis, and are
injected into upstream requests by the proxy — agents never receive them.

Redis layout:
  vault:conn:{ID}      hash: service, label, base_url, auth_json,
                             secret_enc (encrypted JSON), description,
                             skill_description, status, created_at, updated_at
  vault:conns          zset index of connection IDs
  vault:grant:{agent}:{ID}   grant marker (same prefix as the legacy key system,
                             so existing grants survive the migration)
  vault:oauth_state:{state}  pending OAuth flow (10 min TTL)

Legacy `vault:key:{NAME}` entries are migrated in-place to connections on
startup (service guessed from the stored metadata; defaults to bearer auth).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from catalog import CATALOG, get_template

logger = logging.getLogger("vault.connections")

PFX_CONN = "vault:conn:"
CONN_INDEX = "vault:conns"
PFX_GRANT = "vault:grant:"
PFX_OAUTH_STATE = "vault:oauth_state:"
PFX_REQUEST = "vault:request:"
REQUEST_INDEX = "vault:requests"
PFX_REQUEST_RATE = "vault:reqrate:"
REQUEST_MAX_PER_AGENT = 10
REQUEST_MAX_TOTAL = 200
REQUEST_TTL_SECONDS = 7 * 24 * 3600  # stale requests age out after a week
REQUEST_RATE_MAX = 20  # per agent per hour

# Legacy prefixes (migrated on startup)
PFX_LEGACY_KEY = "vault:key:"
LEGACY_KEY_INDEX = "vault:keys"
PFX_LEGACY_SKILL = "vault:skill:"
LEGACY_SKILL_INDEX = "vault:skills"

_ID_RE = re.compile(r"[^A-Z0-9_]+")

# Map legacy "service" metadata strings to catalog templates.
_LEGACY_SERVICE_HINTS = {
    "openai": "openai",
    "elevenlabs": "elevenlabs",
    "eleven labs": "elevenlabs",
    "discord": "discord",
    "n8n": "n8n",
    "openphone": "quo",
    "quo": "quo",
    "railway": "railway",
}


def normalize_id(name: str) -> str:
    return _ID_RE.sub("", (name or "").strip().upper().replace(" ", "_").replace("-", "_"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectionStore:
    def __init__(self, redis_client, encrypt, decrypt):
        self.r = redis_client
        self.encrypt = encrypt
        self.decrypt = decrypt

    # -- CRUD ---------------------------------------------------------------

    def list_ids(self) -> List[str]:
        return self.r.zrange(CONN_INDEX, 0, -1)

    def get(self, conn_id: str) -> Optional[Dict[str, Any]]:
        data = self.r.hgetall(f"{PFX_CONN}{conn_id}")
        if not data:
            return None
        data["id"] = conn_id
        try:
            data["auth"] = json.loads(data.get("auth_json") or "{}")
        except ValueError:
            data["auth"] = {}
        return data

    def get_secrets(self, conn_id: str) -> Dict[str, Any]:
        data = self.r.hget(f"{PFX_CONN}{conn_id}", "secret_enc")
        if not data:
            return {}
        try:
            return json.loads(self.decrypt(data))
        except Exception:
            logger.exception("Failed to decrypt secrets for %s", conn_id)
            return {}

    def set_secrets(self, conn_id: str, secrets: Dict[str, Any]) -> None:
        self.r.hset(f"{PFX_CONN}{conn_id}", mapping={
            "secret_enc": self.encrypt(json.dumps(secrets)),
            "updated_at": now_iso(),
        })

    def save(self, conn_id: str, *, service: str, label: str = "",
             base_url: str = "", auth: Optional[Dict[str, Any]] = None,
             secrets: Optional[Dict[str, Any]] = None, description: str = "",
             skill_description: str = "", status: str = "ready") -> str:
        conn_id = normalize_id(conn_id)
        if not conn_id:
            raise ValueError("Connection name is required")
        tpl = get_template(service) or CATALOG["custom"]
        auth = auth or dict(tpl["auth"])
        existing = self.r.exists(f"{PFX_CONN}{conn_id}")
        mapping = {
            "service": service,
            "label": label or tpl["label"],
            "base_url": (base_url or tpl.get("base_url") or "").rstrip("/"),
            "auth_json": json.dumps(auth),
            "description": description,
            "skill_description": skill_description,
            "status": status,
            "updated_at": now_iso(),
        }
        if not existing:
            mapping["created_at"] = now_iso()
        if secrets is not None:
            mapping["secret_enc"] = self.encrypt(json.dumps(secrets))
        self.r.hset(f"{PFX_CONN}{conn_id}", mapping=mapping)
        self.r.zadd(CONN_INDEX, {conn_id: time.time()})
        return conn_id

    def delete(self, conn_id: str, agent_names: List[str]) -> None:
        self.r.delete(f"{PFX_CONN}{conn_id}")
        self.r.zrem(CONN_INDEX, conn_id)
        for agent in agent_names:
            self.r.delete(f"{PFX_GRANT}{agent}:{conn_id}")

    # -- Grants ---------------------------------------------------------------

    def has_grant(self, agent: str, conn_id: str) -> bool:
        return bool(self.r.exists(f"{PFX_GRANT}{agent}:{conn_id}"))

    def set_grant(self, agent: str, conn_id: str, granted: bool, by: str = "admin") -> bool:
        """Returns True if the state changed."""
        key = f"{PFX_GRANT}{agent}:{conn_id}"
        if granted and not self.r.exists(key):
            self.r.hset(key, mapping={"granted_at": now_iso(), "granted_by": by})
            return True
        if not granted and self.r.exists(key):
            self.r.delete(key)
            return True
        return False

    # -- Host allowlist (SSRF guard) -----------------------------------------

    def allowed_hosts(self, conn: Dict[str, Any]) -> List[str]:
        tpl = get_template(conn.get("service", "")) or {}
        hosts = list(tpl.get("allowed_hosts") or [])
        base = conn.get("base_url") or ""
        if base:
            host = urlsplit(base).hostname
            if host and host not in hosts:
                hosts.append(host)
        return hosts

    # -- Legacy migration ------------------------------------------------------

    def migrate_legacy_keys(self) -> int:
        """Convert vault:key:* entries into connections (idempotent)."""
        migrated = 0
        for name in self.r.zrange(LEGACY_KEY_INDEX, 0, -1):
            conn_id = normalize_id(name)
            if self.r.exists(f"{PFX_CONN}{conn_id}"):
                continue
            legacy = self.r.hgetall(f"{PFX_LEGACY_KEY}{name}")
            if not legacy:
                continue
            try:
                value = self.decrypt(legacy.get("encrypted_value", ""))
            except Exception:
                logger.warning("Skipping legacy key %s: cannot decrypt", name)
                continue
            svc_meta = (legacy.get("service") or "").strip().lower()
            service = "custom"
            for hint, svc in _LEGACY_SERVICE_HINTS.items():
                if hint in svc_meta or hint in name.lower():
                    service = svc
                    break
            skill = self.r.hgetall(f"{PFX_LEGACY_SKILL}{name}") or {}
            tpl = get_template(service) or {}
            # Only mark ready when the template gives us a working base URL;
            # instance-specific services (n8n, custom) need admin attention.
            has_base = bool(tpl.get("base_url"))
            self.save(
                conn_id,
                service=service,
                label=legacy.get("service") or name,
                base_url="",  # template default fills in at save time
                secrets={"api_key": value},
                description=legacy.get("description", ""),
                skill_description=skill.get("description", ""),
                status="ready" if has_base else "needs_base_url",
            )
            migrated += 1
            logger.info("Migrated legacy key %s -> connection %s (%s)", name, conn_id, service)
        return migrated

    # -- Connection lookup (agent "does this exist?" check) ---------------------

    def find_connection(self, query: str) -> Optional[Dict[str, Any]]:
        """Fuzzy-match a connection by id, service, or label."""
        q = (query or "").strip().lower()
        if not q:
            return None
        qid = normalize_id(query)
        conn = self.get(qid) if qid else None
        if conn:
            return conn
        for cid in self.list_ids():
            conn = self.get(cid)
            if not conn:
                continue
            if q == (conn.get("service") or "").lower():
                return conn
            if q in cid.lower() or q in (conn.get("label") or "").lower():
                return conn
        return None

    # -- Agent-initiated setup requests -----------------------------------------

    def check_request_rate(self, agent: str) -> bool:
        """Sliding per-agent budget: max REQUEST_RATE_MAX new/updated requests
        per hour. Returns False when the budget is exhausted."""
        key = f"{PFX_REQUEST_RATE}{agent}"
        count = self.r.incr(key)
        if count == 1:
            self.r.expire(key, 3600)
        return int(count) <= REQUEST_RATE_MAX

    def create_request(self, agent: str, kind: str, target: str,
                       service: str = "", reason: str = "") -> Dict[str, Any]:
        """Record a pending request (kind: 'connection' or 'grant').

        Idempotent per (agent, kind, target): repeated calls refresh the
        timestamp/reason instead of duplicating. Bounded: per-agent and global
        caps (oldest evicted), plus a TTL so stale requests age out.
        """
        target_id = (normalize_id(target) or "UNKNOWN")[:64]
        rid = f"{kind}:{agent}:{target_id}"
        key = f"{PFX_REQUEST}{rid}"
        existing = bool(self.r.exists(key))
        mapping = {
            "agent": agent,
            "kind": kind,
            "target": target_id,
            "service": (service or "")[:64],
            "reason": (reason or "")[:500],
            "updated_at": now_iso(),
        }
        if not existing:
            mapping["created_at"] = now_iso()
        self.r.hset(key, mapping=mapping)
        self.r.expire(key, REQUEST_TTL_SECONDS)
        self.r.zadd(REQUEST_INDEX, {rid: time.time()})

        # Enforce caps: evict oldest for this agent, then oldest overall.
        agent_rids = [r for r in self.r.zrange(REQUEST_INDEX, 0, -1)
                      if r.split(":", 2)[1:2] == [agent]]
        for old in agent_rids[:max(0, len(agent_rids) - REQUEST_MAX_PER_AGENT)]:
            self.delete_request(old)
        total = self.r.zcard(REQUEST_INDEX)
        if total > REQUEST_MAX_TOTAL:
            for old in self.r.zrange(REQUEST_INDEX, 0, total - REQUEST_MAX_TOTAL - 1):
                self.delete_request(old)

        out = self.r.hgetall(key)
        out["id"] = rid
        out["already_pending"] = existing
        return out

    def get_request(self, rid: str) -> Optional[Dict[str, Any]]:
        data = self.r.hgetall(f"{PFX_REQUEST}{rid}")
        if not data:
            return None
        data["id"] = rid
        return data

    def list_requests(self) -> List[Dict[str, Any]]:
        result = []
        for rid in self.r.zrange(REQUEST_INDEX, 0, -1, desc=True):
            data = self.r.hgetall(f"{PFX_REQUEST}{rid}")
            if data:
                data["id"] = rid
                result.append(data)
            else:
                self.r.zrem(REQUEST_INDEX, rid)  # TTL-expired: prune index
        return result

    def delete_request(self, rid: str) -> None:
        self.r.delete(f"{PFX_REQUEST}{rid}")
        self.r.zrem(REQUEST_INDEX, rid)

    def pending_request_count(self) -> int:
        return int(self.r.zcard(REQUEST_INDEX) or 0)

    # -- OAuth state -----------------------------------------------------------

    def put_oauth_state(self, state: str, payload: Dict[str, Any], ttl: int = 600) -> None:
        self.r.setex(f"{PFX_OAUTH_STATE}{state}", ttl, json.dumps(payload))

    def pop_oauth_state(self, state: str) -> Optional[Dict[str, Any]]:
        key = f"{PFX_OAUTH_STATE}{state}"
        raw = self.r.get(key)
        if raw:
            self.r.delete(key)
            try:
                return json.loads(raw)
            except ValueError:
                return None
        return None


# ---------------------------------------------------------------------------
# Credential injection
# ---------------------------------------------------------------------------

class AuthInjectionError(Exception):
    pass


def build_auth(conn: Dict[str, Any], secrets: Dict[str, Any],
               access_token: Optional[str] = None) -> Dict[str, Any]:
    """Return {'headers': {...}, 'params': {...}} to attach to the upstream call."""
    auth = conn.get("auth") or {}
    kind = auth.get("kind", "bearer")
    headers: Dict[str, str] = {}
    params: Dict[str, str] = {}

    if kind == "oauth2":
        if not access_token:
            raise AuthInjectionError("No access token — connect this service first")
        headers["Authorization"] = f"Bearer {access_token}"
    elif kind == "bearer":
        key = secrets.get("api_key")
        if not key:
            raise AuthInjectionError("No API key stored for this connection")
        headers["Authorization"] = f"Bearer {key}"
    elif kind == "header":
        key = secrets.get("api_key")
        if not key:
            raise AuthInjectionError("No API key stored for this connection")
        headers[auth.get("header_name") or "Authorization"] = f"{auth.get('prefix', '')}{key}"
    elif kind == "query":
        key = secrets.get("api_key")
        if not key:
            raise AuthInjectionError("No API key stored for this connection")
        params[auth.get("param_name") or "api_key"] = key
    elif kind == "email":
        raise AuthInjectionError(
            "This is an email (IMAP/SMTP) connection — it cannot be used with "
            "the HTTP proxy. Call POST /api/vault/email/{connection} instead "
            '(e.g. {"action": "list"} or {"action": "send", ...}).'
        )
    else:
        raise AuthInjectionError(f"Unknown auth kind: {kind}")

    # Extra static headers (custom API connections) — stored with the secrets
    # because they may embed credentials; injected server-side like the key.
    extra = secrets.get("extra_headers")
    if isinstance(extra, dict):
        for hname, hval in extra.items():
            if isinstance(hname, str) and hname:
                headers[hname] = str(hval)
    return {"headers": headers, "params": params}
