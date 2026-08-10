#!/usr/bin/env python3
"""Persistent per-agent goal charters + owner-question queue.

A *charter* is an agent's standing, end-state-oriented mission (e.g. "always
respond quickly to client inquiries"), stored centrally in Redis so it
survives redeploys — unlike the session-scoped /goal state in the local
state.db. On boot the Discord plugin's CharterManager re-arms the /goal
engine from the charter, so work resumes without anyone re-typing /goal.

Every business-facing charter carries the shared mandate: reduce the owner's
involvement to roughly 10% of routine tasks for each business.

Charters run in two phases:
  * ``discovery`` — the agent enumerates what it does NOT know about its job
    or business and files questions for the owner via the ``ask_owner`` tool
    (registered here). Pending questions PARK the goal loop instead of
    letting the judge terminate it.
  * ``execution`` — the agent works toward its standing objectives.

Redis layout (fleet-shared, exclusive namespace ``goalcharter:v1``):
    goalcharter:v1:charter:<agent>   JSON charter record
    goalcharter:v1:qseq:<agent>      INCR counter for question ids
    goalcharter:v1:q:<agent>:<id>    JSON question record
    goalcharter:v1:qindex:<agent>    zset of question ids by created_at
    goalcharter:v1:applied:<agent>   charter revision last applied to /goal
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

_NS = "goalcharter:v1"
QUESTION_MAX = 1500
QUESTIONS_OPEN_MAX = 12          # cap pending questions per agent
CHARTER_GOAL_TAG = "[Standing charter]"

DEFAULT_MANDATE = (
    "Primary mandate: reduce Garrett's direct involvement to roughly 10% of "
    "routine tasks for the business(es) you serve. Every workflow you design "
    "or run should move toward that."
)


def _redis():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is not configured — goal charters unavailable.")
    import redis
    return redis.from_url(url, decode_responses=True)


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _k(name: str, agent: str) -> str:
    return f"{_NS}:{name}:{agent}"


# ----------------------------------------------------------------------
# charter records
# ----------------------------------------------------------------------

def load_charter(r, agent: str) -> Optional[Dict[str, Any]]:
    raw = r.get(_k("charter", agent))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("goal_charter: unparseable charter for %s", agent)
        return None
    if not isinstance(data, dict) or not data.get("objectives"):
        return None
    data.setdefault("phase", "discovery")
    data.setdefault("status", "active")
    data.setdefault("mandate", DEFAULT_MANDATE)
    data.setdefault("rev", 0)
    return data


def save_charter(r, agent: str, charter: Dict[str, Any]) -> Dict[str, Any]:
    charter = dict(charter)
    charter["rev"] = int(charter.get("rev", 0)) + 1
    charter["updated_at"] = time.time()
    r.set(_k("charter", agent), json.dumps(charter, ensure_ascii=False))
    return charter


def render_goal_text(agent: str, charter: Dict[str, Any]) -> str:
    """The /goal text a charter arms. Prefixed with CHARTER_GOAL_TAG so the
    CharterManager can tell charter-armed goals from owner-typed /goal text
    (it never clobbers the latter)."""
    objectives = [str(o).strip() for o in (charter.get("objectives") or []) if str(o).strip()]
    phase = charter.get("phase") or "discovery"
    lines = [
        f"{CHARTER_GOAL_TAG} Standing mission for {agent}:",
        "",
    ]
    lines += [f"- {o}" for o in objectives]
    mandate = (charter.get("mandate") or "").strip()
    if mandate:
        lines += ["", mandate]
    lines += [
        "",
        f"Current phase: {phase}.",
        (
            "Phase 'discovery': before doing work, enumerate what you do NOT "
            "know about your job, your business, its clients, accounts, and "
            "processes. File each gap as a question with the ask_owner tool "
            "(one clear question per call). Questions park this goal until "
            "answered — that is expected; never abandon the goal because "
            "you are waiting."
            if phase == "discovery" else
            "Phase 'execution': work toward the objectives above. When you "
            "hit a gap only the owner can fill, file it with the ask_owner "
            "tool and continue other work; never abandon the goal because "
            "you are waiting."
        ),
        "This is a standing mission — it is never 'done'; make continuous, "
        "concrete progress each turn and prefer building repeatable systems "
        "over one-off actions.",
    ]
    return "\n".join(lines)


def is_charter_goal(goal_text: str) -> bool:
    return (goal_text or "").lstrip().startswith(CHARTER_GOAL_TAG)


# ----------------------------------------------------------------------
# owner-question queue
# ----------------------------------------------------------------------

def _qkey(agent: str, qid: int) -> str:
    return f"{_NS}:q:{agent}:{int(qid)}"


def file_question(r, agent: str, question: str) -> Dict[str, Any]:
    question = (question or "").strip()[:QUESTION_MAX]
    if not question:
        raise ValueError("question text is required")
    pending = [q for q in list_questions(r, agent) if q["status"] == "pending"]
    if len(pending) >= QUESTIONS_OPEN_MAX:
        raise RuntimeError(
            f"{len(pending)} questions already pending for the owner — wait "
            "for answers before filing more."
        )
    qid = int(r.incr(_k("qseq", agent)))
    rec = {
        "id": qid,
        "agent": agent,
        "question": question,
        "status": "pending",           # pending | answered
        "answer": "",
        "created_at": time.time(),
        "answered_at": 0.0,
        "posted": False,               # surfaced in Discord home channel yet?
        "consumed": False,             # answer already injected as a turn?
    }
    r.set(_qkey(agent, qid), json.dumps(rec, ensure_ascii=False))
    r.zadd(_k("qindex", agent), {str(qid): rec["created_at"]})
    return rec


def get_question(r, agent: str, qid: int) -> Optional[Dict[str, Any]]:
    raw = r.get(_qkey(agent, qid))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def save_question(r, agent: str, rec: Dict[str, Any]) -> None:
    r.set(_qkey(agent, int(rec["id"])), json.dumps(rec, ensure_ascii=False))


def list_questions(r, agent: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for qid in r.zrange(_k("qindex", agent), 0, -1):
        rec = get_question(r, agent, int(qid))
        if rec is None:
            continue
        if status and rec.get("status") != status:
            continue
        out.append(rec)
    return out


def answer_question(r, agent: str, qid: int, answer: str) -> Dict[str, Any]:
    rec = get_question(r, agent, qid)
    if rec is None:
        raise KeyError(f"question #{qid} not found for {agent}")
    if rec.get("status") == "answered":
        raise RuntimeError(f"question #{qid} is already answered")
    rec["status"] = "answered"
    rec["answer"] = (answer or "").strip()[:QUESTION_MAX]
    rec["answered_at"] = time.time()
    save_question(r, agent, rec)
    return rec


# ----------------------------------------------------------------------
# native tool: ask_owner
# ----------------------------------------------------------------------

def ask_owner_tool(args: Dict[str, Any]) -> str:
    try:
        r = _redis()
        agent = _agent_name()
        rec = file_question(r, agent, str(args.get("question") or ""))
        return json.dumps({
            "ok": True,
            "question_id": rec["id"],
            "note": (
                "Question filed for the owner. Your standing goal will PARK "
                "until it is answered — that is normal. Keep working on "
                "anything not blocked by this question; the answer arrives "
                "as a new turn."
            ),
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("ask_owner tool failed")
        return json.dumps({"error": f"ask_owner failed: {e}"})


registry.register(
    name="ask_owner",
    toolset=TOOLSET,
    schema={
        "name": "ask_owner",
        "description": (
            "File a question for the human owner (Garrett) when you are "
            "missing information about your job, business, accounts, or "
            "processes that only he can provide. One clear, specific "
            "question per call. Questions are surfaced to the owner in your "
            "home channel and PARK your standing goal until answered — the "
            "answer arrives as a new turn. Use this instead of stalling, "
            "guessing, or declaring a goal blocked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question for the owner — specific and answerable.",
                },
            },
            "required": ["question"],
        },
    },
    handler=ask_owner_tool,
    check_fn=lambda: bool(os.getenv("REDIS_URL")),
    description="Owner-question queue for standing goal charters",
)
