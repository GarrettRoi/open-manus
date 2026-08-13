"""Agent-managed pinned tracking threads in the agent's Discord home channel.

Gives every agent a native ``tracking_thread`` tool to maintain rolling,
structured logs in its own home channel:

  * ``create_tracker``  — create (or reuse) a named tracker thread. The
    anchor message is posted in the home channel, pinned there, and the
    thread is started from it, so the owner sees the tracker pinned in the
    agent's home channel.
  * ``list_trackers``   — list this agent's trackers.
  * ``archive_tracker`` — unpin the anchor, archive the thread, drop records.
  * ``add_entry``       — post an entry message keyed by a stable entry key.
  * ``edit_entry``      — rewrite the message for an existing key in place.
  * ``delete_entry``    — delete the message and forget the key.
  * ``list_entries``    — list the keys recorded for a tracker.

Persistence is Redis (same store the taskboard/dispatch pins use), so
thread and message ids survive gateway restarts and are reused instead of
duplicated. Reconciliation is built into every action: when a stored
thread or message no longer exists on Discord, the stale record is
dropped (and, for ``create_tracker``, the thread is recreated) instead of
erroring.

Discord access is via the REST API with DISCORD_BOT_TOKEN (no dependency
on the gateway adapter's client), so the tool works identically in
interactive turns and cron-driven turns.

Redis layout (fleet-shared instance, per-agent namespaced):

    discord:tracker:v1:trackers:<agent>          hash  name -> JSON record
    discord:tracker:v1:entries:<agent>:<name>    hash  entry_key -> message id
    discord:tracker:v1:threads                   set   all tracker thread ids
                                                       (adapter territory gate)

Loop safety: tracker threads are bot territory. The Discord adapter calls
:func:`in_tracker_territory` and drops ALL conversation there (same rule
as the taskboard), so posting entries can never trigger agent turns.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)

TOOLSET = "vault"  # ships with the fleet-wide toolset every agent has

_NS = "discord:tracker:v1"
_THREADS_SET = f"{_NS}:threads"
# Version counter bumped on every tracker create/archive. The adapter's
# territory gate re-reads the thread-id set whenever this changes, so
# membership is immediately consistent across all adapter processes (no
# TTL window where a freshly created tracker thread is unguarded).
_THREADS_VER = f"{_NS}:threads_ver"

NAME_MAX = 60
KEY_MAX = 80
CONTENT_MAX = 1900          # Discord hard cap is 2000; leave headroom
MAX_TRACKERS = 25           # sanity cap per agent
MAX_ENTRIES = 500           # sanity cap per tracker


def _agent_name() -> str:
    return (os.getenv("AGENT_NAME", "").strip() or "unknown").lower()


def _home_channel_id() -> str:
    return (os.getenv("DISCORD_HOME_CHANNEL") or "").strip()


def _redis():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is not configured — tracking threads "
                           "are unavailable on this agent.")
    import redis
    return redis.from_url(url, decode_responses=True, socket_timeout=10)


def _trackers_key(agent: str) -> str:
    return f"{_NS}:trackers:{agent}"


def _entries_key(agent: str, name: str) -> str:
    return f"{_NS}:entries:{agent}:{name}"


def _norm_name(name: Any) -> str:
    text = re.sub(r"\s+", " ", str(name or "").strip())
    return text[:NAME_MAX]


def _norm_key(key: Any) -> str:
    return str(key or "").strip()[:KEY_MAX]


# ---------------------------------------------------------------------------
# Discord REST helpers (reuse the request core from discord_tool)
# ---------------------------------------------------------------------------

def _token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")
    return token


def _api(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    """One Discord REST call with a single 429 retry."""
    from tools.discord_tool import DiscordAPIError, _discord_request
    try:
        return _discord_request(method, path, _token(), body=body)
    except DiscordAPIError as e:
        if e.status == 429:
            retry_after = 2.0
            try:
                retry_after = min(float(json.loads(e.body).get("retry_after", 2.0)), 10.0)
            except Exception:
                pass
            time.sleep(retry_after)
            return _discord_request(method, path, _token(), body=body)
        raise


def _thread_alive(thread_id: str) -> Optional[Dict[str, Any]]:
    """Return the thread channel object, or None when it no longer exists."""
    from tools.discord_tool import DiscordAPIError
    try:
        return _api("GET", f"/channels/{thread_id}")
    except DiscordAPIError as e:
        # Only a confirmed 404 means the thread is gone. 403 (permission
        # problem, possibly transient/misconfigured) and other errors must
        # propagate so reconciliation never destroys valid persisted state.
        if e.status == 404:
            return None
        raise


def _ensure_unarchived(thread: Dict[str, Any]) -> None:
    """Best-effort keep-alive: unarchive + re-arm max auto-archive window."""
    meta = thread.get("thread_metadata") or {}
    if not meta.get("archived"):
        return
    try:
        _api("PATCH", f"/channels/{thread['id']}",
             body={"archived": False, "locked": False,
                   "auto_archive_duration": 10080})
    except Exception:
        logger.debug("tracker: could not unarchive thread %s",
                     thread.get("id"), exc_info=True)


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------

def _load_tracker(r, agent: str, name: str) -> Optional[Dict[str, Any]]:
    raw = r.hget(_trackers_key(agent), name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _save_tracker(r, agent: str, name: str, rec: Dict[str, Any]) -> None:
    r.hset(_trackers_key(agent), name, json.dumps(rec, ensure_ascii=False))
    if rec.get("thread_id"):
        r.sadd(_THREADS_SET, str(rec["thread_id"]))
        r.incr(_THREADS_VER)


def _drop_tracker(r, agent: str, name: str,
                  rec: Optional[Dict[str, Any]]) -> None:
    r.hdel(_trackers_key(agent), name)
    r.delete(_entries_key(agent, name))
    if rec and rec.get("thread_id"):
        r.srem(_THREADS_SET, str(rec["thread_id"]))
        r.incr(_THREADS_VER)


def _resolve_thread(r, agent: str, name: str) -> tuple:
    """Return (record, live_thread) for a tracker, reconciling stale state.

    When the stored thread no longer exists the record is dropped and
    ``(None, None)`` is returned.
    """
    rec = _load_tracker(r, agent, name)
    if not rec:
        return None, None
    thread = _thread_alive(str(rec.get("thread_id") or ""))
    if thread is None:
        logger.info("tracker %r: stored thread %s gone; dropping record",
                    name, rec.get("thread_id"))
        _drop_tracker(r, agent, name, rec)
        return None, None
    return rec, thread


# ---------------------------------------------------------------------------
# Adapter territory gate
# ---------------------------------------------------------------------------

_territory_cache: Dict[str, Any] = {"ver": None, "ids": frozenset()}


def in_tracker_territory(channel_ids) -> bool:
    """True when a message's channel-id set touches any tracker thread.

    Used by the Discord adapter to drop ALL conversation inside tracker
    threads (bot territory, like the taskboard). Consistency: the cached
    thread-id set is keyed on a Redis version counter that every tracker
    create/archive bumps, so a freshly created tracker thread is gated
    immediately in EVERY adapter process — one cheap GET per message, a
    full SMEMBERS only when the set actually changed. Fail-open on Redis
    errors: the last-known set is used so the home channel keeps working.
    """
    if not channel_ids:
        return False
    ids = _territory_cache["ids"]
    try:
        r = _redis()
        ver = r.get(_THREADS_VER)
        if ver != _territory_cache["ver"]:
            ids = frozenset(str(x) for x in r.smembers(_THREADS_SET))
            _territory_cache.update(ver=ver, ids=ids)
    except Exception:
        pass  # fail-open with last-known ids
    return any(str(c) in ids for c in channel_ids)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _action_create(r, agent: str, name: str, description: str) -> str:
    rec, thread = _resolve_thread(r, agent, name)
    if rec and thread:
        _ensure_unarchived(thread)
        return json.dumps({"ok": True, "reused": True,
                           "tracker": name, "thread_id": rec["thread_id"],
                           "note": "Tracker already exists; reusing it."})
    if r.hlen(_trackers_key(agent)) >= MAX_TRACKERS:
        return _err(f"Tracker limit reached ({MAX_TRACKERS}). Archive an old "
                    "tracker first.")
    home = _home_channel_id()
    header = f"📌 **Tracker — {name}**"
    if description:
        header += f"\n{description[:500]}"
    header += f"\n-# maintained by {agent} via the tracking_thread tool"
    anchor = _api("POST", f"/channels/{home}/messages",
                  body={"content": header[:CONTENT_MAX]})
    thread = _api("POST",
                  f"/channels/{home}/messages/{anchor['id']}/threads",
                  body={"name": f"📌 {name}"[:100],
                        "auto_archive_duration": 10080})
    try:
        _api("PUT", f"/channels/{home}/pins/{anchor['id']}")
        pinned = True
    except Exception:
        logger.info("tracker %r: could not pin anchor message (missing "
                    "Manage Messages permission?)", name)
        pinned = False
    rec = {"thread_id": str(thread["id"]), "anchor_id": str(anchor["id"]),
           "channel_id": home, "created_at": int(time.time()),
           "description": description[:500]}
    _save_tracker(r, agent, name, rec)
    return json.dumps({"ok": True, "created": True, "tracker": name,
                       "thread_id": rec["thread_id"], "pinned": pinned})


def _action_list(r, agent: str) -> str:
    out: List[Dict[str, Any]] = []
    for name, raw in (r.hgetall(_trackers_key(agent)) or {}).items():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        out.append({"tracker": name, "thread_id": rec.get("thread_id"),
                    "entries": r.hlen(_entries_key(agent, name)),
                    "description": rec.get("description") or ""})
    return json.dumps({"trackers": sorted(out, key=lambda t: t["tracker"]),
                       "count": len(out)}, ensure_ascii=False)


def _action_archive(r, agent: str, name: str) -> str:
    rec = _load_tracker(r, agent, name)
    if not rec:
        return _err(f"No tracker named {name!r}. Use action='list_trackers'.")
    # Best-effort Discord cleanup; records are dropped regardless.
    try:
        _api("DELETE", f"/channels/{rec['channel_id']}/pins/{rec['anchor_id']}")
    except Exception:
        pass
    try:
        _api("PATCH", f"/channels/{rec['thread_id']}", body={"archived": True})
    except Exception:
        pass
    _drop_tracker(r, agent, name, rec)
    return json.dumps({"ok": True, "archived": True, "tracker": name})


def _entry_content(key: str, content: str) -> str:
    return f"**[{key}]** {content}"[:CONTENT_MAX]


def _action_add(r, agent: str, name: str, key: str, content: str) -> str:
    rec, thread = _resolve_thread(r, agent, name)
    if not rec:
        return _err(f"No tracker named {name!r} (or its thread was deleted). "
                    "Create it with action='create_tracker'.")
    ek = _entries_key(agent, name)
    existing = r.hget(ek, key)
    if existing:
        # Stable-key contract: adding over an existing key edits in place
        # rather than appending a duplicate.
        return _action_edit(r, agent, name, key, content)
    if r.hlen(ek) >= MAX_ENTRIES:
        return _err(f"Entry limit reached ({MAX_ENTRIES}) for tracker {name!r}.")
    _ensure_unarchived(thread)
    msg = _api("POST", f"/channels/{rec['thread_id']}/messages",
               body={"content": _entry_content(key, content)})
    r.hset(ek, key, str(msg["id"]))
    return json.dumps({"ok": True, "added": True, "tracker": name,
                       "entry_key": key, "message_id": str(msg["id"])})


def _action_edit(r, agent: str, name: str, key: str, content: str) -> str:
    from tools.discord_tool import DiscordAPIError
    rec, thread = _resolve_thread(r, agent, name)
    if not rec:
        return _err(f"No tracker named {name!r} (or its thread was deleted).")
    ek = _entries_key(agent, name)
    msg_id = r.hget(ek, key)
    if not msg_id:
        return _err(f"No entry {key!r} in tracker {name!r}. Use "
                    "action='add_entry' to create it, or 'list_entries' to "
                    "see existing keys.")
    _ensure_unarchived(thread)
    try:
        _api("PATCH", f"/channels/{rec['thread_id']}/messages/{msg_id}",
             body={"content": _entry_content(key, content)})
    except DiscordAPIError as e:
        if e.status == 404:
            r.hdel(ek, key)
            return _err(f"Entry {key!r} message was deleted on Discord; the "
                        "stale record was dropped. Re-add it with "
                        "action='add_entry'.")
        raise
    return json.dumps({"ok": True, "edited": True, "tracker": name,
                       "entry_key": key})


def _action_delete(r, agent: str, name: str, key: str) -> str:
    from tools.discord_tool import DiscordAPIError
    rec, _thread = _resolve_thread(r, agent, name)
    if not rec:
        return _err(f"No tracker named {name!r} (or its thread was deleted).")
    ek = _entries_key(agent, name)
    msg_id = r.hget(ek, key)
    if not msg_id:
        return _err(f"No entry {key!r} in tracker {name!r}.")
    try:
        _api("DELETE", f"/channels/{rec['thread_id']}/messages/{msg_id}")
    except DiscordAPIError as e:
        if e.status != 404:
            raise
    r.hdel(ek, key)
    return json.dumps({"ok": True, "deleted": True, "tracker": name,
                       "entry_key": key})


def _action_entries(r, agent: str, name: str) -> str:
    rec = _load_tracker(r, agent, name)
    if not rec:
        return _err(f"No tracker named {name!r}.")
    keys = sorted((r.hgetall(_entries_key(agent, name)) or {}).keys())
    return json.dumps({"tracker": name, "entry_keys": keys,
                       "count": len(keys)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

def tracking_thread_tool(action: str = "", name: str = "",
                         entry_key: str = "", content: str = "",
                         description: str = "",
                         **_kwargs: Any) -> str:
    """Manage this agent's pinned tracking threads in its home channel."""
    from tools.discord_tool import DiscordAPIError
    action = (action or "").strip().lower()
    agent = _agent_name()
    if not _home_channel_id():
        return _err("DISCORD_HOME_CHANNEL is not configured — this agent has "
                    "no home channel to host tracker threads.")
    try:
        r = _redis()
    except Exception as e:
        return _err(str(e))

    name = _norm_name(name)
    entry_key = _norm_key(entry_key)
    try:
        if action == "list_trackers":
            return _action_list(r, agent)
        if not name:
            return _err("'name' (tracker name) is required for this action.")
        if action == "create_tracker":
            return _action_create(r, agent, name, str(description or "").strip())
        if action == "archive_tracker":
            return _action_archive(r, agent, name)
        if action == "list_entries":
            return _action_entries(r, agent, name)
        if action in ("add_entry", "edit_entry"):
            if not entry_key:
                return _err("'entry_key' is required for add/edit_entry.")
            if not str(content or "").strip():
                return _err("'content' is required for add/edit_entry.")
            fn = _action_add if action == "add_entry" else _action_edit
            return fn(r, agent, name, entry_key, str(content).strip())
        if action == "delete_entry":
            if not entry_key:
                return _err("'entry_key' is required for delete_entry.")
            return _action_delete(r, agent, name, entry_key)
        return _err(f"Unknown action {action!r}. Use create_tracker, "
                    "list_trackers, archive_tracker, add_entry, edit_entry, "
                    "delete_entry, or list_entries.")
    except DiscordAPIError as e:
        if e.status == 403:
            return _err("Discord refused the request (403). The bot likely "
                        "lacks a permission in the home channel (Send "
                        "Messages, Create Public Threads, Manage Messages "
                        "for pinning, or Manage Threads).")
        return _err(f"Discord API error {e.status}: {e.body[:300]}")
    except Exception as e:
        logger.exception("tracking_thread tool failed")
        return _err(f"tracking_thread failed: {e}")


registry.register(
    name="tracking_thread",
    toolset=TOOLSET,
    schema={
        "name": "tracking_thread",
        "description": (
            "Maintain pinned tracking threads in YOUR OWN Discord home "
            "channel — rolling, structured logs you own (e.g. a day-trade "
            "log, a live open-positions board, periodic check-ins). Each "
            "tracker is a pinned thread; each entry is a message addressed "
            "by a stable entry_key you choose (e.g. 'QQQ-2026-08-13'), so a "
            "later edit_entry rewrites the SAME message in place instead of "
            "appending a duplicate. Use add_entry for append-style logs and "
            "edit_entry for live boards (one entry per open position, "
            "updated as metrics change). add_entry on an existing key edits "
            "it in place. Threads and entries survive restarts and are "
            "reused, never duplicated. Also callable from your scheduled "
            "cron-job turns for automated check-ins. Trackers live only in "
            "your home channel; nobody converses in them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_tracker", "list_trackers",
                             "archive_tracker", "add_entry", "edit_entry",
                             "delete_entry", "list_entries"],
                },
                "name": {"type": "string",
                         "description": "Tracker name (all actions except list_trackers)."},
                "description": {"type": "string",
                                "description": "create_tracker: short purpose blurb shown in the pinned anchor."},
                "entry_key": {"type": "string",
                              "description": "Stable key identifying an entry (add/edit/delete_entry)."},
                "content": {"type": "string",
                            "description": "Entry text (add/edit_entry). Max ~1900 chars."},
            },
            "required": ["action"],
        },
    },
    handler=tracking_thread_tool,
    check_fn=lambda: bool(os.getenv("REDIS_URL"))
    and bool(os.getenv("DISCORD_BOT_TOKEN"))
    and bool(os.getenv("DISCORD_HOME_CHANNEL")),
    description="Agent-managed pinned tracking threads in the Discord home channel",
)
