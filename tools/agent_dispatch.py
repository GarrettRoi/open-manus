#!/usr/bin/env python3
"""Discord-native inter-agent task dispatch.

Agents delegate work to each other through dispatch *chains*. Each chain is
mirrored to a thread in the fleet's shared dispatch channel (Discord is the
audit surface); Redis is the source of truth for state.

Protocol (enforced by adapter-side gating + this tool):
  * The dispatcher calls action="dispatch" -> a thread is opened in the
    dispatch channel and the order posted there, @mentioning the assignee.
  * The assignee acks with a reaction ONLY (👀), works silently, and posts
    text in the thread ONLY for a question (action="question") or the final
    result (action="complete").
  * Status is carried by reactions: 👀 received, 🔧 working, ❓ question,
    ✅ done, ❌ failed.
  * The owner's messages in any dispatch thread are authoritative steering.

Loop rails:
  * max chain depth (DISPATCH_MAX_DEPTH, default 3)
  * fan-out cap per chain (DISPATCH_MAX_FANOUT, default 5 children)
  * one-ack-per-order dedup (SETNX claim, adapter side)

Redis layout (fleet-shared):
    dispatch:seq                INCR counter for chain ids
    dispatch:chain:<id>         JSON chain record
    dispatch:children:<id>      list of child chain ids
    dispatch:active             set of active chain ids
    dispatch:inbox:<agent>      list of JSON events for <agent>'s intake watcher
    dispatch:outbox:<agent>     list of JSON Discord actions for <agent>'s adapter
    dispatch:thread:<thread_id> chain id for a Discord thread
    dispatch:roster:<agent>     JSON roster entry (published by adapters)
    dispatch:ack:<id>           SETNX one-ack-per-order claim
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

TOOLSET = "vault"  # ships with the fleet-wide toolset every agent has

TASK_MAX = 6000
RESULT_MAX = 6000
QUESTION_MAX = 2000
CHAIN_TTL = 30 * 24 * 3600      # chains expire from Redis after 30 days
INBOX_MAX = 500                 # bound per-agent queues
OUTBOX_MAX = 500

STATUS_EMOJI = {
    "pending": "📨", "acked": "👀", "working": "🔧",
    "waiting": "❓", "done": "✅", "failed": "❌", "cancelled": "🛑",
}

_ACTIVE_STATUSES = {"pending", "acked", "working", "waiting"}


def _redis():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError(
            "REDIS_URL is not configured — inter-agent dispatch is unavailable "
            "on this agent."
        )
    import redis
    return redis.from_url(url, decode_responses=True, socket_timeout=10)


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _max_depth() -> int:
    try:
        return int(os.getenv("DISPATCH_MAX_DEPTH", "3"))
    except ValueError:
        return 3


def _max_fanout() -> int:
    try:
        return int(os.getenv("DISPATCH_MAX_FANOUT", "5"))
    except ValueError:
        return 5


# ---------------------------------------------------------------------------
# Store operations (also used by the Discord adapter dispatch module)
# ---------------------------------------------------------------------------
def get_chain(r, chain_id: str) -> Optional[Dict[str, Any]]:
    raw = r.get(f"dispatch:chain:{chain_id}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def save_chain(r, chain: Dict[str, Any]) -> None:
    chain["updated_at"] = int(time.time())
    r.set(f"dispatch:chain:{chain['id']}", json.dumps(chain, ensure_ascii=False),
          ex=CHAIN_TTL)
    if chain.get("status") in _ACTIVE_STATUSES:
        r.sadd("dispatch:active", chain["id"])
    else:
        r.srem("dispatch:active", chain["id"])


def push_inbox(r, agent: str, event: Dict[str, Any]) -> None:
    key = f"dispatch:inbox:{agent.lower()}"
    r.rpush(key, json.dumps(event, ensure_ascii=False))
    r.ltrim(key, -INBOX_MAX, -1)
    r.expire(key, CHAIN_TTL)


def push_outbox(r, agent: str, action: Dict[str, Any]) -> None:
    key = f"dispatch:outbox:{agent.lower()}"
    r.rpush(key, json.dumps(action, ensure_ascii=False))
    r.ltrim(key, -OUTBOX_MAX, -1)
    r.expire(key, CHAIN_TTL)


def get_roster(r) -> List[Dict[str, Any]]:
    out = []
    for key in sorted(r.scan_iter("dispatch:roster:*", count=100)):
        raw = r.get(key)
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def roster_entry(r, agent: str) -> Optional[Dict[str, Any]]:
    raw = r.get(f"dispatch:roster:{agent.lower()}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def create_chain(r, from_agent: str, to_agent: str, task: str,
                 parent_id: str = "") -> Dict[str, Any]:
    """Create a chain record and queue the Discord-side open action.

    Raises RuntimeError on rail violations (depth / fan-out / unknown agent).
    """
    from_agent = from_agent.lower()
    to_agent = to_agent.lower()
    if to_agent == from_agent:
        raise RuntimeError("You cannot dispatch a task to yourself.")
    if not roster_entry(r, to_agent):
        known = [e.get("agent") for e in get_roster(r)]
        raise RuntimeError(
            f"Unknown agent '{to_agent}'. Known agents in the dispatch roster: "
            f"{', '.join(sorted(str(k) for k in known if k)) or '(roster empty)'}."
        )
    depth = 0
    root_id = ""
    if parent_id:
        parent = get_chain(r, parent_id)
        if not parent:
            raise RuntimeError(f"Parent chain {parent_id} not found.")
        depth = int(parent.get("depth", 0)) + 1
        root_id = parent.get("root_id") or parent["id"]
        if depth >= _max_depth():
            raise RuntimeError(
                f"Chain depth limit reached ({_max_depth()}). Do the work "
                "yourself or ask a question in the existing thread instead "
                "of dispatching deeper."
            )
        if r.llen(f"dispatch:children:{parent_id}") >= _max_fanout():
            raise RuntimeError(
                f"Fan-out limit reached ({_max_fanout()} sub-tasks for chain "
                f"{parent_id}). Wait for existing sub-tasks to finish."
            )
    chain_id = str(r.incr("dispatch:seq"))
    chain = {
        "id": chain_id,
        "root_id": root_id or chain_id,
        "parent_id": parent_id,
        "depth": depth,
        "from": from_agent,
        "to": to_agent,
        "task": task[:TASK_MAX],
        "status": "pending",
        "thread_id": "",
        "order_message_id": "",
        "agents": sorted({from_agent, to_agent}),
        "waiting_on": "",
        "question": "",
        "result": "",
        "created_at": int(time.time()),
    }
    save_chain(r, chain)
    if parent_id:
        r.rpush(f"dispatch:children:{parent_id}", chain_id)
        r.expire(f"dispatch:children:{parent_id}", CHAIN_TTL)
        # Attach every agent in the subtree to the root record so Harmony's
        # PM watcher can detect multi-agent chains.
        root = get_chain(r, root_id)
        if root:
            root["agents"] = sorted(set(root.get("agents", [])) | {from_agent, to_agent})
            save_chain(r, root)
    # The dispatcher's own adapter opens the thread + posts the order.
    push_outbox(r, from_agent, {"kind": "open_chain", "chain_id": chain_id})
    return chain


def _require_chain(r, chain_id: str) -> Dict[str, Any]:
    chain = get_chain(r, chain_id)
    if not chain:
        raise RuntimeError(f"No dispatch chain with id {chain_id}.")
    return chain


def mark_working(r, chain_id: str, agent: str) -> Dict[str, Any]:
    chain = _require_chain(r, chain_id)
    if chain["to"] != agent:
        raise RuntimeError(f"Chain {chain_id} is assigned to {chain['to']}, not you.")
    if chain["status"] in ("done", "failed", "cancelled"):
        raise RuntimeError(f"Chain {chain_id} is already {chain['status']}.")
    chain["status"] = "working"
    save_chain(r, chain)
    push_outbox(r, agent, {"kind": "react", "chain_id": chain_id,
                           "add": "🔧", "remove": "👀"})
    return chain


def ask_question(r, chain_id: str, agent: str, to: str, question: str) -> Dict[str, Any]:
    chain = _require_chain(r, chain_id)
    if agent not in (chain["to"], chain["from"]):
        raise RuntimeError(f"You are not a participant in chain {chain_id}.")
    if chain["status"] in ("done", "failed", "cancelled"):
        raise RuntimeError(f"Chain {chain_id} is already {chain['status']}.")
    to = (to or "owner").lower()
    if to not in ("owner", "garrett") and not roster_entry(r, to):
        raise RuntimeError(f"Unknown question addressee '{to}'. Use 'owner' or a roster agent name.")
    if to == "garrett":
        to = "owner"
    chain["status"] = "waiting"
    chain["waiting_on"] = to
    chain["question"] = question[:QUESTION_MAX]
    chain["asked_by"] = agent
    save_chain(r, chain)
    push_outbox(r, agent, {
        "kind": "post", "chain_id": chain_id, "mention": to,
        "text": f"❓ {question[:QUESTION_MAX]}",
        "react_add": "❓", "react_remove": "🔧",
    })
    if to != "owner":
        push_inbox(r, to, {"kind": "question", "chain_id": chain_id, "from": agent})
    return chain


def answer_question(r, chain_id: str, agent: str, answer: str) -> Dict[str, Any]:
    chain = _require_chain(r, chain_id)
    if chain.get("status") != "waiting":
        raise RuntimeError(f"Chain {chain_id} has no pending question.")
    if chain.get("waiting_on") != agent:
        raise RuntimeError(
            f"The question in chain {chain_id} is addressed to "
            f"{chain.get('waiting_on') or 'owner'}, not you.")
    asked_by = chain.get("asked_by") or chain["to"]
    chain["status"] = "working" if asked_by == chain["to"] else "acked"
    chain["waiting_on"] = ""
    chain["answer"] = answer[:QUESTION_MAX]
    save_chain(r, chain)
    push_outbox(r, agent, {
        "kind": "post", "chain_id": chain_id, "mention": asked_by,
        "text": f"💬 {answer[:QUESTION_MAX]}",
        "react_add": "", "react_remove": "",
    })
    push_inbox(r, asked_by, {"kind": "answer", "chain_id": chain_id,
                             "from": agent, "answer": answer[:QUESTION_MAX]})
    return chain


def complete_chain(r, chain_id: str, agent: str, result: str,
                   success: bool = True) -> Dict[str, Any]:
    chain = _require_chain(r, chain_id)
    if chain["to"] != agent:
        raise RuntimeError(f"Chain {chain_id} is assigned to {chain['to']}, not you.")
    if chain["status"] in ("done", "failed", "cancelled"):
        raise RuntimeError(f"Chain {chain_id} is already {chain['status']}.")
    open_children = []
    for cid in r.lrange(f"dispatch:children:{chain_id}", 0, -1):
        child = get_chain(r, cid)
        if child and child.get("status") in _ACTIVE_STATUSES:
            open_children.append(cid)
    if open_children:
        raise RuntimeError(
            f"Chain {chain_id} still has open sub-tasks: "
            f"{', '.join(open_children)}. Wait for them or cancel them first.")
    chain["status"] = "done" if success else "failed"
    chain["result"] = result[:RESULT_MAX]
    save_chain(r, chain)
    emoji = "✅" if success else "❌"
    push_outbox(r, agent, {
        "kind": "post", "chain_id": chain_id, "mention": chain["from"],
        "text": f"{emoji} **Result:** {result[:RESULT_MAX]}",
        "react_add": emoji, "react_remove": "🔧",
    })
    push_inbox(r, chain["from"], {
        "kind": "completed", "chain_id": chain_id, "from": agent,
        "success": success, "result": result[:RESULT_MAX],
    })
    return chain


def cancel_chain(r, chain_id: str, by: str, reason: str = "") -> Dict[str, Any]:
    chain = _require_chain(r, chain_id)
    if chain["status"] in ("done", "failed", "cancelled"):
        return chain
    chain["status"] = "cancelled"
    chain["result"] = (reason or "cancelled")[:RESULT_MAX]
    save_chain(r, chain)
    push_inbox(r, chain["to"], {"kind": "cancelled", "chain_id": chain_id, "by": by,
                                "reason": reason})
    return chain


def list_chains_for(r, agent: str, limit: int = 20) -> List[Dict[str, Any]]:
    agent = agent.lower()
    out = []
    for cid in r.smembers("dispatch:active"):
        chain = get_chain(r, cid)
        if chain and agent in (chain.get("from"), chain.get("to")):
            out.append(chain)
        elif chain is None:
            r.srem("dispatch:active", cid)
    out.sort(key=lambda c: c.get("created_at", 0), reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Agent-facing tool
# ---------------------------------------------------------------------------
def _slim(chain: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("id", "from", "to", "status", "task", "thread_id", "waiting_on",
            "question", "result", "depth", "parent_id")
    slim = {k: chain.get(k) for k in keys}
    if slim.get("task"):
        slim["task"] = slim["task"][:300]
    return slim


def agent_dispatch_tool(args: dict, **_kw) -> str:
    action = str(args.get("action") or "").strip().lower()
    me = _agent_name()
    try:
        r = _redis()
        if action == "dispatch":
            to = str(args.get("to") or "").strip().lower()
            task = str(args.get("task") or "").strip()
            if not to or not task:
                return json.dumps({"error": "Both 'to' (agent name) and 'task' are required."})
            chain = create_chain(r, me, to, task,
                                 parent_id=str(args.get("parent_chain_id") or "").strip())
            return json.dumps({
                "dispatched": True, "chain_id": chain["id"],
                "note": (f"Order queued for {to}. A thread will open in the "
                         "dispatch channel; you will be notified in a new turn "
                         "when the assignee finishes or asks a question. Do "
                         "NOT poll — continue with other work."),
            }, ensure_ascii=False)
        if action == "working":
            chain = mark_working(r, str(args.get("chain_id") or "").strip(), me)
            return json.dumps({"ok": True, "chain": _slim(chain)}, ensure_ascii=False)
        if action == "question":
            chain = ask_question(r, str(args.get("chain_id") or "").strip(), me,
                                 str(args.get("to") or "owner"),
                                 str(args.get("text") or "").strip())
            return json.dumps({
                "ok": True, "chain": _slim(chain),
                "note": ("Question posted in the dispatch thread. Work on this "
                         "chain is paused until the answer arrives as a new "
                         "turn — continue with other work meanwhile."),
            }, ensure_ascii=False)
        if action == "answer":
            chain = answer_question(r, str(args.get("chain_id") or "").strip(), me,
                                    str(args.get("text") or "").strip())
            return json.dumps({"ok": True, "chain": _slim(chain)}, ensure_ascii=False)
        if action == "complete":
            success = args.get("success")
            success = True if success is None else bool(success)
            chain = complete_chain(r, str(args.get("chain_id") or "").strip(), me,
                                   str(args.get("text") or "").strip() or "(no result text)",
                                   success=success)
            return json.dumps({"ok": True, "chain": _slim(chain)}, ensure_ascii=False)
        if action == "cancel":
            chain = cancel_chain(r, str(args.get("chain_id") or "").strip(), me,
                                 str(args.get("text") or ""))
            return json.dumps({"ok": True, "chain": _slim(chain)}, ensure_ascii=False)
        if action == "status":
            cid = str(args.get("chain_id") or "").strip()
            if not cid:
                return json.dumps({"error": "'chain_id' is required for status."})
            chain = _require_chain(r, cid)
            return json.dumps({"chain": _slim(chain)}, ensure_ascii=False)
        if action == "list":
            chains = list_chains_for(r, me)
            return json.dumps({"active_chains": [_slim(c) for c in chains]},
                              ensure_ascii=False)
        if action == "roster":
            roster = get_roster(r)
            slim = [{k: e.get(k) for k in ("agent", "role", "tools")}
                    for e in roster]
            return json.dumps({"roster": slim}, ensure_ascii=False)
        return json.dumps({"error": f"Unknown action '{action}'. Use dispatch, "
                                    "working, question, answer, complete, cancel, "
                                    "status, list, or roster."})
    except Exception as e:
        logger.exception("agent dispatch tool failed")
        return json.dumps({"error": f"Dispatch failed: {e}"})


registry.register(
    name="agent_dispatch",
    toolset=TOOLSET,
    schema={
        "name": "agent_dispatch",
        "description": (
            "Delegate a task to another agent in the fleet, or manage a "
            "dispatch chain you are part of. Each chain gets a Discord thread "
            "in the shared dispatch channel — that thread is the audit trail. "
            "PROTOCOL: never post chat messages in dispatch threads; all "
            "communication goes through this tool. When you RECEIVE an order "
            "you are auto-acked with 👀 — call action='working' when you "
            "start, action='question' if blocked (addressee 'owner' for the "
            "human owner, or an agent name), and action='complete' with the "
            "result when finished (success=false if you failed). When you "
            "DISPATCH (action='dispatch', with 'to' and 'task'), you'll be "
            "notified in a new turn on completion — do not poll. Peer-to-peer "
            "dispatch is preferred for simple work; check action='roster' for "
            "who can do what. If you are working a dispatched order and need "
            "to sub-delegate, pass parent_chain_id so depth/fan-out rails "
            "apply. action='list' shows your active chains."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["dispatch", "working", "question", "answer",
                             "complete", "cancel", "status", "list", "roster"],
                },
                "to": {"type": "string",
                       "description": "Target agent name (dispatch) or question addressee (question; 'owner' = human owner)."},
                "task": {"type": "string",
                         "description": "Full task description with success criteria (dispatch)."},
                "chain_id": {"type": "string",
                             "description": "Chain id (working/question/answer/complete/cancel/status)."},
                "text": {"type": "string",
                         "description": "Question text, answer text, result text, or cancel reason."},
                "success": {"type": "boolean",
                            "description": "complete: true (default) if the task succeeded."},
                "parent_chain_id": {"type": "string",
                                    "description": "dispatch: the chain you are currently working, when sub-delegating."},
            },
            "required": ["action"],
        },
    },
    handler=agent_dispatch_tool,
    check_fn=lambda: bool(os.getenv("REDIS_URL")) and bool(os.getenv("DISPATCH_CHANNEL_ID")),
    description="Discord-native inter-agent task dispatch (chains, threads, reactions)",
)
