"""LLM fact extraction for the Qdrant memory provider.

Distills conversation turns into durable, third-person facts via the
engine's auxiliary LLM client (cheap model). Best-effort: any failure
returns an empty list and the agent keeps working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Auxiliary task name — operators can override the model via
# auxiliary.memory_extraction.{provider,model} in config.yaml.
AUX_TASK = "memory_extraction"

_MAX_INPUT_CHARS = 24_000

_SYSTEM_PROMPT = """You extract durable memories from an AI agent's conversation.

Return a JSON array (possibly empty) of facts worth remembering weeks or
months from now: decisions, client/project details, preferences, commitments,
corrections, outcomes. Skip chit-chat, transient status, restated context the
agent obviously already knows, and anything that is only relevant today.

Each item: {"text": "<one self-contained sentence, third person, with names
and dates>", "importance": 1-5, "scope": "agent"|"shared"}

scope "shared" ONLY for facts other agents on the team clearly need
(client identity/contact details, business-wide decisions, owner-stated
policies). Everything else is "agent".

Return ONLY the JSON array, no prose."""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n[...truncated...]\n" + text[-half:]


def _parse_facts(raw: str) -> List[Dict[str, Any]]:
    """Parse the model output into a validated fact list."""
    raw = raw.strip()
    # Strip markdown fences if present.
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []
    facts: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "")).strip()
        if not text or len(text) > 600:
            continue
        try:
            importance = max(1, min(5, int(it.get("importance", 3))))
        except Exception:
            importance = 3
        scope = it.get("scope", "agent")
        if scope not in ("agent", "shared"):
            scope = "agent"
        facts.append({"text": text, "importance": importance, "scope": scope})
    return facts[:12]


def extract_facts(transcript: str) -> List[Dict[str, Any]]:
    """Run LLM fact extraction over a transcript chunk. Fail-soft."""
    if not transcript or not transcript.strip():
        return []
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client(AUX_TASK)
        if client is None or not model:
            logger.debug("qdrant memory: no auxiliary LLM available for extraction")
            return []
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _clip(transcript, _MAX_INPUT_CHARS)},
            ],
            temperature=0.1,
            max_tokens=1200,
        )
        content = (resp.choices[0].message.content or "") if resp.choices else ""
        return _parse_facts(content)
    except Exception as e:
        logger.debug("qdrant memory: fact extraction failed: %s", e)
        return []


def transcript_from_messages(messages: List[Dict[str, Any]],
                             max_chars: int = _MAX_INPUT_CHARS) -> str:
    """Flatten OpenAI-style messages into a plain transcript (user/assistant only)."""
    lines: List[str] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not content or not str(content).strip():
            continue
        lines.append(f"{role.upper()}: {str(content).strip()}")
    return _clip("\n".join(lines), max_chars)
