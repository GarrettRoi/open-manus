"""Durable v2 targeted work tickets. Redis is authoritative, never Discord.

Acceptance is an execution boundary: an expired accepted lease is ambiguous
and requires reconciliation, not automatic replay of possible external effects.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
import uuid

from redis.exceptions import WatchError

ticket_execution_context = contextvars.ContextVar("ticket_execution_context", default=None)
TERMINAL = {"succeeded", "failed", "blocked", "cancelled"}
MAX_ATTEMPTS = 5


class TicketStore:
    def __init__(self, r, clock=time.time):
        self.r, self.clock = r, clock

    @staticmethod
    def _key(ticket_id):
        return f"dispatch:chain:{ticket_id}"

    def get(self, ticket_id, actor=None):
        raw = self.r.get(self._key(ticket_id))
        t = json.loads(raw) if raw else None
        if not t or t.get("version") != 2:
            raise ValueError("No v2 ticket; legacy chains require the legacy migration path.")
        if actor is not None and actor not in (t["from"], t["to"], "owner"):
            raise PermissionError("Not a ticket participant.")
        return t

    def _save(self, pipe, t):
        t["updated_at"] = self.clock()
        pipe.set(self._key(t["id"]), json.dumps(t))
        queue = f"dispatch:tickets:queue:{t['to']}"
        if t["status"] == "queued" and not t["delivery"].get("token"):
            pipe.zadd(queue, {t["id"]: t["delivery"]["next_attempt_at"]})
        else:
            pipe.zrem(queue, t["id"])
        # Metadata-only audit, committed in the same transaction as state.
        # Never copy objectives/results, credentials or worker error strings.
        pipe.rpush(f"dispatch:tickets:events:{t['id']}", json.dumps({
            "at": t["updated_at"], "status": t["status"],
            "delivery_state": t["delivery"]["state"],
            "attempt": t["delivery"]["attempts"],
        }))

    def _mutate(self, ticket_id, fn):
        key = self._key(ticket_id)
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(key)
                    raw = p.get(key)
                    t = json.loads(raw) if raw else None
                    if not t or t.get("version") != 2:
                        raise ValueError("Ticket not found.")
                    before = json.dumps(t, sort_keys=True)
                    fn(t)
                    if json.dumps(t, sort_keys=True) == before:
                        p.unwatch()
                        return t
                    p.multi()
                    self._save(p, t)
                    p.execute()
                    return t
                except WatchError:
                    continue

    def submit(self, requester, assignee, objective, inputs, constraints,
               expected_output, artifacts=None, parent_id="", dedup_key="",
               _continuation=False):
        requester, assignee = requester.strip().lower(), assignee.strip().lower()
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", requester)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", assignee)
                or requester == assignee):
            raise ValueError("A distinct requester and assignee are required.")
        if not (self.r.exists(f"dispatch:roster:{assignee}")
                or self.r.zscore("vault:agents", assignee) is not None):
            raise ValueError("Unknown assignee; use grant-backed discovery first.")
        ctx = ticket_execution_context.get() or {}
        if ctx.get("ticket_id"):
            if parent_id and parent_id != ctx["ticket_id"]:
                raise ValueError("Cannot override the current execution parent.")
            parent_id = ctx["ticket_id"]
            current = self.get(parent_id)
            if current["to"] != requester:
                raise PermissionError("Execution context does not belong to requester.")
            self._fence(current, ctx.get("token"))
        if not parent_id:
            assigned = [self.get(tid) for tid in self.r.zrevrange(
                f"dispatch:tickets:index:{requester}", 0, -1)]
            running = [t for t in assigned if t["to"] == requester and t["status"] == "running"]
            if running:
                parent_id = running[0]["id"]
        for name, value in (("objective", objective), ("expected_output", expected_output)):
            if not isinstance(value, str) or not value.strip() or len(value) > 12000:
                raise ValueError(f"{name} must be nonempty text of at most 12000 characters.")
        if not isinstance(inputs, (dict, list)) or not isinstance(constraints, (list, dict)):
            raise ValueError("inputs and constraints must be explicit JSON objects or arrays.")
        self._references(artifacts)
        payload = dict(objective=objective, inputs=inputs, constraints=constraints,
                       expected_output=expected_output, artifacts=artifacts or [])
        if len(json.dumps(payload)) > 64000:
            raise ValueError("Request exceeds 64000 characters; supply artifact references.")
        fingerprint = hashlib.sha256(json.dumps(
            [requester, assignee, parent_id, payload], sort_keys=True).encode()).hexdigest()
        dk = "dispatch:tickets:dedup:" + hashlib.sha256(
            f"{requester}:{dedup_key}".encode()).hexdigest()
        parent_key = self._key(parent_id) if parent_id else "dispatch:tickets:no-parent"
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(dk, parent_key, "dispatch:seq")
                    if dedup_key and p.get(dk):
                        prior = self.get(p.get(dk), requester)
                        if prior["fingerprint"] != fingerprint:
                            raise ValueError("Dedup key already belongs to a different request.")
                        return prior
                    parent = json.loads(p.get(parent_key) or "null")
                    depth = 0
                    if parent_id:
                        if not parent or parent.get("version") != 2:
                            raise ValueError("Parent must be a v2 ticket.")
                        allowed = (requester == parent["to"] and parent["status"] == "running")
                        if _continuation:
                            allowed = (requester == parent["from"] and assignee == parent["to"]
                                       and parent["status"] == "blocked")
                        if not allowed:
                            raise ValueError("Only the running parent assignee may delegate.")
                        if ctx.get("ticket_id"):
                            self._fence(parent, ctx.get("token"))
                        depth = parent["depth"] + 1
                        if depth >= 3 or len(parent.get("children", [])) >= 5:
                            raise ValueError("Ticket depth/fan-out limit reached.")
                    tid = str(int(p.get("dispatch:seq") or 0) + 1)
                    now = self.clock()
                    t = dict(id=tid, version=2, **{"from": requester, "to": assignee},
                             **payload, fingerprint=fingerprint, status="queued",
                             parent_id=parent_id, root_id=parent["root_id"] if parent else tid,
                             depth=depth, children=[], created_at=now, updated_at=now,
                             result=None, delivery={"state": "queued", "attempts": 0,
                                                    "next_attempt_at": now})
                    p.multi()
                    p.set("dispatch:seq", tid)
                    self._save(p, t)
                    for agent in (requester, assignee):
                        p.zadd(f"dispatch:tickets:index:{agent}", {tid: now})
                    if dedup_key:
                        p.set(dk, tid)
                    if parent:
                        parent.setdefault("children", []).append(tid)
                        self._save(p, parent)
                    p.execute()
                    return t
                except WatchError:
                    continue

    def continue_parent(self, parent_id, actor):
        """Explicit new bounded work, never automatic resurrection/re-execution.

        The requester must deliberately request synthesis after a blocked parent.
        Stable dedup key permits exactly one continuation per parent; depth and
        fan-out limits also apply to continuations.
        """
        parent = self.get(parent_id, actor)
        if actor != parent["from"]:
            raise PermissionError("Only the original requester may request continuation.")
        dedup = hashlib.sha256(f"{actor}:continuation:{parent_id}".encode()).hexdigest()
        prior = self.r.get(f"dispatch:tickets:dedup:{dedup}")
        if prior:
            return self.get(prior, actor)
        children = [self.get(cid) for cid in parent.get("children", [])]
        if not children or any(c["status"] not in TERMINAL for c in children):
            raise ValueError("Continuation requires finished child tickets.")
        return self.submit(
            actor, parent["to"],
            "Continue parent work by synthesizing stored child results. Do not repeat completed external actions.",
            {"parent_id": parent_id, "objective": parent["objective"],
             "previous_result": parent["result"],
             "children": [{"id": c["id"], "status": c["status"], "result": c["result"]}
                          for c in children]},
            parent["constraints"], parent["expected_output"],
            parent_id=parent_id, dedup_key=f"continuation:{parent_id}", _continuation=True)

    def list_for(self, actor, limit=50):
        ids = self.r.zrevrange(f"dispatch:tickets:index:{actor}", 0, max(1, min(100, limit)) - 1)
        return [self.get(tid, actor) for tid in ids]

    def heartbeat(self, agent, ttl=90):
        self.r.set(f"dispatch:tickets:heartbeat:{agent}", str(self.clock()), ex=ttl)

    def claim(self, agent, worker_id, lease_seconds=120):
        self.recover(agent)
        ids = self.r.zrangebyscore(f"dispatch:tickets:queue:{agent}", "-inf", self.clock(),
                                  start=0, num=20)
        for tid in ids:
            def take(t):
                d = t["delivery"]
                if (t["to"] != agent or t["status"] != "queued" or d.get("token")
                        or d["next_attempt_at"] > self.clock()):
                    raise ValueError("Not claimable.")
                d.update(token=uuid.uuid4().hex, worker_id=worker_id,
                         lease_until=self.clock() + lease_seconds, state="claimed",
                         attempts=d["attempts"] + 1)
            try:
                return self._mutate(tid, take)
            except ValueError:
                continue
        return None

    def _fence(self, t, token):
        d = t["delivery"]
        if (t["status"] in TERMINAL or not token or d.get("token") != token
                or d.get("lease_until", 0) <= self.clock()):
            raise ValueError("Stale execution lease or terminal ticket.")

    def accept(self, ticket_id, token):
        def change(t):
            self._fence(t, token)
            if t["delivery"]["state"] != "claimed":
                raise ValueError("Already accepted; do not schedule twice.")
            t["status"] = "running"
            t["started_at"] = self.clock()
            t["delivery"].update(state="accepted", accepted_at=self.clock())
        return self._mutate(ticket_id, change)

    def renew(self, ticket_id, token, lease_seconds=120):
        def change(t):
            self._fence(t, token)
            t["delivery"]["lease_until"] = self.clock() + lease_seconds
        return self._mutate(ticket_id, change)

    def finish(self, ticket_id, token, status, result, artifacts=None, required_inputs=None):
        if status not in {"succeeded", "failed", "blocked"}:
            raise ValueError("Result status must be succeeded, failed or blocked.")
        if not isinstance(result, str) or not result.strip() or len(result) > 64000:
            raise ValueError("Result must be nonempty text up to 64000 characters.")
        self._references(artifacts)
        if (required_inputs is not None and (
                not isinstance(required_inputs, list)
                or any(not isinstance(x, str) or not x.strip() or len(x) > 2000
                       for x in required_inputs))):
            raise ValueError("required_inputs must be an array of nonempty strings.")
        if status == "blocked" and not required_inputs:
            raise ValueError("Blocked results require a nonempty required_inputs list.")
        def change(t):
            self._fence(t, token)
            if t["status"] != "running":
                raise ValueError("Ticket not accepted.")
            if status == "succeeded" and any(
                    self.get(cid)["status"] not in TERMINAL for cid in t["children"]):
                raise ValueError("Cannot succeed while child tickets remain active.")
            t.update(status=status, result=result, result_artifacts=artifacts or [],
                     required_inputs=required_inputs or [], completed_at=self.clock())
            t["delivery"].update(state="completed", token=None)
        return self._mutate(ticket_id, change)

    @staticmethod
    def _references(refs):
        if refs is not None and (not isinstance(refs, list) or len(refs) > 100 or any(
                not isinstance(ref, str) or not ref.strip() or len(ref) > 2000
                for ref in refs)):
            raise ValueError("artifacts must contain at most 100 nonempty string references (max 2000 characters each).")

    def _retry(self, t, reason):
        d = t["delivery"]
        d.update(token=None, last_error=reason)
        if d["attempts"] >= MAX_ATTEMPTS:
            t.update(status="failed", result="Delivery exhausted before acceptance.",
                     completed_at=self.clock())
            d["state"] = "exhausted"
        else:
            d.update(state="retrying", next_attempt_at=self.clock() + min(300, 2 ** d["attempts"]))

    def retry(self, ticket_id, token, reason):
        def change(t):
            self._fence(t, token)
            if t["delivery"]["state"] != "claimed":
                raise ValueError("Accepted execution requires reconciliation, not retry.")
            self._retry(t, reason)
        return self._mutate(ticket_id, change)

    def recover(self, agent):
        # Scan durable recipient index, including claims removed from ready queue.
        for tid in self.r.zrange(f"dispatch:tickets:index:{agent}", 0, -1):
            def change(t):
                d = t["delivery"]
                if (t["to"] != agent or t["status"] in TERMINAL or not d.get("token")
                        or d.get("lease_until", 0) > self.clock()):
                    return
                if d["state"] == "accepted":
                    t.update(status="blocked", result="Execution lease expired after acceptance; reconcile external effects before issuing new work.",
                             completed_at=self.clock())
                    d.update(state="reconciliation_required", token=None,
                             last_error="Accepted worker lost; execution outcome unknown.")
                else:
                    self._retry(t, "Worker lease expired before acceptance.")
            self._mutate(tid, change)

    def cancel(self, ticket_id, actor, reason=""):
        def change(t):
            if actor not in (t["from"], t["to"], "owner"):
                raise PermissionError("Not a participant.")
            if t["status"] in TERMINAL:
                return
            accepted = t["delivery"]["state"] == "accepted"
            t.update(status="cancelled", result=reason or "Cancelled", completed_at=self.clock())
            t["delivery"].update(token=None, state="cancelled",
                                 last_error="Cancellation cannot undo external effects." if accepted else "")
        return self._mutate(ticket_id, change)