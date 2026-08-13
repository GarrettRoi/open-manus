"""Shutdown/restart status-channel notices with fleet-wide rate limiting.

When a platform has a dedicated ``status_channel`` configured, the gateway's
shutdown broadcast is routed there instead of the home channel. Because a
fleet redeploy restarts every agent within minutes, the notice is rate-limited
and coalesced through the shared Redis (``REDIS_URL``):

* per-agent cooldown — at most one status message per agent per cooldown
  window (``SET NX EX`` on a per-agent key);
* coalescing — close-together shutdowns push their agent name onto a shared
  pending list; the first agent to grab the group lock waits briefly, drains
  the list, and posts a single grouped message ("3 agents restarting:
  Harmony, Lexi, Nova"). Agents that lose the lock skip posting (their name
  rides along in the group message).

If Redis is unavailable, we fail open: the notice is still posted (unlimited),
because a missing rate limiter must never silence a legitimate shutdown notice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Env knobs (read at call time so tests/ops can tune without a restart).
COOLDOWN_ENV = "HERMES_SHUTDOWN_STATUS_COOLDOWN"
COALESCE_WAIT_ENV = "HERMES_SHUTDOWN_STATUS_COALESCE_WAIT"

DEFAULT_COOLDOWN = 300.0        # seconds; one notice per agent per window
DEFAULT_COALESCE_WAIT = 3.0     # seconds the lock winner waits to group names

_KEY_PREFIX = "hermes:gateway:shutdown_notice"


def _agent_name() -> str:
    return os.getenv("AGENT_NAME", "").strip().lower() or "unknown"


def _get_redis():
    """Return a shared-Redis client, or None when REDIS_URL is not configured."""
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    import redis

    return redis.from_url(url, decode_responses=True, socket_timeout=5)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    return max(value, 0.0)


def _solo_message(action: str, agent: Optional[str] = None) -> str:
    name = (agent or _agent_name()).capitalize()
    return f"⚠️ {name}: gateway {action}"


def _grouped_message(action: str, names: list[str]) -> str:
    display = [n.capitalize() for n in names]
    if len(display) <= 1:
        return _solo_message(action, display[0] if display else None)
    return f"⚠️ {len(display)} agents {action}: {', '.join(display)}"


async def prepare_shutdown_status_notice(
    action: str,
    *,
    cooldown: Optional[float] = None,
    coalesce_wait: Optional[float] = None,
) -> Optional[str]:
    """Return the status-channel notice text, or None when suppressed.

    ``action`` is the human-readable verb phrase ("shutting down" /
    "restarting"). Suppression (None) means either this agent already posted
    within the cooldown window, or another agent holds the group lock and
    will include this agent's name in its grouped message.
    """
    if cooldown is None:
        cooldown = _float_env(COOLDOWN_ENV, DEFAULT_COOLDOWN)
    if coalesce_wait is None:
        coalesce_wait = _float_env(COALESCE_WAIT_ENV, DEFAULT_COALESCE_WAIT)

    agent = _agent_name()

    try:
        client = _get_redis()
    except Exception as e:  # import error, bad URL, …
        logger.debug("Status-notice Redis unavailable, failing open: %s", e)
        client = None
    if client is None:
        return _solo_message(action, agent)

    try:
        # 1. Per-agent cooldown: only the first shutdown in the window posts.
        cooldown_key = f"{_KEY_PREFIX}:cooldown:{agent}"
        if cooldown > 0 and not client.set(
            cooldown_key, str(time.time()), nx=True, ex=max(int(cooldown), 1)
        ):
            logger.info(
                "Shutdown status notice suppressed for %s: within cooldown window",
                agent,
            )
            return None

        # 2. Join the current wave so a grouped message can name us.
        pending_key = f"{_KEY_PREFIX}:pending"
        client.rpush(pending_key, agent)
        client.expire(pending_key, max(int(cooldown), 60))

        # 3. Group lock: the winner composes the (possibly grouped) message;
        #    losers stay silent — their name rides along in the winner's post.
        lock_ttl = max(int(coalesce_wait * 2) + 1, 10)
        lock_key = f"{_KEY_PREFIX}:lock"
        if not client.set(lock_key, agent, nx=True, ex=lock_ttl):
            logger.info(
                "Shutdown status notice for %s coalesced into another agent's "
                "grouped message",
                agent,
            )
            return None

        if coalesce_wait > 0:
            await asyncio.sleep(coalesce_wait)

        names_raw = client.lrange(pending_key, 0, -1) or []
        client.delete(pending_key)

        # Dedupe preserving arrival order; make sure we name ourselves even if
        # the list expired or was drained by an overlapping wave.
        seen: set[str] = set()
        names: list[str] = []
        for n in names_raw:
            n = str(n).strip().lower()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        if agent not in seen:
            names.append(agent)

        return _grouped_message(action, names)
    except Exception as e:
        # Fail toward the louder, more-visible behaviour: post an individual
        # notice rather than letting a Redis blip silence the broadcast.
        logger.debug("Status-notice rate limiting failed, failing open: %s", e)
        return _solo_message(action, agent)
