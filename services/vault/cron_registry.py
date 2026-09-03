"""Redis-backed read/control plane for the fleet cron registry."""
import json
import re
import time
import uuid
from datetime import datetime, timezone


class CronRegistryStore:
    PREFIX = "fleet:cron:v1"

    def __init__(self, redis_client, heartbeat_lease=180, audit_limit=2000):
        self.r = redis_client
        self.heartbeat_lease = heartbeat_lease
        self.audit_limit = audit_limit

    def _key(self, suffix):
        return f"{self.PREFIX}:{suffix}"

    def _decode(self, value, default=None):
        try:
            return json.loads(value) if value else (default if default is not None else {})
        except (TypeError, ValueError):
            return default if default is not None else {}

    @staticmethod
    def _number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return 0

    def _operation_id(self, idempotency_key):
        if idempotency_key:
            return str(uuid.uuid5(uuid.NAMESPACE_URL, "fleet-cron:" + idempotency_key))
        return str(uuid.uuid4())

    @staticmethod
    def _validate_identifier(value, label):
        value = str(value or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
            raise ValueError(f"Invalid {label}")
        return value

    def _audit(self, entry):
        self.r.lpush(self._key("audit"), json.dumps(entry, separators=(",", ":")))
        self.r.ltrim(self._key("audit"), 0, self.audit_limit - 1)

    def list(self, agent=None, state=None, search=None):
        now = time.time()
        agents = []
        jobs = []
        for name in sorted(self.r.smembers(self._key("agents"))):
            if agent and name != agent:
                continue
            meta = self.r.hgetall(self._key("agent:" + name))
            heartbeat = self._number(meta.get("heartbeat_epoch")) or self._number(meta.get("heartbeat_at"))
            online = bool(heartbeat and now - heartbeat <= self.heartbeat_lease)
            item = dict(meta)
            item.update({"name": name, "online": online, "stale": not online})
            agents.append(item)
            snapshots = self.r.hgetall(self._key("jobs:" + name))
            desired = self.r.hgetall(self._key("desired:" + name))
            tombstones = self.r.hgetall(self._key("tombstones:" + name))
            for job_id, raw in snapshots.items():
                snapshot = self._decode(raw)
                wanted = self._decode(desired.get(job_id), None)
                revision = int(
                    snapshot.get("control_revision", snapshot.get("revision", 0))
                    or 0
                )
                desired_revision = int((wanted or {}).get("revision", 0) or 0)
                enabled = snapshot.get("enabled", True)
                pending = bool(wanted and desired_revision > revision)
                effective = wanted.get("enabled") if pending else enabled
                row = {
                    "key": f"{name}:{job_id}", "agent": name, "job_id": job_id,
                    "snapshot": snapshot, "desired": wanted, "enabled": bool(enabled),
                    "desired_enabled": effective, "pending": pending, "online": online,
                    "stale": not online, "tombstone": self._decode(tombstones.get(job_id), None),
                    "control_revision": max(revision, desired_revision),
                }
                haystack = " ".join([
                    name,
                    job_id,
                    str(snapshot.get("name", "")),
                    str(snapshot.get("schedule", "")),
                    str(snapshot.get("schedule_display", "")),
                    str(snapshot.get("description", "")),
                    " ".join(map(str, snapshot.get("skills") or [])),
                ]).lower()
                status = "pending" if pending else ("enabled" if effective else "paused")
                if state and state != "all" and state != status:
                    continue
                if search and search.lower() not in haystack:
                    continue
                jobs.append(row)
        freeze = self._decode(self.r.hgetall(self._key("freeze")) and
                              json.dumps(self.r.hgetall(self._key("freeze"))), {})
        return {"agents": agents, "jobs": jobs, "freeze": freeze,
                "stats": {"total": len(jobs), "enabled": sum(x["enabled"] for x in jobs),
                          "paused": sum(not x["enabled"] for x in jobs),
                          "pending": sum(x["pending"] for x in jobs),
                          "stale_agents": sum(x["stale"] for x in agents)}}

    def toggle(self, agent, job_id, enabled, actor="admin", reason="", expected_revision=None,
               idempotency_key=None):
        agent = self._validate_identifier(agent, "agent")
        job_id = self._validate_identifier(job_id, "job id")
        jobs_key, desired_key = self._key("jobs:" + agent), self._key("desired:" + agent)
        op_id = self._operation_id(idempotency_key)
        while True:
            pipe = self.r.pipeline()
            try:
                pipe.watch(jobs_key, desired_key)
                snapshot_raw = pipe.hget(jobs_key, job_id)
                if not snapshot_raw:
                    pipe.unwatch()
                    self._audit({
                        "operation_id": op_id,
                        "actor": actor,
                        "action": "cron_toggle_rejected",
                        "agent": agent,
                        "job_id": job_id,
                        "reason": "not_found",
                        "at": datetime.now(timezone.utc).isoformat(),
                    })
                    return {"agent": agent, "job_id": job_id, "status": "not_found"}
                snapshot = self._decode(snapshot_raw)
                prior = self._decode(pipe.hget(desired_key, job_id), None)
                current = max(
                    int(snapshot.get(
                        "control_revision", snapshot.get("revision", 0)
                    ) or 0),
                    int((prior or {}).get("revision", 0) or 0),
                )
                if prior and prior.get("operation_id") == op_id and bool(prior.get("enabled")) == bool(enabled):
                    pipe.unwatch()
                    return {"agent": agent, "job_id": job_id, "status": "idempotent",
                            "revision": prior["revision"], "operation_id": op_id}
                if expected_revision is not None and int(expected_revision) != current:
                    pipe.unwatch()
                    self._audit({
                        "operation_id": op_id,
                        "actor": actor,
                        "action": "cron_toggle_rejected",
                        "agent": agent,
                        "job_id": job_id,
                        "reason": "revision_conflict",
                        "expected_revision": expected_revision,
                        "current_revision": current,
                        "at": datetime.now(timezone.utc).isoformat(),
                    })
                    return {"agent": agent, "job_id": job_id, "status": "conflict",
                            "expected_revision": expected_revision, "current_revision": current}
                desired = {"enabled": bool(enabled), "revision": current + 1, "operation_id": op_id,
                           "actor": actor, "reason": reason, "requested_at": datetime.now(timezone.utc).isoformat()}
                pipe.multi()
                pipe.hset(desired_key, job_id, json.dumps(desired, separators=(",", ":")))
                pipe.execute()
                self._audit({"operation_id": op_id, "actor": actor, "action": "cron_toggle",
                             "agent": agent, "job_id": job_id, "enabled": bool(enabled),
                             "revision": current + 1, "at": desired["requested_at"]})
                return {"agent": agent, "job_id": job_id, "status": "ok", **desired}
            except Exception as exc:
                # WatchError is deliberately retried; do not hide Redis errors.
                if exc.__class__.__name__ == "WatchError":
                    continue
                raise

    def bulk(self, enabled, keys=None, filters=None, actor="admin", reason="", idempotency_key=None,
             freeze=None):
        filters = filters or {}
        selected = list(dict.fromkeys(keys or []))
        listing = self.list(
            **({} if selected else {
                "agent": filters.get("agent"),
                "state": filters.get("state"),
                "search": filters.get("search"),
            })
        )
        by_key = {j["key"]: j for j in listing["jobs"]}
        targets = (
            [by_key[key] for key in selected if key in by_key]
            if selected else listing["jobs"]
        )
        outcomes = []
        for key in selected:
            if key not in by_key:
                outcomes.append({"key": key, "status": "not_found"})
        outcomes.extend([
            self.toggle(
                j["agent"], j["job_id"], enabled, actor, reason,
                j["control_revision"],
                (idempotency_key + ":" + j["key"]) if idempotency_key else None,
            )
            for j in targets
        ])
        is_fleet_wide = (not selected and not filters.get("agent") and not filters.get("search")
                         and filters.get("state") in (None, "", "all"))
        if freeze is not None and is_fleet_wide:
            self.set_freeze(freeze, actor, reason, idempotency_key)
        self._audit({
            "operation_id": self._operation_id(idempotency_key),
            "actor": actor,
            "action": "cron_bulk_request",
            "enabled": bool(enabled),
            "target_count": len(selected) if selected else len(targets),
            "outcome_counts": {
                status: sum(x.get("status") == status for x in outcomes)
                for status in {"ok", "idempotent", "conflict", "not_found"}
            },
            "at": datetime.now(timezone.utc).isoformat(),
        })
        return {"outcomes": outcomes, "count": len(outcomes)}

    def set_freeze(self, enabled, actor="admin", reason="", idempotency_key=None):
        key, op_id = self._key("freeze"), self._operation_id(idempotency_key)
        while True:
            pipe = self.r.pipeline()
            try:
                pipe.watch(key)
                current = pipe.hgetall(key)
                if current.get("operation_id") == op_id and current.get("enabled") == str(bool(enabled)).lower():
                    pipe.unwatch()
                    return {**current, "enabled": bool(enabled), "revision": int(current["revision"]),
                            "status": "idempotent"}
                revision = int(current.get("revision", 0) or 0) + 1
                payload = {"enabled": str(bool(enabled)).lower(), "revision": str(revision),
                           "operation_id": op_id, "actor": actor, "reason": reason,
                           "requested_at": datetime.now(timezone.utc).isoformat()}
                pipe.multi()
                pipe.hset(key, mapping=payload)
                pipe.execute()
                self._audit({"operation_id": op_id, "actor": actor, "action": "cron_freeze",
                             "enabled": bool(enabled), "revision": revision, "at": payload["requested_at"]})
                return {**payload, "enabled": bool(enabled), "revision": revision}
            except Exception as exc:
                if exc.__class__.__name__ == "WatchError":
                    continue
                raise