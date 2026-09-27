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
          ex=None if chain.get("origin") == "crm" else CHAIN_TTL)
    if chain.get("status") in _ACTIVE_STATUSES:
        r.sadd("dispatch:active", chain["id"])
    else:
        r.srem("dispatch:active", chain["id"])


def save_chain_guarded(r, chain: Dict[str, Any],
                       expected_statuses: set) -> Dict[str, Any]:
    """Compare-and-save: persist *chain* only if the stored status is still in
    *expected_statuses* (WATCH/MULTI optimistic lock).

    Prevents concurrent complete/cancel/answer/ack from silently overwriting
    a terminal state. Raises RuntimeError on conflict.
    """
    import redis as _redis_mod
    key = f"dispatch:chain:{chain['id']}"
    with r.pipeline() as pipe:
        while True:
            try:
                pipe.watch(key)
                raw = pipe.get(key)
                current = None
                if raw:
                    try:
                        current = json.loads(raw).get("status")
                    except ValueError:
                        current = None
                if current is not None and current not in expected_statuses:
                    pipe.unwatch()
                    raise RuntimeError(
                        f"Chain {chain['id']} is already {current}; "
                        f"refusing transition to {chain['status']}.")
                chain["updated_at"] = int(time.time())
                pipe.multi()
                pipe.set(key, json.dumps(chain, ensure_ascii=False),
                         ex=None if chain.get("origin") == "crm" else CHAIN_TTL)
                if chain.get("status") in _ACTIVE_STATUSES:
                    pipe.sadd("dispatch:active", chain["id"])
                else:
                    pipe.srem("dispatch:active", chain["id"])
                pipe.execute()
                return chain
            except _redis_mod.WatchError:
                continue


def push_inbox(r, agent: str, event: Dict[str, Any]) -> None:
    key = f"dispatch:inbox:{agent.lower()}"
    r.rpush(key, json.dumps(event, ensure_ascii=False))
    r.ltrim(key, -INBOX_MAX, -1)
    r.expire(key, CHAIN_TTL)


def notification_chain(chain_id: str, recipient: str, task: str, event_id: str) -> dict:
    """Build a trusted CRM intake root, committed atomically with its outbox.

    This is not agent delegation (no fake sender adapter or self-dispatch).
    Subsequent agent delegation still uses create_chain's lineage/depth rails.
    """
    return {
        "id": chain_id, "root_id": chain_id, "parent_id": "", "depth": 0,
        "from": "crm-system", "to": recipient, "task": task,
        "status": "pending", "thread_id": "", "order_message_id": "",
        "agents": [recipient], "waiting_on": "", "question": "", "result": "",
        "origin": "crm", "crm_event_id": event_id, "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


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
    # Rail enforcement: parent lineage cannot be opted out of. If the
    # dispatcher is currently working a dispatched order and gave no parent,
    # its dispatch IS a sub-task of that order — auto-attach it so the depth
    # and fan-out rails apply to the whole chain, not just cooperating agents.
    if not parent_id:
        assigned = [c for c in list_chains_for(r, from_agent)
                    if c.get("to") == from_agent
                    and c.get("status") in ("acked", "working", "waiting")]
        if assigned:
            assigned.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
            parent_id = assigned[0]["id"]
    depth = 0
    root_id = ""
    if parent_id:
        parent = get_chain(r, parent_id)
        if not parent:
            raise RuntimeError(f"Parent chain {parent_id} not found.")
        if from_agent not in (parent.get("from"), parent.get("to")):
            raise RuntimeError(
                f"You are not a participant in chain {parent_id}; you cannot "
                "attach sub-tasks to it.")
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
    save_chain_guarded(r, chain, _ACTIVE_STATUSES)
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
    save_chain_guarded(r, chain, _ACTIVE_STATUSES)
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
    save_chain_guarded(r, chain, {"waiting"})
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
    save_chain_guarded(r, chain, _ACTIVE_STATUSES)
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
    if by not in (chain.get("from"), chain.get("to"), "owner"):
        raise RuntimeError(
            f"Only the dispatcher ({chain.get('from')}), the assignee "
            f"({chain.get('to')}), or the owner may cancel chain {chain_id}.")
    if chain["status"] in ("done", "failed", "cancelled"):
        return chain
    chain["status"] = "cancelled"
    chain["result"] = (reason or "cancelled")[:RESULT_MAX]
    save_chain_guarded(r, chain, _ACTIVE_STATUSES)
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
    """V2 native interface; legacy records remain readable and cancellable."""
    from tools.dispatch_tickets import TicketStore, ticket_execution_context
    action = str(args.get("action") or "").strip().lower()
    me = _agent_name()
    try:
        if action in ("search", "inspect_access", "roster"):
            from tools.vault_tools import discover_agents
            query = dict(args)
            if action == "inspect_access" and not args.get("agent"):
                raise ValueError("agent is required for inspect_access.")
            return json.dumps(discover_agents(query), ensure_ascii=False)
        r = _redis()
        tickets = TicketStore(r)
        cid = str(args.get("chain_id") or args.get("ticket_id") or "").strip()
        if action in ("dispatch", "submit"):
            if "task" in args and not args.get("objective"):
                raise ValueError("Legacy task-only dispatch is retired. Submit objective, inputs, constraints, expected_output and to; no text was truncated or queued.")
            t = tickets.submit(
                me, str(args.get("to") or ""), args.get("objective"),
                args.get("inputs"), args.get("constraints"), args.get("expected_output"),
                artifacts=args.get("artifacts"), parent_id=str(args.get("parent_chain_id") or ""),
                dedup_key=str(args.get("dedup_key") or ""))
            return json.dumps({"ticket": t, "note": "Durably queued. Result retrieval is explicit; no completion conversation is scheduled."})
        if action == "continue_parent":
            return json.dumps({"ticket": tickets.continue_parent(cid, me)})
        if action == "list":
            return json.dumps({"tickets": tickets.list_for(me, int(args.get("limit", 50))),
                               "legacy_chains": [_slim(c) for c in list_chains_for(r, me)],
                               "legacy_note": "Legacy chains retain history; waiting/question flow is retired. Cancel and submit a complete structured ticket."})
        if action in ("status", "result", "cancel", "complete", "blocked", "working",
                      "question", "answer"):
            chain = _require_chain(r, cid)
            if me not in (chain.get("from"), chain.get("to")):
                raise PermissionError("Not a ticket participant.")
            if chain.get("version") != 2:
                # Trusted CRM intake and existing legacy/dev-request callers
                # still finish already-issued work. Only conversational
                # negotiation/new legacy submission is retired.
                if action in ("working", "complete"):
                    if action == "working":
                        updated = mark_working(r, cid, me)
                    else:
                        text = str(args.get("text") or "").strip()
                        if not text or len(text) > RESULT_MAX:
                            raise ValueError(f"Legacy result requires 1..{RESULT_MAX} characters; use artifact references.")
                        updated = complete_chain(r, cid, me, text, success=args.get("success", True))
                    return json.dumps({"legacy_chain": updated,
                                       "note": "Existing work completed through compatibility path; new requests use structured tickets."})
                if action in ("status", "result"):
                    return json.dumps({"legacy_chain": chain, "migration_required": True,
                                       "note": "Legacy record preserved. Cancel and resubmit a structured ticket; no automatic question/answer migration."})
                if action == "cancel":
                    return json.dumps({"legacy_chain": cancel_chain(r, cid, me, str(args.get("text") or ""))})
                raise ValueError("Legacy workflow retired for this interface. Cancel and resubmit structured work; stored history is preserved.")
            if action in ("status", "result"):
                return json.dumps({"ticket": tickets.get(cid, me)})
            if action == "cancel":
                return json.dumps({"ticket": tickets.cancel(cid, me, str(args.get("text") or ""))})
            if action in ("question", "answer"):
                raise ValueError("Conversational negotiation is retired. Return blocked with required_inputs; requester may submit new complete work.")
            ctx = ticket_execution_context.get() or {}
            if ctx.get("ticket_id") != cid or me != chain["to"]:
                raise PermissionError("Completion requires the current fenced ticket execution context.")
            if action == "working":
                return json.dumps({"ticket": tickets.get(cid, me),
                                   "note": "Running state is recorded by durable runtime acceptance."})
            status = "blocked" if action == "blocked" else ("succeeded" if args.get("success", True) else "failed")
            if ctx.get("approval_required"):
                return json.dumps({"ticket": tickets.finish(
                    cid, ctx.get("token"), "blocked",
                    "Owner approval is required for this unattended ticket.",
                    required_inputs=["Explicit owner authorization in an interactive session"])})
            return json.dumps({"ticket": tickets.finish(cid, ctx.get("token"), status,
                              str(args.get("text") or ""), args.get("artifacts"),
                              args.get("required_inputs"))})
        raise ValueError("Unknown action. Use submit, search, inspect_access, list, status, result, complete, blocked, cancel, continue_parent.")
    except Exception as e:
        logger.exception("agent ticket tool failed")
        return json.dumps({"error": str(e)})


def _legacy_agent_dispatch_tool(args: dict, **_kw) -> str:
    """Historical implementation retained only for source compatibility."""
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
            "Submit durable targeted work with objective, inputs, constraints and "
            "expected_output. Discord and Harmony are not delivery prerequisites. "
            "Search or inspect_access for current grant-backed discovery. Return "
            "one complete result or blocked with required_inputs, never questions. "
            "Results are stored without automatic LLM wakeups. continue_parent is "
            "an explicit requester-only bounded, deduplicated synthesis request "
            "for a blocked parent whose children finished. External actions still "
            "require normal approvals; delegation never transfers grants. Legacy "
            "records remain inspectable/cancellable, not conversationally resumed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["submit", "dispatch", "search", "inspect_access",
                             "complete", "blocked", "cancel", "status", "result",
                             "list", "continue_parent"],
                },
                "objective": {"type": "string"},
                "inputs": {"type": "object", "description": "Required input values or artifact references; explicitly empty if none."},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "expected_output": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
                "required_inputs": {"type": "array", "items": {"type": "string"}},
                "dedup_key": {"type": "string"},
                "query": {"type": "string"},
                "service": {"type": "string"},
                "capability": {"type": "string"},
                "account": {"type": "string"},
                "agent": {"type": "string"},
                "limit": {"type": "integer"},
                "to": {"type": "string",
                       "description": "Explicit assignee agent name."},
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
    check_fn=lambda: bool(os.getenv("REDIS_URL")),
    description="Durable targeted agent request tickets and grant-backed discovery",
)
