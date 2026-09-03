"""Best-effort fleet cron registry.

This module deliberately has no import-time Redis or cron-jobs dependency: cron
must remain entirely usable on installations which do not use a fleet vault.
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PREFIX = "fleet:cron:v1"
VERSION = "1"
_sync_lock = threading.Lock()
_publish_lock = threading.Lock()
_sync_pending = False
_sync_requested = False


def _agent():
    """Return a conservative normalized agent name, or None.

    Do not invent an identity: publishing under ``unknown`` joins otherwise
    unrelated profile installations into one control plane.
    """
    raw = os.environ.get("AGENT_NAME", "").strip().lower()
    if not raw:
        return None
    value = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip(".-")
    return value[:128] or None


def _redis():
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis  # optional dependency
        return redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.75,
            retry_on_timeout=False,
        )
    except Exception as exc:
        logger.debug("Fleet cron registry unavailable: %s", exc)
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_prompt(value):
    text = str(value or "")
    # Avoid making an accidental pasted credential searchable in the fleet.
    text = re.sub(
        r"""(?ix)
        (["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|
        password|passwd|authorization|private[_-]?key)["']?\s*[:=]\s*)
        (["']?)[^"'\s,}\]]+\2
        """,
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(?:sk|pk|rk|ghp|gho|github_pat|xox[baprs]|AKIA)[-_A-Za-z0-9]{12,}\b",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@:\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    text = re.sub(
        r"-----BEGIN [^-]{1,40} PRIVATE KEY-----.*?-----END [^-]{1,40} PRIVATE KEY-----",
        "[redacted private key]",
        text,
        flags=re.DOTALL,
    )
    # Opaque high-entropy values are more likely to be credentials than useful
    # prose. Redacting them is preferable to leaking them into shared Redis/LLM.
    text = re.sub(
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_./+=-]{24,}(?![A-Za-z0-9]))"
        r"(?=[A-Za-z0-9_./+=-]*[A-Z])(?=[A-Za-z0-9_./+=-]*[a-z])"
        r"(?=[A-Za-z0-9_./+=-]*\d)[A-Za-z0-9_./+=-]+",
        "[redacted]",
        text,
    )
    return text[:1000]


def _description(job):
    safe_schedule = _safe_prompt(
        job.get("schedule_display")
        or json.dumps(job.get("schedule") or {}, sort_keys=True, default=str)
    )
    safe_skills = [_safe_prompt(x)[:80] for x in (job.get("skills") or [])]
    safe_name = _safe_prompt(job.get("name"))[:120]
    relevant = {
        "schedule": safe_schedule,
        "prompt": _safe_prompt(job.get("prompt")),
        "skills": safe_skills,
        "no_agent": job.get("no_agent"),
        "name": safe_name,
    }
    fingerprint = hashlib.sha256(
        json.dumps(relevant, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    schedule = safe_schedule or "unscheduled"
    subject = _safe_prompt(job.get("prompt")).replace("\n", " ").strip()[:180] or "no prompt"
    skills = ", ".join(safe_skills) or "none"
    return fingerprint, f"- Schedule: {schedule}\n- Task: {subject}\n- Skills: {skills}"


def schedule_llm_description(job, fingerprint):
    """Generate and cache a human description outside the cron hot path.

    The auxiliary client owns provider/auth resolution, so this helper never
    handles model credentials.  Any failure leaves the deterministic summary
    in place and cannot affect scheduling.
    """
    client = _redis()
    if client is None:
        return False
    prompt = _safe_prompt(job.get("prompt"))
    name = _safe_prompt(job.get("name"))[:120]
    schedule = _safe_prompt(
        job.get("schedule_display")
        or json.dumps(job.get("schedule") or {}, sort_keys=True, default=str)
    )[:240]
    skills = ", ".join(_safe_prompt(x)[:80] for x in (job.get("skills") or [])) or "none"
    if "[redacted" in " ".join((prompt, name, schedule, skills)).lower():
        return False
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize a recurring automation as 2-4 short bullet steps. "
                "Start every line with '- '. Describe actions only; do not "
                "mention implementation, credentials, or speculate. Return "
                "plain text bullets and nothing else."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Name: {name}\n"
                f"Schedule: {schedule or 'unscheduled'}\n"
                f"Skills: {skills[:240]}\n"
                f"Instruction: {prompt[:1000]}"
            ),
        },
    ]
    try:
        from agent.auxiliary_client import call_llm
        response = call_llm(
            task="cron_description",
            messages=messages,
            max_tokens=220,
            temperature=0.1,
            timeout=30,
        )
        content = (response.choices[0].message.content or "").strip()
        try:
            from agent.agent_runtime_helpers import strip_think_blocks
            content = strip_think_blocks(None, content).strip()
        except Exception:
            pass
        lines = []
        for raw in content.splitlines():
            clean = _safe_prompt(raw).strip().lstrip("•*0123456789. ").strip()
            if clean:
                lines.append(f"- {clean[:240]}")
            if len(lines) == 4:
                break
        if len(lines) < 2:
            return False
        description = "\n".join(lines)
        client.set(
            f"{PREFIX}:description:{fingerprint}",
            description,
            ex=86400 * 30,
        )
        client.set(
            f"{PREFIX}:description-source:{fingerprint}",
            "llm",
            ex=86400 * 30,
        )
        return True
    except Exception as exc:
        logger.debug("Fleet cron LLM description generation skipped: %s", exc)
        return False


def _snapshot(job, client=None):
    fingerprint, deterministic = _description(job)
    description = deterministic
    source = "deterministic"
    if client is not None:
        try:
            cached = client.get(f"{PREFIX}:description:{fingerprint}")
            if cached:
                description = cached
                source = client.get(
                    f"{PREFIX}:description-source:{fingerprint}"
                ) or "deterministic"
            else:
                # The deterministic description is useful immediately.  A future
                # plugin LLM hook may replace this cache asynchronously; it must
                # never run in this mutation/ticker path.
                client.set(
                    f"{PREFIX}:description:{fingerprint}",
                    description,
                    ex=86400,
                )
                client.set(
                    f"{PREFIX}:description-source:{fingerprint}",
                    "deterministic",
                    ex=86400,
                )
                # Claim enrichment so overlapping snapshots do not fan out one
                # LLM call per ticker/mutation.
                claimed = client.set(
                    f"{PREFIX}:description-pending:{fingerprint}",
                    "1",
                    ex=300,
                    nx=True,
                )
                if claimed:
                    def enrich():
                        try:
                            schedule_llm_description(dict(job), fingerprint)
                        finally:
                            try:
                                cache = _redis()
                                if cache is not None:
                                    cache.delete(
                                        f"{PREFIX}:description-pending:{fingerprint}"
                                    )
                            except Exception:
                                pass
                    threading.Thread(
                        target=enrich,
                        daemon=True,
                        name="fleet-cron-description",
                    ).start()
        except Exception as exc:
            logger.debug("Fleet cron description cache skipped: %s", exc)
    return {
        "id": str(job.get("id", "")), "name": job.get("name") or job.get("id", ""),
        "schedule": job.get("schedule") or {}, "schedule_display": job.get("schedule_display"),
        "enabled": bool(job.get("enabled", True)), "state": job.get("state"),
        "next_run_at": job.get("next_run_at"), "last_run_at": job.get("last_run_at"),
        "skills": [_safe_prompt(x)[:80] for x in (job.get("skills") or [])],
        "no_agent": bool(job.get("no_agent", False)), "description": description,
        "description_source": source, "description_fingerprint": fingerprint,
        "control_revision": int(job.get("registry_control_revision") or 0),
        "updated_at": job.get("updated_at") or _now(),
    }


def publish_snapshot(jobs=None):
    """Publish this profile's current jobs.  All failures are intentionally soft."""
    agent = _agent()
    client = _redis()
    if not agent or client is None:
        return False
    try:
        # All production callers publish from the current jobs file. Serializing
        # the read+write prevents an older async sync from resurrecting a job
        # after a newer deletion snapshot.
        with _publish_lock:
            if jobs is None:
                from cron.jobs import load_jobs
                jobs = load_jobs()
            jobs = list(jobs or [])
            now = _now()
            job_key = f"{PREFIX}:jobs:{agent}"
            tombstone_key = f"{PREFIX}:tombstones:{agent}"
            agent_key = f"{PREFIX}:agent:{agent}"
            snapshot_revision = client.hincrby(agent_key, "snapshot_revision", 1)
            current = {str(j.get("id")) for j in jobs if j.get("id")}
            previous = set(client.hkeys(job_key))
            pipe = client.pipeline()
            pipe.sadd(f"{PREFIX}:agents", agent)
            home = os.environ.get("HERMES_HOME", "")
            pipe.hset(agent_key, mapping={
                "heartbeat_at": now, "heartbeat_epoch": str(time.time()),
                "snapshot_revision": str(snapshot_revision),
                "job_count": str(len(current)),
                "profile": home, "HERMES_HOME": home, "version": VERSION,
            })
            for job in jobs:
                if job.get("id"):
                    pipe.hset(
                        job_key,
                        str(job["id"]),
                        json.dumps(_snapshot(job, client), separators=(",", ":")),
                    )
                    pipe.hdel(tombstone_key, str(job["id"]))
            missing = previous - current
            if missing:
                pipe.hdel(job_key, *missing)
                pipe.hset(
                    tombstone_key,
                    mapping={job_id: now for job_id in missing},
                )
            pipe.execute()
            return True
    except Exception as exc:
        logger.debug("Fleet cron snapshot publish failed: %s", exc)
        return False


def reconcile_desired_state():
    """Apply newer dashboard controls, leaving missing/invalid jobs pending."""
    agent, client = _agent(), _redis()
    if not agent or client is None:
        return False
    try:
        desired = client.hgetall(f"{PREFIX}:desired:{agent}") or {}
        if not desired:
            return True
        from cron import jobs as local
        changed = False
        with local._jobs_lock():
            records = local.load_jobs()
            by_id = {str(j.get("id")): j for j in records}
            for job_id, raw in desired.items():
                try:
                    request = json.loads(raw)
                    revision = int(request["revision"])
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    logger.warning("Ignoring malformed fleet cron desired state for %s", job_id)
                    continue
                job = by_id.get(str(job_id))
                if not job or revision <= int(job.get("registry_control_revision") or 0):
                    continue
                enabled = bool(request.get("enabled"))
                job["enabled"] = enabled
                job["state"] = "scheduled" if enabled else "paused"
                job["paused_at"] = None if enabled else _now()
                job["paused_reason"] = None if enabled else request.get("reason")
                if enabled:
                    job["next_run_at"] = local.compute_next_run(job["schedule"], job.get("last_run_at"))
                job["registry_control_revision"] = revision
                job["registry_operation_id"] = request.get("operation_id")
                changed = True
            if changed:
                local._save_jobs_unlocked(records)
        if changed:
            publish_snapshot()
        return True
    except Exception as exc:
        logger.debug("Fleet cron desired reconciliation failed: %s", exc)
        return False


def fleet_freeze_active():
    agent, client = _agent(), _redis()
    if not agent or client is None:
        return False
    try:
        value = client.hget(f"{PREFIX}:freeze", "enabled")
        return str(value).lower() in ("1", "true", "yes", "on")
    except Exception as exc:
        logger.debug("Fleet cron freeze check failed: %s", exc)
        return False


def request_fleet_freeze(reason, actor):
    agent, client = _agent(), _redis()
    if not agent or client is None:
        return False
    try:
        key = f"{PREFIX}:freeze"
        while True:
            pipe = client.pipeline()
            try:
                pipe.watch(key)
                revision = int(pipe.hget(key, "revision") or 0) + 1
                pipe.multi()
                pipe.hset(key, mapping={
                    "enabled": "true",
                    "revision": str(revision),
                    "operation_id": hashlib.sha256(
                        f"{time.time()}:{agent}".encode()
                    ).hexdigest()[:16],
                    "actor": str(actor or agent),
                    "reason": str(reason or ""),
                    "requested_at": _now(),
                })
                pipe.execute()
                break
            except Exception as exc:
                if exc.__class__.__name__ == "WatchError":
                    continue
                raise
        client.lpush(f"{PREFIX}:audit", json.dumps({"action": "freeze", "agent": agent, "actor": actor, "at": _now()}))
        client.ltrim(f"{PREFIX}:audit", 0, 1999)
        return True
    except Exception as exc:
        logger.debug("Fleet cron freeze request failed: %s", exc)
        return False


def schedule_sync(jobs=None):
    """Debounced fire-and-forget publishing; safe to call while holding jobs lock."""
    global _sync_pending, _sync_requested
    with _sync_lock:
        if _sync_pending:
            _sync_requested = True
            return False
        _sync_pending = True
    def worker():
        global _sync_pending, _sync_requested
        while True:
            with _sync_lock:
                _sync_requested = False
            publish_snapshot()
            with _sync_lock:
                if not _sync_requested:
                    _sync_pending = False
                    return
    threading.Thread(target=worker, name="fleet-cron-sync", daemon=True).start()
    return True