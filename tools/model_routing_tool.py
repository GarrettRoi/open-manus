"""model_routing tool — agent-driven cheap-LLM downshift with escalation.

Lets the owner manage model economics conversationally:

  * ``audit``     — inventory the agent's recurring work (cron jobs, kanban
                    background tasks) with each item's current model and recent
                    run history, so the agent can answer "which of your
                    recurring tasks could a cheaper LLM handle?" concretely.
  * ``downshift`` — after owner approval, pin selected items to the vetted
                    economy model (``routing.economy`` in config.yaml),
                    remembering the previous model for escalation/revert.
  * ``escalate``  — move items back to their previous (strong) model, e.g.
                    when the owner says a downshifted job's output was bad.
                    Optionally re-runs a cron job immediately.
  * ``status``    — report downshifted items, escalation counts, and recent
                    outcomes.

Automatic escalation (failed economy runs retried on the strong model, plus
auto-pin after repeated escalations) lives in cron/scheduler.py + cron/economy.py.
Kanban tasks have no in-process run loop here, so they get downshift/escalate
persistence only (the worker CLI applies ``model_override`` at spawn).
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix
    fcntl = None

logger = logging.getLogger(__name__)

# Serializes sidecar read-modify-write cycles: in-process threading lock plus
# a cross-process advisory flock so concurrent tool calls (gateway turn +
# cron-spawned agent) can't drop each other's recovery metadata.
_sidecar_thread_lock = threading.Lock()


@contextlib.contextmanager
def _sidecar_lock():
    with _sidecar_thread_lock:
        lock_fd = None
        try:
            try:
                path = _sidecar_path().with_suffix(".lock")
                path.parent.mkdir(parents=True, exist_ok=True)
                lock_fd = open(path, "a+", encoding="utf-8")
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as e:
                logger.warning("economy sidecar cross-process lock unavailable: %s", e)
            yield
        finally:
            if lock_fd is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    lock_fd.close()

# Sidecar storing the pre-downshift model_override for kanban tasks (the tasks
# table has no economy metadata column; this keeps the change non-invasive).
_KANBAN_SIDECAR = "economy_kanban.json"


def _sidecar_path() -> Path:
    return get_hermes_home() / "cron" / _KANBAN_SIDECAR


def _load_sidecar() -> Dict[str, Any]:
    try:
        with open(_sidecar_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("economy kanban sidecar unreadable: %s", e)
        return {}


def _save_sidecar(data: Dict[str, Any]) -> None:
    path = _sidecar_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    from utils import atomic_replace
    atomic_replace(tmp, path)


def _load_cfg() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception as e:
        logger.warning("model_routing: config load failed: %s", e)
        return {}


def _resolve_economy(cfg: dict) -> Optional[Dict[str, str]]:
    from hermes_cli.model_routing import resolve_economy_model
    return resolve_economy_model(cfg)


def _primary_model(cfg: dict) -> str:
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        return model_cfg
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or model_cfg.get("model") or "")
    return ""


def _cron_inventory() -> List[Dict[str, Any]]:
    from cron.economy import economy_state, is_downshifted
    from cron.jobs import load_jobs

    items: List[Dict[str, Any]] = []
    for job in load_jobs():
        state = economy_state(job)
        recent = state.get("recent") or []
        entry = {
            "kind": "cron_job",
            "id": job.get("id"),
            "name": job.get("name"),
            "prompt_preview": str(job.get("prompt") or "")[:160],
            "schedule": job.get("schedule_display") or "?",
            "enabled": job.get("enabled", True),
            "no_agent": bool(job.get("no_agent")),
            "model": job.get("model") or "(default/routing)",
            "provider": job.get("provider"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
            "downshifted": is_downshifted(job),
        }
        if state:
            entry["economy"] = {
                "active": bool(state.get("active")),
                "previous_model": state.get("previous_model"),
                "downshifted_at": state.get("downshifted_at"),
                "escalation_count": int(state.get("escalation_count") or 0),
                "recent_runs": recent[-5:],
                "reverted_at": state.get("reverted_at"),
                "reverted_reason": state.get("reverted_reason"),
                "reverted_by": state.get("reverted_by"),
            }
        items.append(entry)
    return items


def _kanban_inventory() -> List[Dict[str, Any]]:
    """Best-effort kanban task inventory; empty when kanban isn't in use."""
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            tasks = kb.list_tasks(conn, limit=100)
        finally:
            conn.close()
    except Exception:
        return []
    sidecar = _load_sidecar()
    items = []
    for t in tasks:
        entry = {
            "kind": "kanban_task",
            "id": t.id,
            "title": getattr(t, "title", None),
            "status": getattr(t, "status", None),
            "model_override": t.model_override or "(default)",
            "downshifted": t.id in sidecar,
        }
        if t.id in sidecar:
            entry["economy"] = sidecar[t.id]
        items.append(entry)
    return items


def _downshift_cron(job_ids: List[str], economy: Dict[str, str], reason: str) -> List[Dict[str, Any]]:
    from cron.economy import apply_downshift
    from cron.jobs import get_job

    results = []
    for job_id in job_ids:
        job = get_job(job_id)
        if not job:
            results.append({"job_id": job_id, "error": "not found"})
            continue
        if job.get("no_agent"):
            results.append({"job_id": job_id, "error": "no_agent job — no LLM involved, nothing to downshift"})
            continue
        changed = apply_downshift(
            job_id,
            model=economy["model"],
            provider=economy.get("provider") or None,
            reason=reason,
        )
        results.append(changed or {"job_id": job_id, "error": "not found"})
    return results


def _escalate_cron(job_ids: List[str], reason: str, rerun_now: bool) -> List[Dict[str, Any]]:
    from cron.economy import revert_downshift
    from cron.jobs import get_job

    results = []
    for job_id in job_ids:
        changed = revert_downshift(job_id, reason=reason, source="owner")
        if not changed:
            results.append({"job_id": job_id, "error": "not found or not downshifted"})
            continue
        if rerun_now:
            try:
                from tools.cronjob_tools import _execute_job_now
                job = get_job(job_id)
                if job:
                    run = _execute_job_now(job)
                    changed["rerun"] = {
                        "claimed": run.get("claimed"),
                        "success": run.get("success"),
                        "error": run.get("error"),
                    }
            except Exception as e:
                changed["rerun"] = {"error": str(e)}
        results.append(changed)
    return results


def _set_kanban_override(task_id: str, value: Optional[str]) -> bool:
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        cur = conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?", (value, task_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _downshift_kanban(task_ids: List[str], economy: Dict[str, str], reason: str) -> List[Dict[str, Any]]:
    from hermes_time import now as _now
    from hermes_cli import kanban_db as kb

    results = []
    with _sidecar_lock():
        for task_id in task_ids:
            try:
                conn = kb.connect()
                try:
                    task = kb.get_task(conn, task_id)
                finally:
                    conn.close()
                if not task:
                    results.append({"task_id": task_id, "error": "not found"})
                    continue
                # Recovery metadata is persisted BEFORE the DB override so a
                # crash in between leaves a recoverable (revertable) record,
                # never an unattributed economy override.
                sidecar = _load_sidecar()
                previous = (
                    sidecar[task_id].get("previous_model_override")
                    if task_id in sidecar
                    else task.model_override
                )
                sidecar[task_id] = {
                    "previous_model_override": previous,
                    "downshifted_at": _now().isoformat(),
                    "reason": reason,
                }
                _save_sidecar(sidecar)
                if not _set_kanban_override(task_id, economy["model"]):
                    sidecar = _load_sidecar()
                    sidecar.pop(task_id, None)
                    _save_sidecar(sidecar)
                    results.append({"task_id": task_id, "error": "update failed"})
                    continue
                results.append({
                    "task_id": task_id,
                    "old_model": previous or "(default)",
                    "new_model": economy["model"],
                })
            except Exception as e:
                results.append({"task_id": task_id, "error": str(e)})
    return results


def _escalate_kanban(task_ids: List[str], reason: str) -> List[Dict[str, Any]]:
    results = []
    with _sidecar_lock():
        for task_id in task_ids:
            sidecar = _load_sidecar()
            entry = sidecar.get(task_id)
            if not entry:
                results.append({"task_id": task_id, "error": "not downshifted"})
                continue
            previous = entry.get("previous_model_override")
            try:
                if not _set_kanban_override(task_id, previous):
                    results.append({"task_id": task_id, "error": "not found"})
                    continue
            except Exception as e:
                results.append({"task_id": task_id, "error": str(e)})
                continue
            sidecar.pop(task_id, None)
            _save_sidecar(sidecar)
            results.append({
                "task_id": task_id,
                "restored_model": previous or "(default)",
                "reason": reason,
            })
    return results


def model_routing(
    action: str,
    job_ids: Optional[List[str]] = None,
    task_ids: Optional[List[str]] = None,
    reason: Optional[str] = None,
    rerun_now: bool = False,
    task_id: str = None,
) -> str:
    del task_id  # handler-signature compatibility
    from tools.registry import tool_error

    normalized = (action or "").strip().lower()
    reason = (reason or "").strip()
    job_ids = [str(j).strip() for j in (job_ids or []) if str(j).strip()]
    task_ids = [str(t).strip() for t in (task_ids or []) if str(t).strip()]

    try:
        cfg = _load_cfg()
        economy = _resolve_economy(cfg)

        if normalized == "audit":
            return json.dumps({
                "success": True,
                "economy_model": economy or None,
                "primary_model": _primary_model(cfg),
                "cron_jobs": _cron_inventory(),
                "kanban_tasks": _kanban_inventory(),
                "note": (
                    "Classify each item yourself (fully / partially / not suitable "
                    "for the economy model) based on its prompt and history, then "
                    "propose candidates to the owner before calling downshift."
                ),
            }, indent=2, default=str)

        if normalized == "downshift":
            if not job_ids and not task_ids:
                return tool_error("downshift requires job_ids and/or task_ids", success=False)
            if not economy or not economy.get("model"):
                return tool_error(
                    "No economy model configured. Set routing.economy (or "
                    "routing.cron_job / routing.background_task) in config.yaml "
                    "first — downshift refuses to pick an unvetted model.",
                    success=False,
                )
            result = {
                "success": True,
                "economy_model": economy,
                "cron": _downshift_cron(job_ids, economy, reason) if job_ids else [],
                "kanban": _downshift_kanban(task_ids, economy, reason) if task_ids else [],
            }
            return json.dumps(result, indent=2, default=str)

        if normalized == "escalate":
            if not job_ids and not task_ids:
                return tool_error("escalate requires job_ids and/or task_ids", success=False)
            result = {
                "success": True,
                "cron": _escalate_cron(job_ids, reason, rerun_now) if job_ids else [],
                "kanban": _escalate_kanban(task_ids, reason) if task_ids else [],
            }
            return json.dumps(result, indent=2, default=str)

        if normalized == "status":
            cron = [i for i in _cron_inventory() if i.get("downshifted") or i.get("economy")]
            kanban = [i for i in _kanban_inventory() if i.get("downshifted")]
            return json.dumps({
                "success": True,
                "economy_model": economy or None,
                "downshifted_cron_jobs": cron,
                "downshifted_kanban_tasks": kanban,
            }, indent=2, default=str)

        return tool_error(
            f"Unknown action '{action}'. Use audit, downshift, escalate, or status.",
            success=False,
        )
    except Exception as e:
        logger.error("model_routing tool failed: %s", e, exc_info=True)
        return tool_error(f"model_routing failed: {e}", success=False)


MODEL_ROUTING_SCHEMA = {
    "name": "model_routing",
    "description": (
        "Manage cheap-LLM (economy-tier) routing for your OWN recurring work. "
        "When the owner asks which recurring tasks a cheaper model could handle, "
        "call action='audit', classify each cron job / background task yourself "
        "(fully / partially / not suitable — judge by how much reasoning the "
        "prompt needs and its recent run history), and present recommendations. "
        "Only after the owner approves, call action='downshift' with the chosen "
        "ids — it pins them to the vetted economy model from config and records "
        "the previous model. Downshifted cron jobs get an automatic safety net: "
        "a failed run is retried once on the stronger model, and repeated "
        "escalations pin the job back automatically. If the owner is unhappy "
        "with a downshifted job's recent output ('that summary was bad — use "
        "the better model', including voice transcripts), call action='escalate' "
        "for that job (set rerun_now=true to re-run it immediately on the strong "
        "model). action='status' reports downshifted items with escalation "
        "counts and recent outcomes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["audit", "downshift", "escalate", "status"],
                "description": "audit: inventory recurring work with models + history. downshift: pin approved items to the economy model. escalate: restore items to their previous strong model. status: report downshifted items.",
            },
            "job_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Cron job IDs (from audit/status) for downshift/escalate.",
            },
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Kanban task IDs (from audit) for downshift/escalate.",
            },
            "reason": {
                "type": "string",
                "description": "Why — recorded for auditability (e.g. 'owner approved 2026-08-16' or 'owner unhappy with output quality').",
            },
            "rerun_now": {
                "type": "boolean",
                "default": False,
                "description": "With action='escalate' on cron jobs: also re-run the job immediately on the restored strong model.",
            },
        },
        "required": ["action"],
    },
}


def check_model_routing_requirements() -> bool:
    from tools.cronjob_tools import check_cronjob_requirements
    return check_cronjob_requirements()


# --- Registry ---
from tools.registry import registry

registry.register(
    name="model_routing",
    toolset="cronjob",
    schema=MODEL_ROUTING_SCHEMA,
    handler=lambda args, **kw: model_routing(
        action=args.get("action", ""),
        job_ids=args.get("job_ids"),
        task_ids=args.get("task_ids"),
        reason=args.get("reason"),
        rerun_now=bool(args.get("rerun_now")),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_model_routing_requirements,
    emoji="💸",
)
