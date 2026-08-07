#!/usr/bin/env python3
"""Dev modification requests — agents ask the dev team for platform changes.

When an agent hits a limitation (missing tool feature, vault capability,
platform bug), it submits a modification request here instead of giving up.
Requests are stored in the fleet-shared Redis and reviewed by the owner in
Discord via /devrequests (Approve / Deny buttons, full text shown). Approved
requests land in a queue the development environment (Replit) reads, so the
dev team can implement them — across ALL projects, not just this one.

Redis layout (shared across the whole fleet):
    devreq:seq             INCR counter for ids
    devreq:item:<id>       JSON blob of the request
    devreq:pending         list of pending ids (newest last)
    devreq:approved        list of approved ids awaiting implementation
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import registry

logger = logging.getLogger(__name__)

TOOLSET = "vault"  # ships alongside the vault tools every agent already has

TITLE_MAX = 200
DESCRIPTION_MAX = 8000
LIST_MAX = 20
PENDING_MAX = 200            # bound the pending queue — agents can't flood Redis
ITEM_TTL = 90 * 24 * 3600    # requests expire from Redis after 90 days

# ---------------------------------------------------------------------------
# Atomic dispatch enqueue — must stay in sync with services/vault/replit_mcp.py
#
# SETNX claim + LPUSH in a single Lua transaction so approval, sweep, and the
# manual /dispatch endpoint can never double-queue the same request.
# ---------------------------------------------------------------------------
_DISPATCH_QUEUE = "devreq:dispatch"
_CLAIM_PREFIX = "replitmcp:claim:"
_CLAIM_TTL = 300  # seconds — matches replit_mcp.CLAIM_TTL

_ENQUEUE_LUA = """
local claimed = redis.call("SET", KEYS[1], "1", "NX", "EX", tonumber(ARGV[1]))
if claimed then
    redis.call("LPUSH", KEYS[2], ARGV[2])
    return 1
end
return 0
"""

# ---------------------------------------------------------------------------
# Startup health check — warn early so operators notice before a /devrequests
# invocation silently fails.
# ---------------------------------------------------------------------------
_redis_url_at_import = os.getenv("REDIS_URL", "").strip()
if not _redis_url_at_import:
    logger.warning(
        "REDIS_URL is not set — request_dev_modification tool is registered "
        "but its check_fn will hide it from all agents, and any attempt to "
        "call it will return an explanatory error rather than submitting a request."
    )


def _enqueue_if_unclaimed(r, req_id: str) -> bool:
    """Atomic SETNX+LPUSH via Lua. Returns True if enqueued, False if already claimed."""
    result = r.eval(
        _ENQUEUE_LUA,
        2,
        _CLAIM_PREFIX + req_id,
        _DISPATCH_QUEUE,
        str(_CLAIM_TTL),
        req_id,
    )
    return bool(result)


def _redis():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError(
            "REDIS_URL is not configured on this agent — the dev-request tool "
            "is unavailable. To file a platform change request, tell the owner "
            "directly what you need changed, or ask them to open /devrequests "
            "in Discord to see the queue. Once REDIS_URL is set this tool will "
            "become available."
        )
    import redis  # already a runtime dependency (memory sync)
    return redis.from_url(url, decode_responses=True, socket_timeout=10)


def _agent_name() -> str:
    return os.getenv("AGENT_NAME", "").strip() or "unknown"


# ---------------------------------------------------------------------------
# Store operations (also used by the Discord /devrequests review UI)
# ---------------------------------------------------------------------------
def submit_request(title: str, description: str, project: str = "",
                   agent: str = "") -> Dict[str, Any]:
    r = _redis()
    if r.llen("devreq:pending") >= PENDING_MAX:
        raise RuntimeError(
            f"The pending request queue is full ({PENDING_MAX}). "
            "Ask the owner to review /devrequests before submitting more.")
    req_id = str(r.incr("devreq:seq"))
    item = {
        "id": req_id,
        "title": title[:TITLE_MAX],
        "description": description[:DESCRIPTION_MAX],
        "project": (project or "open-manus")[:100],
        "agent": agent or _agent_name(),
        "status": "pending",
        "created_at": int(time.time()),
    }
    r.set(f"devreq:item:{req_id}", json.dumps(item, ensure_ascii=False),
          ex=ITEM_TTL)
    r.rpush("devreq:pending", req_id)
    return item


def get_request(req_id: str) -> Optional[Dict[str, Any]]:
    raw = _redis().get(f"devreq:item:{req_id}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _clean_stale_ids(r, list_key: str) -> Tuple[int, List[str]]:
    """LREM any IDs from *list_key* whose item keys have expired or gone missing.

    Returns (removed_count, list_of_removed_ids).
    Logs each removal at WARNING so it is auditable.
    """
    all_ids = r.lrange(list_key, 0, -1)
    removed = []
    for rid in all_ids:
        if not r.exists(f"devreq:item:{rid}"):
            count = r.lrem(list_key, 0, rid)
            if count:
                logger.warning(
                    "Removed stale ID %s from %s (item key missing/expired, "
                    "removed %d occurrence(s))",
                    rid, list_key, count,
                )
                removed.append(rid)
    return len(removed), removed


def list_requests(status: str = "pending", limit: int = LIST_MAX) -> List[Dict[str, Any]]:
    r = _redis()
    if status in ("pending", "approved"):
        # Actively sweep stale IDs before reading the list so the owner sees
        # an accurate count and "no pending" is never a lie.
        _clean_stale_ids(r, f"devreq:{status}")
        ids = r.lrange(f"devreq:{status}", -limit, -1)
    else:  # any status — scan the id space via the counter
        top = int(r.get("devreq:seq") or 0)
        ids = [str(i) for i in range(max(1, top - 100), top + 1)]
    out = []
    for rid in ids:
        item = get_request(rid)
        if item and (status in ("pending", "approved") or item.get("status") == status
                     or status == "all"):
            out.append(item)
    return out[-limit:]


def queue_counts() -> Dict[str, Any]:
    """Return per-status counts including missing/expired items.

    Does NOT remove stale IDs (read-only snapshot for diagnostics).
    Live counts are post-sweep values from list_requests(); this function
    shows the raw state so the operator can see the divergence.
    """
    r = _redis()
    pending_ids = r.lrange("devreq:pending", 0, -1)
    approved_ids = r.lrange("devreq:approved", 0, -1)
    dispatch_len = r.llen(_DISPATCH_QUEUE)
    pending_live = sum(1 for rid in pending_ids if r.exists(f"devreq:item:{rid}"))
    approved_live = sum(1 for rid in approved_ids if r.exists(f"devreq:item:{rid}"))
    heartbeat = r.get("replitmcp:loop_heartbeat")
    return {
        "pending_in_list": len(pending_ids),
        "pending_live": pending_live,
        "pending_expired": len(pending_ids) - pending_live,
        "approved_in_list": len(approved_ids),
        "approved_live": approved_live,
        "approved_expired": len(approved_ids) - approved_live,
        "dispatch_backlog": dispatch_len,
        "heartbeat_ts": int(heartbeat) if heartbeat else None,
    }


def set_status(req_id: str, status: str, decided_by: str = "") -> Optional[Dict[str, Any]]:
    """Decide a PENDING request (used by the Discord approval UI).

    Atomic via LREM: removing the id from the pending list is the claim.
    If another reviewer already decided it, LREM returns 0 and we return the
    item unchanged with a "conflict" marker — no duplicate approvals.
    """
    r = _redis()
    item = get_request(req_id)
    if not item:
        return None
    if item.get("status") != "pending":
        item["conflict"] = "already decided"
        return item
    if r.lrem("devreq:pending", 0, req_id) == 0:
        item = get_request(req_id) or item
        item["conflict"] = "already decided"
        return item
    item["status"] = status
    item["decided_by"] = decided_by
    item["decided_at"] = int(time.time())
    r.set(f"devreq:item:{req_id}", json.dumps(item, ensure_ascii=False),
          ex=ITEM_TTL)
    if status == "approved":
        r.rpush("devreq:approved", req_id)
        r.ltrim("devreq:approved", -PENDING_MAX, -1)
        # Auto-dispatch: atomic Lua claim+LPUSH so concurrent sweep or manual
        # retry can't double-queue the same request.
        queued = _enqueue_if_unclaimed(r, req_id)
        if not queued:
            logger.info("Dev request %s already claimed/queued at approval time", req_id)
    return item


# ---------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------
def dev_request_tool(args: dict, **_kw) -> str:
    action = str(args.get("action") or "submit").strip().lower()
    try:
        if action == "submit":
            title = str(args.get("title") or "").strip()
            description = str(args.get("description") or "").strip()
            if not title or not description:
                return json.dumps({"error": "Both 'title' and 'description' are required. "
                                            "Describe the problem, the suggested change, and why."})
            item = submit_request(title, description,
                                  project=str(args.get("project") or "").strip())
            return json.dumps({
                "submitted": True,
                "request_id": item["id"],
                "status": "pending",
                "note": ("Request queued for owner review. Tell the owner they can "
                         "read and approve it with /devrequests in Discord. Once "
                         "approved it goes to the development team's work queue."),
            }, ensure_ascii=False)
        if action == "status":
            rid = str(args.get("request_id") or "").strip()
            if not rid:
                return json.dumps({"error": "'request_id' is required for status."})
            item = get_request(rid)
            if not item:
                return json.dumps({"error": f"No request with id {rid}."})
            return json.dumps({"request": item}, ensure_ascii=False)
        if action == "list":
            status = str(args.get("filter") or "pending").strip().lower()
            items = list_requests(status=status)
            slim = [{k: it.get(k) for k in ("id", "title", "agent", "project", "status")}
                    for it in items]
            return json.dumps({"requests": slim, "filter": status}, ensure_ascii=False)
        return json.dumps({"error": f"Unknown action '{action}'. Use submit, status, or list."})
    except Exception as e:
        logger.exception("dev request tool failed")
        return json.dumps({"error": f"Dev request failed: {e}"})


registry.register(
    name="request_dev_modification",
    toolset=TOOLSET,
    schema={
        "name": "request_dev_modification",
        "description": (
            "Submit a modification request to the development team when you hit "
            "a platform limitation — a missing tool feature, a vault capability "
            "gap, a bug in shared infrastructure, or an improvement idea (for "
            "this project or any other). Write the FULL details in "
            "'description': what you were trying to do, what failed or is "
            "missing, and the specific change you suggest. The owner reviews "
            "the complete text in Discord (/devrequests) and approves or "
            "denies; approved requests are queued for the development team to "
            "implement. Also supports checking status of an earlier request "
            "(action='status') and listing recent requests (action='list')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["submit", "status", "list"],
                },
                "title": {
                    "type": "string",
                    "description": "Short summary of the requested change (submit).",
                },
                "description": {
                    "type": "string",
                    "description": "Full details: problem, context, suggested "
                                   "modification, why it matters (submit).",
                },
                "project": {
                    "type": "string",
                    "description": "Project/system the change belongs to "
                                   "(default: open-manus).",
                },
                "request_id": {
                    "type": "string",
                    "description": "Request id to check (status).",
                },
                "filter": {
                    "type": "string",
                    "description": "list filter: pending, approved, denied, or all.",
                },
            },
            "required": ["action"],
        },
    },
    handler=dev_request_tool,
    check_fn=lambda: bool(os.getenv("REDIS_URL")),
    description="Submit modification requests to the dev team (owner-approved)",
)
