"""Economy-tier downshift metadata + escalation bookkeeping for cron jobs.

An owner-approved "downshift" pins a cron job to the vetted economy model
(``routing.economy`` in config.yaml) while remembering the model it ran on
before, so the job can be escalated back:

  * automatically — a failed run of a downshifted job is retried once on the
    previous (strong) model by the scheduler, and repeated escalations auto-pin
    the job back to the strong model (see ``record_run_outcome``);
  * by the owner — "that summary was bad, use the better model" reverts the
    job persistently via the ``model_routing`` tool.

All state lives on the job record itself under the ``economy_routing`` key so
it survives restarts/redeploys with jobs.json and needs no extra store:

    economy_routing:
      active: true            # currently running on the economy model
      previous_model: str     # model/provider to escalate back to ("" = the
      previous_provider: str  #   job was unpinned before the downshift)
      downshifted_at: iso     # when + why + by whom, for auditability
      reason: str
      requested_by: str
      recent: [ {at, escalated, success} ... ]   # last N run outcomes
      escalation_count: int   # total automatic escalations since downshift
      reverted_at: iso        # set when escalated back (auto or owner)
      reverted_reason: str
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from hermes_time import now as _hermes_now

from cron.jobs import _jobs_lock, load_jobs, save_jobs

logger = logging.getLogger(__name__)

ECONOMY_KEY = "economy_routing"

# Keep the last N run outcomes on the job record.
RECENT_WINDOW = 10

# Auto-pin threshold: this many automatically-escalated runs within the last
# AUTO_PIN_WINDOW runs pins the job back to the strong model.
AUTO_PIN_ESCALATIONS = 2
AUTO_PIN_WINDOW = 5


def _now_iso() -> str:
    return _hermes_now().isoformat()


def economy_state(job: Dict[str, Any]) -> Dict[str, Any]:
    state = job.get(ECONOMY_KEY)
    return state if isinstance(state, dict) else {}


def is_downshifted(job: Dict[str, Any]) -> bool:
    """True when this job is actively running on the economy tier."""
    return bool(economy_state(job).get("active"))


def escalation_target(job: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(provider, model) to escalate a downshifted job's run back to.

    Empty strings mean "the job was unpinned before the downshift" — the
    caller should clear the per-job pin so normal config resolution applies.
    """
    state = economy_state(job)
    return (
        (state.get("previous_provider") or "").strip() or None,
        (state.get("previous_model") or "").strip() or None,
    )


def apply_downshift(
    job_id: str,
    *,
    model: str,
    provider: Optional[str],
    reason: str = "",
    requested_by: str = "owner",
) -> Optional[Dict[str, Any]]:
    """Pin ``job_id`` to the economy model, remembering the previous pin.

    Returns ``{"job_id", "name", "old_model", "new_model"}`` on success,
    ``None`` if the job does not exist. Re-downshifting an already-active
    job keeps the ORIGINAL previous model (so escalation always returns to
    the true strong model, not to the economy model itself).
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("downshift requires a target model")
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            state = economy_state(job)
            if not state.get("active"):
                state = {
                    "previous_model": (job.get("model") or "").strip(),
                    "previous_provider": (job.get("provider") or "").strip(),
                }
            state.update({
                "active": True,
                "downshifted_at": _now_iso(),
                "reason": (reason or "").strip(),
                "requested_by": (requested_by or "owner").strip(),
                "recent": state.get("recent") or [],
                "escalation_count": int(state.get("escalation_count") or 0),
            })
            state.pop("reverted_at", None)
            state.pop("reverted_reason", None)
            old_model = job.get("model") or "(default/routing)"
            job["model"] = model
            # Provider is a first-class pin: when the economy rule names a
            # provider, pin it; otherwise CLEAR any existing pin so the
            # economy model resolves through the default provider instead of
            # being sent to whatever provider the job was pinned to before
            # (which may not even serve the economy model).
            job["provider"] = provider or None
            job[ECONOMY_KEY] = state
            save_jobs(jobs)
            return {
                "job_id": job_id,
                "name": job.get("name") or job_id,
                "old_model": old_model,
                "new_model": model,
            }
    return None


def revert_downshift(
    job_id: str,
    *,
    reason: str = "",
    source: str = "owner",
) -> Optional[Dict[str, Any]]:
    """Escalate ``job_id`` back to its pre-downshift model, persistently.

    ``source`` is ``"owner"`` (owner feedback) or ``"auto"`` (repeated run
    escalations). Returns ``{"job_id", "name", "old_model", "new_model"}``
    or ``None`` if the job is missing or not downshifted.
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            state = economy_state(job)
            if not state.get("active"):
                return None
            old_model = job.get("model") or "(default/routing)"
            prev_model = (state.get("previous_model") or "").strip()
            prev_provider = (state.get("previous_provider") or "").strip()
            # Restore both pins exactly as they were before the downshift —
            # an empty previous value means "was unpinned", so clear it.
            job["model"] = prev_model or None
            job["provider"] = prev_provider or None
            state["active"] = False
            state["reverted_at"] = _now_iso()
            state["reverted_reason"] = (reason or "").strip()
            state["reverted_by"] = (source or "owner").strip()
            job[ECONOMY_KEY] = state
            save_jobs(jobs)
            return {
                "job_id": job_id,
                "name": job.get("name") or job_id,
                "old_model": old_model,
                "new_model": prev_model or "(default/routing)",
            }
    return None


def record_run_outcome(job_id: str, *, escalated: bool, success: bool) -> bool:
    """Record one run of a downshifted job; True when auto-pin should fire.

    Appends to the job's rolling outcome window and returns True when
    ``AUTO_PIN_ESCALATIONS`` of the last ``AUTO_PIN_WINDOW`` runs required an
    automatic escalation — the caller should then ``revert_downshift(...,
    source="auto")`` and notify the owner. Idempotent against a missing or
    no-longer-downshifted job (returns False).
    """
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job.get("id") != job_id:
                continue
            state = economy_state(job)
            if not state.get("active"):
                return False
            recent = state.get("recent")
            if not isinstance(recent, list):
                recent = []
            recent.append({
                "at": _now_iso(),
                "escalated": bool(escalated),
                "success": bool(success),
            })
            state["recent"] = recent[-RECENT_WINDOW:]
            if escalated:
                state["escalation_count"] = int(state.get("escalation_count") or 0) + 1
            job[ECONOMY_KEY] = state
            save_jobs(jobs)
            window = state["recent"][-AUTO_PIN_WINDOW:]
            escalations = sum(1 for r in window if r.get("escalated"))
            return escalations >= AUTO_PIN_ESCALATIONS
    return False
