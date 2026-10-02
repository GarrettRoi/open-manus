"""Narrow approved-dev-request clarification channel.

No credentials, state, or question text is written onto the dispatch request.
Admission uses one fleet-wide Redis TIME/Lua transaction; delivery uses fenced
claims and the same acceptance/reconciliation boundary as the ticket consumer.
This module is standalone because the vault image does not contain tools/.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import uuid

from redis.exceptions import WatchError

PREFIX = "devreq:clarification:"
BUDGET = PREFIX + "global-budget"
CAP_TTL = 86400
DELIVERY_TTL = 86400
TEXT_MAX = 8000
TERMINAL = {"succeeded", "failed", "blocked", "cancelled", "expired"}


class RateLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(float(retry_after)))
        super().__init__("Four developer questions already accepted in the rolling 300 seconds.")


_ADMIT = """
local capraw = redis.call('GET', KEYS[1])
local reqraw = redis.call('GET', KEYS[2])
if not capraw or not reqraw then return {'denied'} end
local cap = cjson.decode(capraw)
local req = cjson.decode(reqraw)
local tm = redis.call('TIME')
local now = tonumber(tm[1]) + tonumber(tm[2]) / 1000000
if cap.request_id ~= ARGV[1] or cap.destination ~= ARGV[2]
  or cap.expires_at <= now or req.status ~= 'approved'
  or req.dispatch_repl_id ~= cap.destination
  or req.agent ~= cap.origin then return {'denied'} end
local prior = redis.call('GET', KEYS[3])
if prior then
  local q = cjson.decode(prior)
  if q.question ~= ARGV[3] or q.destination ~= cap.destination
    or q.origin ~= cap.origin then return {'conflict'} end
  return {'ok', prior}
end
redis.call('ZREMRANGEBYSCORE', KEYS[4], '-inf', now - 300)
if redis.call('ZCARD', KEYS[4]) >= 4 then
  local first = redis.call('ZRANGE', KEYS[4], 0, 0)
  local score = redis.call('ZSCORE', KEYS[4], first[1])
  return {'limited', tostring(tonumber(score) + 300 - now)}
end
local q = cjson.decode(ARGV[4])
q.created_at = now
q.updated_at = now
q.expires_at = now + tonumber(ARGV[5])
q.delivery.next_attempt_at = now
q.to = string.lower(req.agent)
q.origin = req.agent
q.developer_id = cap.developer_id
local encoded = cjson.encode(q)
redis.call('SET', KEYS[3], encoded)
redis.call('ZADD', KEYS[4], now, q.id)
redis.call('ZADD', ARGV[6] .. q.to, now, q.id)
redis.call('ZADD', ARGV[7] .. q.to, now, q.id)
return {'ok', encoded}
"""


def _text(value, name, limit=TEXT_MAX):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} requires 1..{limit} characters.")
    return value


class ClarificationStore:
    def __init__(self, r):
        self.r = r

    def clock(self):
        seconds, micros = self.r.time()
        return int(seconds) + int(micros) / 1000000

    @staticmethod
    def _key(qid):
        return PREFIX + "item:" + qid

    @staticmethod
    def _cap(token):
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            raise PermissionError("Invalid clarification authorization.")
        return PREFIX + "cap:" + hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _request(raw, destination):
        req = json.loads(raw or "null")
        if (not req or req.get("status") != "approved"
                or not destination or req.get("dispatch_repl_id") != destination
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", req.get("agent", ""))):
            raise PermissionError("Approved request with matching pinned destination required.")
        return req

    def provision(self, request_id, destination, developer_id):
        _text(developer_id, "developer_id", 200)
        key = f"devreq:item:{request_id}"
        token = secrets.token_urlsafe(32)
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(key)
                    req = self._request(p.get(key), destination)
                    cap = dict(request_id=str(request_id), destination=destination,
                               origin=req["agent"], developer_id=developer_id,
                               expires_at=self.clock() + CAP_TTL)
                    p.multi()
                    p.set(self._cap(token), json.dumps(cap), ex=CAP_TTL)
                    p.execute()
                    return token
                except WatchError:
                    continue

    def _authorize(self, token, request_id, destination):
        cap = json.loads(self.r.get(self._cap(token)) or "null")
        req = self._request(self.r.get(f"devreq:item:{request_id}"), destination)
        if (not cap or cap["request_id"] != str(request_id)
                or cap["destination"] != destination or cap["origin"] != req["agent"]
                or cap["expires_at"] <= self.clock()):
            raise PermissionError("Invalid or expired clarification authorization.")
        return cap

    def ask(self, token, request_id, destination, question, idempotency_key):
        question = _text(question, "question")
        idem = _text(idempotency_key, "idempotency_key", 200)
        qid = "cq-" + hashlib.sha256(
            json.dumps([str(request_id), idem]).encode()).hexdigest()
        q = dict(id=qid, version=2, kind="dev_clarification",
                 request_id=str(request_id), destination=destination,
                 question=question, status="queued", answer=None,
                 answer_state="unanswered", result=None, children=[],
                 delivery=dict(state="queued", attempts=0),
                 **{"from": "dev-request"})
        result = self.r.eval(
            _ADMIT, 4, self._cap(token), f"devreq:item:{request_id}",
            self._key(qid), BUDGET, str(request_id), destination, question,
            json.dumps(q), DELIVERY_TTL, PREFIX + "queue:", PREFIX + "index:")
        state = result[0]
        if isinstance(state, bytes):
            state = state.decode()
        if state == "denied":
            raise PermissionError("Invalid or expired clarification authorization.")
        if state == "conflict":
            raise ValueError("Idempotency key belongs to a different question.")
        if state == "limited":
            raise RateLimited(result[1])
        return self._public(json.loads(result[1]))

    def read(self, token, request_id, destination, question_id):
        cap = self._authorize(token, request_id, destination)
        q = self.get(question_id)
        if (q["request_id"] != str(request_id) or q["destination"] != destination
                or q["origin"] != cap["origin"]):
            raise PermissionError("Question is outside authorization scope.")
        q = self._mutate(question_id, self._recover_one)
        return self._public(q)

    def _public(self, q):
        out = {k: v for k, v in q.items() if k not in ("origin", "developer_id")}
        out["delivery"] = {k: v for k, v in q["delivery"].items()
                           if k not in ("token", "worker_id")}
        out["recipient_online"] = bool(self.r.exists(
            f"dispatch:tickets:heartbeat:{q['to']}"))
        return out

    def get(self, question_id):
        raw = self.r.get(self._key(question_id))
        if not raw:
            raise ValueError("Clarification not found.")
        return json.loads(raw)

    def _save(self, p, q):
        q["updated_at"] = self.clock()
        p.set(self._key(q["id"]), json.dumps(q))
        queue = PREFIX + "queue:" + q["to"]
        if q["status"] == "queued" and not q["delivery"].get("token"):
            p.zadd(queue, {q["id"]: q["delivery"]["next_attempt_at"]})
        else:
            p.zrem(queue, q["id"])

    def _mutate(self, qid, fn):
        key = self._key(qid)
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(key)
                    q = json.loads(p.get(key) or "null")
                    if not q:
                        raise ValueError("Clarification not found.")
                    fn(q)
                    p.multi()
                    self._save(p, q)
                    p.execute()
                    return q
                except WatchError:
                    continue

    def heartbeat(self, agent):
        self.r.set(f"dispatch:tickets:heartbeat:{agent}", str(self.clock()), ex=90)

    def _recover_one(self, q):
        if q["status"] in TERMINAL:
            return
        d = q["delivery"]
        now = self.clock()
        if now >= q["expires_at"]:
            q.update(status="expired", answer_state="expired")
            d.update(state="expired", token=None)
        elif d.get("token") and d.get("lease_until", 0) <= now:
            if d["state"] == "accepted":
                q.update(status="blocked", answer_state="unanswered")
                d.update(state="reconciliation_required", token=None,
                         last_error="Accepted worker lost; no automatic replay.")
            elif d["attempts"] >= 5:
                q["status"] = "failed"
                d.update(state="exhausted", token=None)
            else:
                d.update(state="retrying", token=None,
                         next_attempt_at=now + min(300, 2 ** d["attempts"]))

    def recover(self, agent):
        for qid in self.r.zrange(PREFIX + "index:" + agent, 0, -1):
            self._mutate(qid, self._recover_one)

    def claim(self, agent, worker_id, lease_seconds=120):
        self.recover(agent)
        for qid in self.r.zrangebyscore(PREFIX + "queue:" + agent, "-inf",
                                       self.clock(), start=0, num=20):
            def take(q):
                d = q["delivery"]
                if (q["to"] != agent or q["status"] != "queued" or d.get("token")
                        or d["next_attempt_at"] > self.clock()):
                    raise ValueError("Not claimable.")
                d.update(token=uuid.uuid4().hex, worker_id=worker_id,
                         state="claimed", attempts=d["attempts"] + 1,
                         lease_until=self.clock() + lease_seconds)
            try:
                return self._mutate(qid, take)
            except ValueError:
                continue
        return None

    def _fence(self, q, token):
        if (q["status"] in TERMINAL or not token
                or token != q["delivery"].get("token")
                or q["delivery"].get("lease_until", 0) <= self.clock()
                or q["expires_at"] <= self.clock()):
            raise ValueError("Stale clarification execution lease.")

    def accept(self, qid, token):
        def change(q):
            self._fence(q, token)
            if q["delivery"]["state"] != "claimed":
                raise ValueError("Already accepted; do not schedule twice.")
            q["status"] = "running"
            q["delivery"].update(state="accepted", accepted_at=self.clock())
        return self._mutate(qid, change)

    def renew(self, qid, token, lease_seconds=120):
        def change(q):
            self._fence(q, token)
            q["delivery"]["lease_until"] = self.clock() + lease_seconds
        return self._mutate(qid, change)

    def finish(self, qid, token, status, result, artifacts=None, required_inputs=None):
        # Adapter acknowledgment / ordinary ticket completion is NOT an answer.
        if status not in ("failed", "blocked"):
            raise ValueError("Use the explicit clarification answer operation.")
        def change(q):
            self._fence(q, token)
            q.update(status=status, result="Execution ended without a clarification answer.")
            q["delivery"].update(state=status, token=None)
        return self._mutate(qid, change)

    def answer(self, question_id, agent, answer, idempotency_key, token):
        answer = _text(answer, "answer")
        idem = _text(idempotency_key, "idempotency_key", 200)
        def change(q):
            if q["to"] != agent:
                raise PermissionError("Only the originating agent may answer.")
            if q.get("answer") is not None:
                if q.get("answer_key") == idem and q["answer"] == answer:
                    return
                raise ValueError("An answer already exists.")
            self._fence(q, token)
            if q["delivery"]["state"] != "accepted":
                raise ValueError("Clarification has not been accepted.")
            q.update(answer=answer, answer_key=idem, answer_state="answered",
                     answered_at=self.clock(), status="succeeded")
            q["delivery"].update(state="completed", token=None)
        return self._public(self._mutate(question_id, change))