"""Shared, fail-closed routing contract. No provider calls or implicit ownership."""
import hashlib
import json
import os
import time

try:
    from .dev_projects import (K_PROJECTS, K_TARGET, validate_work_scope,
                               resolve_project, _validate_repl_id, _read_snapshot,
                               effective_projects)
except ImportError:
    from dev_projects import (K_PROJECTS, K_TARGET, validate_work_scope,
                              resolve_project, _validate_repl_id, _read_snapshot,
                              effective_projects)


def owner_id():
    """Same authoritative owner used by the fleet vault review surface."""
    return os.environ.get("DISCORD_OWNER_ID", "700339484507766826").strip()


def require_owner(actor):
    if not owner_id() or actor != owner_id():
        raise ValueError("Only the configured owner may confirm or correct routing")


def content_digest(item):
    fields = ("id", "title", "description", "agent", "project", "work_scope",
              "submitted_repl_id", "routing_revision")
    return hashlib.sha256(json.dumps(
        {k: item.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def review_token(r, item):
    return hashlib.sha256(json.dumps(
        [item, r.mget([K_PROJECTS, K_TARGET])], sort_keys=True
    ).encode()).hexdigest()


def approved_route(item):
    """Accept consistent scoped approvals; never resolve an old pin anew."""
    try:
        if not isinstance(item, dict):
            raise ValueError("request record must be an object")
        scope, project = validate_work_scope(item.get("work_scope"), item.get("project"))
        repl_id = _validate_repl_id(item.get("submitted_repl_id"), project)
        if item.get("status") != "approved":
            raise ValueError("request is not approved")
        if (item.get("dispatch_project") != project
                or item.get("dispatch_repl_id") != repl_id):
            raise ValueError("approval pin conflicts with scope/submission snapshot")
        if not item.get("decided_by") or not item.get("decided_at"):
            raise ValueError("owner approval evidence is missing")
        require_owner(item["decided_by"])
        if item.get("owner_confirmation") != {"version": 1, "owner_id": item["decided_by"]}:
            raise ValueError("owner-only confirmation evidence is missing")
        if not item.get("approval_digest") or item["approval_digest"] != content_digest(item):
            raise ValueError("request content changed after approval")
        return project, repl_id
    except ValueError as exc:
        raise ValueError(f"needs-routing-review: {exc}; open /devrequests and correct "
                         "the route, then confirm approval again") from exc


def dispatch_route(r, item):
    route = approved_route(item)
    # Deleted names disable dispatch. Changed IDs never replace an approved pin.
    resolve_project(r, route[0])
    mappings = effective_projects(*_read_snapshot(r))
    if any(name != route[0] and target == route[1] for name, target in mappings.items()):
        raise ValueError("Approved identity now belongs to another project; routing review required")
    return route


def enqueue(r, req_id, force=False):
    """Validate and enqueue from a single watched snapshot (all entry points)."""
    from redis.exceptions import WatchError
    key, lease, claim = (f"devreq:item:{req_id}", f"replitmcp:lease:{req_id}",
                         f"replitmcp:claim:{req_id}")
    with r.pipeline() as p:
        try:
            p.watch(key, lease, claim, K_PROJECTS, K_TARGET)
            item = json.loads(p.get(key) or "{}")
            dispatch_route(p, item)
            if item.get("dispatch_status") == "started" or item.get("provider_attempt_at"):
                raise ValueError("Provider call already issued or outcome uncertain; "
                                 "retry cannot undo it. Inspect the existing run.")
            if (item.get("dispatch_status") == "failed" and not item.get("routing_blocked")
                    and item.get("provider_disposition") != "not_issued"):
                raise ValueError("Legacy failure has uncertain provider outcome; inspect the existing run")
            if p.exists(lease):
                return 0
            if p.exists(claim) and not force:
                return 3
            p.multi()
            p.set(claim, "1", ex=300)
            p.lpush("devreq:dispatch", req_id)
            p.execute()
            return 1
        except WatchError:
            raise ValueError("Request or routing changed; review and retry") from None


def approval_route(r, item):
    scope, project = validate_work_scope(item.get("work_scope"), item.get("project"))
    repl_id = _validate_repl_id(item.get("submitted_repl_id"), project)
    if resolve_project(r, project) != (project, repl_id):
        raise ValueError("destination mapping changed since submission; correct route "
                         "in /devrequests and review again")
    if item.get("dispatch_repl_id") or item.get("dispatch_project"):
        raise ValueError("pending request has an existing pin; correct route and review again")
    return project, repl_id


def preview(r, item):
    result = {"token": review_token(r, item), "reason": "", "eligible": False}
    try:
        route = (dispatch_route(r, item) if item.get("status") == "approved"
                 else approval_route(r, item))
        result.update(project=route[0], repl_id=route[1], eligible=True,
                      reason="Explicit ownership and submission snapshot agree.")
    except ValueError as exc:
        result["reason"] = str(exc)
    return result


def correct_route(r, req_id, scope, project, expected, actor):
    """Owner surface only: unapprove atomically, retaining original and audit."""
    from redis.exceptions import WatchError
    require_owner(actor)
    key = f"devreq:item:{req_id}"
    with r.pipeline() as p:
        try:
            p.watch(key, K_PROJECTS, K_TARGET, f"replitmcp:lease:{req_id}")
            raw = p.get(key)
            item = json.loads(raw) if raw else None
            if not item or review_token(p, item) != expected:
                raise ValueError("Review changed; reopen /devrequests")
            if (item.get("status") not in ("pending", "approved")
                    or item.get("dispatch_status") == "started"
                    or item.get("provider_attempt_at")
                    or p.exists(f"replitmcp:lease:{req_id}")):
                raise ValueError("Cannot reroute started, attempted, closed, or in-flight work")
            # Old failed dispatches may have made a provider call without a receipt.
            if (item.get("dispatch_status") == "failed" and not item.get("routing_blocked")
                    and item.get("provider_disposition") != "not_issued"):
                raise ValueError("Legacy failure has uncertain provider outcome; inspect manually")
            scope, project = validate_work_scope(scope, project)
            _, target = resolve_project(p, project)
            item.setdefault("original_submission", {k: item.get(k) for k in
                ("work_scope", "project", "submitted_repl_id")})
            event = {k: item.get(k) for k in (
                "project", "work_scope", "submitted_repl_id", "dispatch_project",
                "dispatch_repl_id", "decided_by", "decided_at", "approval_digest",
                "dispatch_status", "dispatch_error", "owner_confirmation")}
            event.update(actor=actor, at=int(time.time()), action="routing_correction")
            item.setdefault("routing_history", []).append(event)
            item.update(work_scope=scope, project=project, submitted_repl_id=target,
                        routing_revision=item.get("routing_revision", 0) + 1, status="pending")
            for k in ("dispatch_project", "dispatch_repl_id", "approval_digest",
                      "decided_by", "decided_at", "dispatch_status", "dispatch_error",
                      "routing_blocked"):
                item.pop(k, None)
            item.pop("owner_confirmation", None)
            ttl = p.ttl(key)
            p.multi()
            p.set(key, json.dumps(item), ex=ttl if ttl > 0 else None)
            p.lrem("devreq:approved", 0, req_id)
            p.lrem("devreq:dispatch", 0, req_id)
            p.delete(f"replitmcp:claim:{req_id}")
            p.lrem("devreq:pending", 0, req_id)
            p.rpush("devreq:pending", req_id)
            p.execute()
            return item
        except WatchError:
            raise ValueError("Request or destinations changed; reopen /devrequests") from None