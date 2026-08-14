"""Pinned cron-jobs & cron-history Discord threads (LOCAL feature).

Keeps two pinned threads in the agent's Discord home channel:

  * ``cron-jobs-🕐`` — one post per currently-scheduled cron job (name,
    schedule, next run, paused/active). Posts are edited when the job
    changes and deleted when the job is removed.
  * ``cron-history`` — append-only log of every cron run (job name, run
    time, success/failure). Never edited or deleted.

Design notes:
  - Kept in its own module (never merged into upstream files' logic) per the
    local upstream-sync-safety convention. Hooks in cron/scheduler.py are
    two tiny best-effort call sites.
  - All Discord I/O runs as coroutines scheduled onto the gateway's event
    loop via ``run_coroutine_threadsafe`` — the cron ticker thread NEVER
    blocks on Discord and posting failures never affect job execution.
  - Reconciliation is driven from the scheduler tick (60s cadence), so jobs
    created/removed while the gateway was down converge on the first tick
    after startup. ``request_sync()`` (wired into
    ``_notify_provider_jobs_changed``) triggers an immediate sync when a
    job is mutated through the tool/CLI/REST surfaces.
  - "Pinned thread": Discord text channels cannot pin threads directly, so
    each thread is anchored to a pinned starter message in the home channel
    and kept at the maximum auto-archive duration (unarchived on sync if
    Discord archived it anyway).
  - Missing permissions disable the feature for the process lifetime with a
    single warning instead of crashing or spamming.

State (thread ids + job→message map) is persisted alongside the cron store
at ``<cron dir>/discord_cron_threads.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Dict, Optional

from cron.jobs import CRON_DIR, list_jobs
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

JOBS_THREAD_NAME = "cron-jobs-🕐"
HISTORY_THREAD_NAME = "cron-history"
STATE_FILE = CRON_DIR / "discord_cron_threads.json"

# Marker line embedded in every job post so reconciliation can re-map posts
# to jobs even if the local state file is lost.
_ID_MARKER_RE = re.compile(r"🆔 `([^`\n]+)`")

_lock = threading.Lock()
# Serializes _bootstrap on the gateway loop so a concurrent sync + history
# log can never double-create the threads.
_bootstrap_lock: Optional[asyncio.Lock] = None
_disabled = False          # set on Forbidden / fatal errors; process-lifetime
_pins_disabled = False     # no perms for pin management; threads keep working
_state_loaded = False      # persisted thread ids loaded once per process
_sync_inflight = False
_cached_adapter = None     # last live Discord adapter seen by schedule_sync
_cached_loop = None        # gateway event loop
_message_map: Optional[Dict[str, int]] = None  # job_id -> discord message id
_jobs_thread_id: Optional[int] = None
_history_thread_id: Optional[int] = None
_last_posted: Dict[str, str] = {}  # job_id -> last content posted/edited


def _enabled() -> bool:
    if _disabled:
        return False
    if os.getenv("HERMES_CRON_DISCORD_THREADS", "1").strip().lower() in ("0", "false", "off"):
        return False
    return bool(os.getenv("DISCORD_HOME_CHANNEL", "").strip())


def _find_discord_adapter(adapters: Any):
    """Locate the live Discord adapter in the gateway's adapters dict."""
    if not adapters:
        return None
    try:
        for platform, adapter in dict(adapters).items():
            value = getattr(platform, "value", platform)
            if str(value).lower() == "discord":
                return adapter
    except Exception:
        return None
    return None


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save_state() -> None:
    try:
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "jobs_thread_id": _jobs_thread_id,
                    "history_thread_id": _history_thread_id,
                    "messages": {k: v for k, v in (_message_map or {}).items()},
                },
                f,
            )
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        logger.debug("cron discord-threads: state save failed: %s", e)


def _format_job_post(job: Dict[str, Any]) -> str:
    name = str(job.get("name") or job.get("id") or "cron job")
    schedule = str(job.get("schedule_display") or "?")
    next_run = job.get("next_run_at") or "—"
    if isinstance(next_run, str) and "T" in next_run:
        next_run = next_run.replace("T", " ")[:19]
    paused = (not job.get("enabled", True)) or (str(job.get("state") or "") == "paused")
    status = "⏸️ paused" if paused else "✅ active"
    return (
        f"🆔 `{job.get('id')}`\n"
        f"**{name[:200]}**\n"
        f"Schedule: `{schedule[:100]}`\n"
        f"Next run: {next_run}\n"
        f"Status: {status}"
    )


def _format_history_entry(job: Dict[str, Any], success: bool, error: Optional[str], ran_at: str) -> str:
    name = str(job.get("name") or job.get("id") or "cron job")
    icon = "✅" if success else "❌"
    line = f"{icon} **{name[:150]}** — {ran_at}"
    if not success and error:
        err = re.sub(r"\s+", " ", str(error)).strip()
        if len(err) > 200:
            err = err[:197] + "..."
        line += f"\n> {err}"
    return line


def _disable(reason: str) -> None:
    global _disabled
    if not _disabled:
        _disabled = True
        logger.warning("cron discord-threads: disabled — %s", reason)


# ---------------------------------------------------------------------------
# Async internals (run on the gateway event loop)
# ---------------------------------------------------------------------------

async def _get_home_channel(adapter):
    import discord  # gateway env always has it when a Discord adapter exists

    client = getattr(adapter, "_client", None)
    if client is None or getattr(client, "user", None) is None:
        return None, None
    raw = os.getenv("DISCORD_HOME_CHANNEL", "").strip()
    try:
        channel_id = int(raw)
    except ValueError:
        _disable(f"DISCORD_HOME_CHANNEL is not a numeric channel id: {raw!r}")
        return None, None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.Forbidden:
            _disable("no access to home channel")
            return None, None
        except discord.HTTPException as e:
            logger.debug("cron discord-threads: fetch_channel failed: %s", e)
            return None, None
    if not hasattr(channel, "create_thread"):
        _disable("home channel does not support threads")
        return None, None
    return client, channel


async def _resolve_thread(client, channel, thread_id: Optional[int], name: str):
    """Find an existing thread by stored id or name; return it or None."""
    import discord

    # 1. Stored id (in-memory or restored from the persisted state file).
    if thread_id:
        th = client.get_channel(thread_id)
        if th is None:
            try:
                th = await client.fetch_channel(thread_id)
            except discord.HTTPException:
                th = None
        if (th is not None
                and getattr(th, "parent_id", None) == channel.id
                and getattr(th, "name", None) == name):
            return th
    # 2. Active threads by name.
    for th in getattr(channel, "threads", []) or []:
        if th.name == name:
            return th
    # 3. Archived threads by name.
    try:
        async for th in channel.archived_threads(limit=50):
            if th.name == name:
                return th
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass
    return None


def _anchor_text(name: str, thread_id: Optional[int] = None) -> str:
    text = f"📌 {name}"
    if thread_id:
        text += f" → <#{thread_id}>"
    return text


def _anchor_re(name: str) -> "re.Pattern[str]":
    # EXACT anchor formats only: "📌 <name>" or "📌 <name> → <#123>".
    # A prefix match would let e.g. "📌 cron-history-old" be deleted.
    return re.compile(rf"^📌 {re.escape(name)}( → <#\d+>)?$")


async def _channel_pins(channel):
    """Return the channel's pinned messages, or None when the pin inventory
    is unavailable (permission denied / transient API failure). Callers must
    NOT treat None as 'no pins exist'."""
    global _pins_disabled
    import discord

    if _pins_disabled:
        return None
    try:
        pins = channel.pins()
        if hasattr(pins, "__aiter__"):  # discord.py >= 2.4 returns an async iterator
            return [m async for m in pins]
        return list(await pins)
    except discord.Forbidden:
        if not _pins_disabled:
            _pins_disabled = True
            logger.warning(
                "cron discord-threads: no permission to read pins — anchor "
                "pin management disabled (threads still work)")
        return None
    except Exception as e:
        logger.debug("cron discord-threads: pins fetch failed: %s", e)
        return None


def _matching_anchors(client, pins, name):
    """Bot-authored pinned messages that EXACTLY match the anchor format."""
    me = getattr(getattr(client, "user", None), "id", None)
    pat = _anchor_re(name)
    out = []
    for m in pins or []:
        if getattr(getattr(m, "author", None), "id", None) != me:
            continue
        if pat.match(m.content or ""):
            out.append(m)
    return out


async def _dedup_anchors(pins_matching, keep_id: Optional[int], name: str) -> None:
    """Unpin + delete every duplicate anchor except the one with keep_id.
    Only messages that passed the exact `_matching_anchors` filter are ever
    touched."""
    global _pins_disabled
    import discord

    for m in pins_matching:
        if keep_id is not None and m.id == keep_id:
            continue
        try:
            await m.unpin()
        except discord.Forbidden:
            _pins_disabled = True
            logger.warning("cron discord-threads: no permission to unpin — "
                           "anchor pin management disabled")
            return
        except Exception:
            pass
        try:
            await m.delete()
        except discord.Forbidden:
            _pins_disabled = True
            logger.warning("cron discord-threads: no permission to delete "
                           "anchors — anchor pin management disabled")
            return
        except Exception as e:
            logger.debug("cron discord-threads: dup anchor delete failed for %s: %s", name, e)
            continue
        logger.info("cron discord-threads: removed duplicate anchor pin for %s (%s)", name, m.id)


async def _ensure_thread(client, channel, thread_id: Optional[int], name: str):
    """Locate or create the named thread, keep exactly one pinned anchor,
    keep it unarchived."""
    global _pins_disabled
    import discord

    th = await _resolve_thread(client, channel, thread_id, name)
    pins = await _channel_pins(channel)  # None = inventory unavailable
    anchors = _matching_anchors(client, pins, name)

    if th is None and anchors:
        # State + thread lookup missed, but an anchor pin survives (e.g. the
        # thread aged out of archived_threads(limit=50)). A thread spawned
        # from a message shares its id — recover it from the newest anchor,
        # but only bind to a thread whose name AND parent both check out.
        for anchor in sorted(anchors, key=lambda m: m.id, reverse=True):
            recovered = getattr(anchor, "thread", None)
            if recovered is None:
                try:
                    recovered = await client.fetch_channel(anchor.id)
                except discord.HTTPException:
                    recovered = None
            if (recovered is not None
                    and getattr(recovered, "parent_id", None) == channel.id
                    and getattr(recovered, "name", None) == name):
                th = recovered
                break

    if th is None:
        if pins is None and not _pins_disabled:
            # Pin inventory temporarily unreadable — an anchor may still
            # exist. Creating now risks a duplicate; retry next sync instead.
            logger.debug("cron discord-threads: deferring create of %s — "
                         "pin inventory unavailable", name)
            return None
        # Create: anchor message in home channel, pin it, spawn the thread.
        anchor = await channel.send(_anchor_text(name))
        try:
            await anchor.pin()
        except discord.Forbidden:
            _pins_disabled = True
            logger.warning("cron discord-threads: no permission to pin anchor "
                           "for %s — anchor pin management disabled", name)
        except discord.HTTPException as e:
            logger.warning("cron discord-threads: could not pin anchor for %s: %s", name, e)
        th = await channel.create_thread(
            name=name, message=anchor, auto_archive_duration=10080
        )
        try:
            await anchor.edit(content=_anchor_text(name, th.id))
        except Exception:
            pass
        logger.info("cron discord-threads: created thread %s (%s)", name, th.id)
        return th

    if pins is not None:
        # Reuse: exactly one pinned anchor — the thread's own starter message.
        keep = next((m for m in anchors if m.id == th.id), None)
        await _dedup_anchors(anchors, th.id, name)
        if keep is None and not _pins_disabled:
            # The live thread's starter message is not pinned (or was deleted).
            try:
                starter = await channel.fetch_message(th.id)
                await starter.edit(content=_anchor_text(name, th.id))
                await starter.pin()
            except discord.Forbidden:
                _pins_disabled = True
                logger.warning("cron discord-threads: no permission to pin — "
                               "anchor pin management disabled")
            except Exception as e:
                logger.debug("cron discord-threads: could not (re)pin anchor for %s: %s", name, e)
        elif keep is not None and "<#" not in (keep.content or ""):
            try:
                await keep.edit(content=_anchor_text(name, th.id))
            except Exception:
                pass

    # Unarchive / extend auto-archive when needed.
    try:
        if getattr(th, "archived", False):
            await th.edit(archived=False, auto_archive_duration=10080)
        elif getattr(th, "auto_archive_duration", 10080) != 10080:
            await th.edit(auto_archive_duration=10080)
    except discord.HTTPException as e:
        logger.debug("cron discord-threads: thread edit failed for %s: %s", name, e)
    return th


async def _bootstrap(adapter):
    """Ensure both threads exist. Returns (jobs_thread, history_thread) or (None, None)."""
    global _jobs_thread_id, _history_thread_id, _bootstrap_lock
    import discord

    global _state_loaded
    if _bootstrap_lock is None:
        _bootstrap_lock = asyncio.Lock()
    async with _bootstrap_lock:
        if not _state_loaded:
            # Fresh process (redeploy): restore persisted thread ids so
            # resolution goes straight to fetch-by-id instead of relying on
            # active/archived scans or pin discovery.
            _state_loaded = True
            state = _load_state()
            try:
                if _jobs_thread_id is None and state.get("jobs_thread_id"):
                    _jobs_thread_id = int(state["jobs_thread_id"])
                if _history_thread_id is None and state.get("history_thread_id"):
                    _history_thread_id = int(state["history_thread_id"])
            except (TypeError, ValueError):
                pass
        client, channel = await _get_home_channel(adapter)
        if channel is None:
            return None, None
        try:
            jobs_th = await _ensure_thread(client, channel, _jobs_thread_id, JOBS_THREAD_NAME)
            hist_th = await _ensure_thread(client, channel, _history_thread_id, HISTORY_THREAD_NAME)
        except discord.Forbidden:
            _disable("missing permissions to create/manage threads in home channel")
            return None, None
        if jobs_th is None or hist_th is None:
            # Deferred (pin inventory unavailable) — retry on the next sync.
            return None, None
        if _jobs_thread_id != jobs_th.id or _history_thread_id != hist_th.id:
            _jobs_thread_id, _history_thread_id = jobs_th.id, hist_th.id
            _save_state()
        return jobs_th, hist_th


async def _rebuild_message_map(client, jobs_thread) -> Dict[str, int]:
    """Re-map job posts from thread history (self-healing after state loss)."""
    mapping: Dict[str, int] = {}
    try:
        async for msg in jobs_thread.history(limit=200):
            if msg.author.id != client.user.id:
                continue
            m = _ID_MARKER_RE.search(msg.content or "")
            if m:
                # Keep the newest post per job id; delete duplicates.
                if m.group(1) in mapping:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
                else:
                    mapping[m.group(1)] = msg.id
    except Exception as e:
        logger.debug("cron discord-threads: history scan failed: %s", e)
    return mapping


async def _sync_async(adapter):
    """Reconcile jobs.json against the cron-jobs thread."""
    global _message_map
    import discord

    jobs_th, _hist_th = await _bootstrap(adapter)
    if jobs_th is None:
        return
    client = adapter._client

    if _message_map is None:
        # First sync this process: load persisted state, then verify against
        # the actual thread history (authoritative — survives state loss and
        # catches posts whose messages were deleted manually).
        state = _load_state()
        _message_map = await _rebuild_message_map(client, jobs_th)
        # Keep persisted entries that history missed (e.g. >200 messages).
        for jid, mid in (state.get("messages") or {}).items():
            _message_map.setdefault(str(jid), int(mid))
        _last_posted.clear()

    jobs = {str(j["id"]): j for j in list_jobs(include_disabled=True)}
    changed = False

    # Delete posts for jobs that no longer exist.
    for jid in list(_message_map.keys()):
        if jid not in jobs:
            mid = _message_map.pop(jid)
            _last_posted.pop(jid, None)
            changed = True
            try:
                msg = jobs_th.get_partial_message(mid)
                await msg.delete()
            except discord.HTTPException as e:
                logger.debug("cron discord-threads: delete post for %s failed: %s", jid, e)

    # Post new jobs / edit changed ones.
    for jid, job in jobs.items():
        content = _format_job_post(job)
        mid = _message_map.get(jid)
        if mid is None:
            try:
                msg = await jobs_th.send(content)
                _message_map[jid] = msg.id
                _last_posted[jid] = content
                changed = True
            except discord.HTTPException as e:
                logger.debug("cron discord-threads: post for %s failed: %s", jid, e)
        elif _last_posted.get(jid) != content:
            try:
                await jobs_th.get_partial_message(mid).edit(content=content)
                _last_posted[jid] = content
            except discord.NotFound:
                # Message deleted out from under us — repost next sync.
                _message_map.pop(jid, None)
                _last_posted.pop(jid, None)
                changed = True
            except discord.HTTPException as e:
                logger.debug("cron discord-threads: edit for %s failed: %s", jid, e)

    if changed:
        _save_state()


async def _log_run_async(adapter, job: Dict[str, Any], success: bool,
                         error: Optional[str], ran_at: str):
    """Append one entry to the cron-history thread (append-only)."""
    _jobs_th, hist_th = await _bootstrap(adapter)
    if hist_th is None:
        return
    try:
        await hist_th.send(_format_history_entry(job, success, error, ran_at))
    except Exception as e:
        logger.debug("cron discord-threads: history post failed: %s", e)


# ---------------------------------------------------------------------------
# Public hooks (called from the cron scheduler / tool surfaces)
# ---------------------------------------------------------------------------

def schedule_sync(adapters=None, loop=None) -> None:
    """Fire-and-forget reconcile of the cron-jobs thread. Never raises,
    never blocks the calling (ticker) thread."""
    global _sync_inflight, _cached_adapter, _cached_loop
    try:
        if not _enabled():
            return
        adapter = _find_discord_adapter(adapters) or _cached_adapter
        if adapter is None or loop is None and _cached_loop is None:
            return
        use_loop = loop or _cached_loop
        if use_loop is None or use_loop.is_closed():
            return
        _cached_adapter, _cached_loop = adapter, use_loop
        with _lock:
            if _sync_inflight:
                return
            _sync_inflight = True

        def _done(fut):
            global _sync_inflight
            with _lock:
                _sync_inflight = False
            try:
                exc = fut.exception()
                if exc is not None:
                    logger.warning("cron discord-threads: sync failed: %s", exc)
            except Exception:
                pass

        fut = asyncio.run_coroutine_threadsafe(_sync_async(adapter), use_loop)
        fut.add_done_callback(_done)
    except Exception as e:
        with _lock:
            _sync_inflight = False
        logger.debug("cron discord-threads: schedule_sync error: %s", e)


def request_sync() -> None:
    """Immediate sync after a job mutation (tool/CLI/REST). Uses the adapter
    and loop cached from the last ticker sync; silently no-ops when the
    gateway hasn't ticked yet (the next tick will reconcile anyway)."""
    schedule_sync(None, None)


def log_run(job: Dict[str, Any], success: bool, error: Optional[str] = None,
            *, adapters=None, loop=None) -> None:
    """Append a run entry to cron-history. Fire-and-forget; never raises."""
    global _cached_adapter, _cached_loop
    try:
        if not _enabled():
            return
        adapter = _find_discord_adapter(adapters) or _cached_adapter
        use_loop = loop or _cached_loop
        if adapter is None or use_loop is None or use_loop.is_closed():
            return
        _cached_adapter, _cached_loop = adapter, use_loop
        ran_at = _hermes_now().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        fut = asyncio.run_coroutine_threadsafe(
            _log_run_async(adapter, dict(job), bool(success), error, ran_at),
            use_loop,
        )

        def _done(f):
            try:
                exc = f.exception()
                if exc is not None:
                    logger.debug("cron discord-threads: log_run failed: %s", exc)
            except Exception:
                pass

        fut.add_done_callback(_done)
    except Exception as e:
        logger.debug("cron discord-threads: log_run error: %s", e)
