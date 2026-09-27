"""Grant-scoped, owner-approved public agent discovery metadata.

Only the allowlisted fields below are ever rendered; vault connection objects
and credentials must never be returned by this module.
"""

import json
import math
import re
import time
from datetime import datetime

from fastapi import HTTPException

KEY_PREFIX = "vault:discovery:"
MAX_CONNECTIONS = 20
MAX_PUBLIC_CONNECTIONS = 200
MAX_TAGS = 20
MAX_LIMIT = 20
HEARTBEAT_FRESH_SECONDS = 180
HEALTH_FRESH_SECONDS = 3600
_TAG = re.compile(r"^[a-z0-9][a-z0-9 _./+-]{0,63}$")
_EMAIL = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_ACCOUNT = re.compile(r"^@?[a-z0-9][a-z0-9_.-]{1,31}$")


def _public_account(value):
    return (isinstance(value, str) and len(value) <= 100
            and bool(_EMAIL.fullmatch(value) or _ACCOUNT.fullmatch(value)))


def validate_metadata(body, agent, store):
    """Validate explicit owner allowlist, never infer account identity."""
    if not isinstance(body, dict) or set(body) != {"connections"}:
        raise HTTPException(400, "Expected only 'connections'")
    entries = body["connections"]
    if not isinstance(entries, list) or len(entries) > MAX_CONNECTIONS:
        raise HTTPException(400, "connections must be a list of at most 20")
    seen = set()
    clean = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "connection_id", "capabilities", "shareable_accounts"
        }:
            raise HTTPException(400, "Each connection needs connection_id, capabilities, shareable_accounts")
        cid = item["connection_id"]
        if not isinstance(cid, str) or not cid or len(cid) > 100 or cid in seen:
            raise HTTPException(400, "Invalid or duplicate connection_id")
        seen.add(cid)
        if not store.get(cid) or not store.has_grant(agent, cid):
            raise HTTPException(400, "Connection does not exist or is not granted to agent")
        for field in ("capabilities", "shareable_accounts"):
            values = item[field]
            if (not isinstance(values, list) or len(values) > MAX_TAGS
                    or any(not isinstance(v, str) or
                           not (_TAG.fullmatch(v.lower()) if field == "capabilities"
                                else _public_account(v.lower())) for v in values)):
                raise HTTPException(400, f"Invalid {field}; use at most 20 short public values")
            if len(set(v.lower() for v in values)) != len(values):
                raise HTTPException(400, f"Duplicate {field}")
        clean.append({
            "connection_id": cid,
            "capabilities": [v.lower() for v in item["capabilities"]],
            "shareable_accounts": [v.lower() for v in item["shareable_accounts"]],
        })
    return {"connections": clean}


def _availability(r, agent):
    raw = r.get(f"dispatch:availability:{agent}")
    try:
        updated = float(json.loads(raw).get("updated_at")) if raw else 0
    except (ValueError, TypeError, AttributeError):
        updated = 0
    if not math.isfinite(updated) or updated < 0:
        updated = 0
    age = time.time() - updated
    state = ("unknown" if not updated else "online" if 0 <= age <= HEARTBEAT_FRESH_SECONDS
             else "stale")
    return {"state": state,
            "updated_at": int(updated) if updated > 0 else None}


def _health(conn, now):
    """Only a vault probe is health evidence; configured status is separate."""
    raw = conn.get("last_test_at")
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > now:
            raise ValueError("invalid probe timestamp")
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"state": "unknown", "checked_at": None, "freshness": "unknown"}
    observed = conn.get("last_test_ok")
    state = {"1": "verified", "0": "failed"}.get(observed, "unknown")
    return {"state": state, "checked_at": raw if state != "unknown" else None,
            "freshness": ("fresh" if now - timestamp <= HEALTH_FRESH_SECONDS
                          else "stale") if state != "unknown" else "unknown"}


def public_agent(r, store, agent):
    """Read fresh grants on every request. Stale metadata cannot bypass revocation."""
    now = time.time()
    try:
        metadata = json.loads(r.get(f"{KEY_PREFIX}{agent}") or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    raw_entries = metadata.get("connections")
    if not isinstance(raw_entries, list):
        raw_entries = []
    approved = {}
    for entry in raw_entries[:MAX_CONNECTIONS]:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("connection_id")
        if not isinstance(cid, str):
            continue
        capabilities = entry.get("capabilities")
        accounts = entry.get("shareable_accounts")
        if (not isinstance(capabilities, list) or not isinstance(accounts, list)
                or len(capabilities) > MAX_TAGS or len(accounts) > MAX_TAGS
                or any(not isinstance(v, str) or not _TAG.fullmatch(v)
                       for v in capabilities)
                or any(not _public_account(v) for v in accounts)):
            continue
        approved[cid] = {"capabilities": capabilities, "shareable_accounts": accounts}
    connections = []
    ids = sorted(store.list_ids())
    truncated = False
    for cid in ids:
        if not store.has_grant(agent, cid):
            continue
        if len(connections) >= MAX_PUBLIC_CONNECTIONS:
            truncated = True
            break
        conn = store.get(cid)
        if not conn:
            continue
        extras = approved.get(cid, {})
        connections.append({
            "service": str(conn.get("service") or "")[:64],
            "granted": True,
            "capabilities": extras.get("capabilities", []),
            "shareable_accounts": extras.get("shareable_accounts", []),
            "configuration": ("ready" if conn.get("status") == "ready"
                              else "not_ready" if conn.get("status") in
                              ("needs_login", "needs_reauth", "error")
                              else "unknown"),
            "health": _health(conn, now),
        })
    connections.sort(key=lambda c: (c["service"], c["capabilities"], c["shareable_accounts"]))
    return {"agent": agent, "checked_at": int(now), "availability": _availability(r, agent),
            "connections": connections, "truncated": truncated}


def search_agents(r, store, agents, *, query="", service="", capability="", account="", limit=10):
    """Deterministic bounded matching, with no private search corpus."""
    filters = [query, service, capability, account]
    if any(not isinstance(v, str) or len(v) > 100 for v in filters):
        raise HTTPException(400, "Search fields must be strings of at most 100 characters")
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid limit")
    if not 1 <= limit <= MAX_LIMIT:
        raise HTTPException(400, "limit must be between 1 and 20")
    q, svc, cap, acct = [v.strip().lower() for v in filters]
    results = []
    for agent in sorted(agents):
        view = public_agent(r, store, agent)
        matches = []
        for conn in view["connections"]:
            if svc and svc not in conn["service"].lower():
                continue
            if cap and not any(cap in c.lower() for c in conn["capabilities"]):
                continue
            if acct and not any(acct in a.lower() for a in conn["shareable_accounts"]):
                continue
            if q and not any(q in v.lower() for v in
                             [agent, conn["service"], *conn["capabilities"],
                              *conn["shareable_accounts"]]):
                continue
            matches.append(conn)
        if matches:
            view["connections"] = matches
            results.append(view)
    return {"results": results[:limit], "total": len(results)}