#!/usr/bin/env python3
"""
Redis Memory Sync — Persistent memory for agents without Railway volumes.

For agents that hit the Railway volume limit (10/project), this script
provides Redis-backed persistence for the Hermes workspace directory.

Usage:
    # On startup — restore memory from Redis
    python3 redis_memory_sync.py --action restore --agent samantha

    # On shutdown / periodic save — save memory to Redis
    python3 redis_memory_sync.py --action save --agent samantha

    # Watch mode — auto-save every 5 minutes
    python3 redis_memory_sync.py --action watch --agent samantha
"""
import argparse
import gzip
import json
import os
import sys
import time
import base64
from pathlib import Path
from datetime import datetime

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
WORKSPACE_DIR = os.getenv("HERMES_WORKSPACE_DIR", "/root/.hermes/workspace")
# MEMORY_DIR is hardcoded to /root/.hermes — the standard single-agent Railway
# layout where HERMES_HOME is unset and defaults to this path. If Railway
# deployments ever adopt profile mode (HERMES_HOME=/root/.hermes/profiles/<name>),
# this would need to read $HERMES_HOME instead so save/restore paths match the
# real cron/jobs.py JOBS_FILE location.
MEMORY_DIR = "/root/.hermes"

# Files to persist (relative to MEMORY_DIR)
PERSIST_FILES = [
    "MEMORY.md",
    "USER.md",
    # The memory tool actually writes to ~/.hermes/memories/ — the root-level
    # MEMORY.md/USER.md above are legacy/deploy copies. Both are kept in sync
    # so nothing the agent learns is lost on redeploy.
    "memories/MEMORY.md",
    "memories/USER.md",
    "workspace/MEMORY.md",
    # LEGACY — no part of the cron subsystem reads/writes this path. The active
    # cron job store is cron/jobs.json (see entry below). Kept here to avoid
    # breaking workspace_sync.py's MEMORY_SYNC_OWNED handoff; annotated rather
    # than removed so the history is clear.
    "workspace/cron_jobs.json",
    "workspace/tasks.json",
    "workspace/notes.md",
    # Session index (which sessions exist / channel bindings)
    "sessions/sessions.json",
    # Active cron job store — persists agent-scheduled jobs across Railway
    # redeploys. Path: /root/.hermes/cron/jobs.json (cron/jobs.py: JOBS_FILE).
    # Redis key: agent:{name}:memory:cron:jobs.json
    # Output files (cron/output/…) are intentionally excluded — they are
    # ephemeral per-job artefacts and would exceed the 512 KB cap.
    "cron/jobs.json",
    # Discord cron-thread state (thread ids + job→message map). Persisting it
    # lets redeploys reuse the existing threads/pinned anchors instead of
    # creating duplicates (the module also self-heals by scanning pins, but
    # keeping state avoids the extra API churn).
    "cron/discord_cron_threads.json",
]

# Directories whose text files are swept wholesale (relative to MEMORY_DIR).
# key layout: agent:{name}:memory:{dir}:{filename}
SWEEP_DIRS = {
    "workspace": (".md", ".txt", ".json", ".yaml", ".yml"),
    "memories": (".md", ".txt", ".json"),
    "sessions": (".json",),
}

# Binary files persisted base64-encoded. state.db holds ALL session
# transcripts/history — without it, agents lose their conversation history on
# every redeploy. Snapshotted via the SQLite backup API for a consistent copy.
BINARY_FILES = [
    "state.db",
]

# Max size per file (bytes) to prevent Redis bloat
MAX_FILE_SIZE = 512 * 1024  # 512KB
MAX_BINARY_SIZE = 64 * 1024 * 1024  # 64MB — state.db grows with history


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def save_memory(agent_name: str):
    """Save agent memory files to Redis."""
    r = get_redis()
    saved = []
    skipped = []

    for rel_path in PERSIST_FILES:
        full_path = Path(MEMORY_DIR) / rel_path
        if not full_path.exists():
            continue

        size = full_path.stat().st_size
        if size > MAX_FILE_SIZE:
            skipped.append(f"{rel_path} (too large: {size} bytes)")
            continue

        content = full_path.read_text(encoding="utf-8", errors="replace")
        key = f"agent:{agent_name}:memory:{rel_path.replace('/', ':')}"
        r.set(key, content)
        saved.append(rel_path)

    # Sweep whole directories for text files (workspace, memories, sessions)
    for dirname, suffixes in SWEEP_DIRS.items():
        base = Path(WORKSPACE_DIR) if dirname == "workspace" else Path(MEMORY_DIR) / dirname
        if not base.exists():
            continue
        for f in base.iterdir():
            if f.is_file() and f.suffix in suffixes:
                rel = f"{dirname}/{f.name}"
                if rel in PERSIST_FILES:
                    continue  # Already handled
                size = f.stat().st_size
                if size > MAX_FILE_SIZE:
                    skipped.append(f"{rel} (too large)")
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                key = f"agent:{agent_name}:memory:{dirname}:{f.name}"
                r.set(key, content)
                saved.append(rel)

    # Binary files (state.db) — snapshot via SQLite backup API so we never
    # capture a half-written database, then gzip + base64. Compression matters
    # beyond Redis footprint: when a blob exceeded MAX_BINARY_SIZE the save was
    # skipped but the OLD blob stayed in Redis, so every redeploy restored a
    # days-old state.db whose gateway_routing timestamps tripped false
    # "inactive for 24h" session resets mid-conversation (#104). SQLite DBs
    # typically compress 4-10x, keeping saves well under the cap.
    for rel_path in BINARY_FILES:
        full_path = Path(MEMORY_DIR) / rel_path
        if not full_path.exists():
            continue
        try:
            data = _snapshot_sqlite(full_path) if full_path.suffix == ".db" else full_path.read_bytes()
        except Exception as e:
            skipped.append(f"{rel_path} (snapshot failed: {e})")
            continue
        raw_size = len(data)
        data = gzip.compress(data, compresslevel=6)
        if len(data) > MAX_BINARY_SIZE:
            skipped.append(
                f"{rel_path} (too large: {len(data)} bytes gzipped, {raw_size} raw)"
            )
            # Loud, operator-visible alert: session history is NO LONGER being
            # persisted for this agent. Surfaced in Redis so the dashboard /
            # health checks can pick it up.
            print(
                f"[{agent_name}] WARNING: {rel_path} is {len(data)} bytes "
                f"(cap {MAX_BINARY_SIZE}) — session history is NOT being backed "
                "up to Redis. Prune old sessions or raise MAX_BINARY_SIZE.",
                file=sys.stderr,
            )
            r.set(
                f"agent:{agent_name}:memory:alert:state_db_too_large",
                json.dumps({"size": len(data), "cap": MAX_BINARY_SIZE,
                            "at": datetime.utcnow().isoformat()}),
            )
            continue
        # Clear any stale size alert once we fit under the cap again
        r.delete(f"agent:{agent_name}:memory:alert:state_db_too_large")
        key = f"agent:{agent_name}:memory:binary:{rel_path.replace('/', ':')}"
        r.set(key, base64.b64encode(data).decode("ascii"))
        # Per-blob freshness stamp: last_saved covers text files even when the
        # binary save is skipped, so restore uses THIS key to detect a stale
        # binary snapshot (#104).
        r.set(f"{key}:saved_at", datetime.utcnow().isoformat())
        saved.append(rel_path)

    r.set(f"agent:{agent_name}:memory:last_saved", datetime.utcnow().isoformat())
    print(f"[{agent_name}] Saved {len(saved)} files to Redis memory.")
    if skipped:
        print(f"[{agent_name}] Skipped: {', '.join(skipped)}")
    return saved


def _snapshot_sqlite(path: Path) -> bytes:
    """Take a consistent snapshot of a SQLite DB via the backup API."""
    import sqlite3
    import tempfile

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(str(path))
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return Path(tmp).read_bytes()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def restore_memory(agent_name: str):
    """Restore agent memory files from Redis.

    Existing local files are NEVER overwritten: on a fresh (ephemeral)
    container nothing exists yet so everything restores; on a volume-backed
    agent the local copies are newer than the last 5-minute Redis save, so
    clobbering them would lose data.
    """
    r = get_redis()
    restored = []
    skipped = []

    # Get all keys for this agent's memory
    pattern = f"agent:{agent_name}:memory:*"
    keys = r.keys(pattern)

    for key in keys:
        if key.endswith(":last_saved") or key.endswith(":saved_at"):
            continue

        # Reconstruct file path from key
        suffix = key.replace(f"agent:{agent_name}:memory:", "")

        # Metadata keys (alerts etc.) are not files
        if suffix.startswith("alert:"):
            continue

        is_binary = suffix.startswith("binary:")
        if is_binary:
            # Stale-snapshot guard (#104): if the per-blob saved_at stamp is
            # much older than the agent-wide last_saved, saves for this blob
            # have been failing/skipped (e.g. size cap) and we are about to
            # restore an OLD snapshot. Restore anyway (history beats nothing)
            # but warn loudly so the operator sees it.
            try:
                blob_saved_at = r.get(f"{key}:saved_at")
                last_saved = r.get(f"agent:{agent_name}:memory:last_saved")
                if blob_saved_at and last_saved:
                    age = (datetime.fromisoformat(last_saved)
                           - datetime.fromisoformat(blob_saved_at)).total_seconds()
                    if age > 3600:
                        print(
                            f"[{agent_name}] WARNING: restoring STALE snapshot "
                            f"{key} — last saved {age/3600:.1f}h before the most "
                            "recent sync (its saves are being skipped, likely "
                            "size cap).",
                            file=sys.stderr,
                        )
            except Exception:
                pass
            suffix = suffix[len("binary:"):]

        rel_path = suffix.replace(":", "/")
        full_path = Path(MEMORY_DIR) / rel_path
        # Workspace files are anchored at WORKSPACE_DIR (may be overridden)
        if suffix.startswith("workspace:") or rel_path.startswith("workspace/"):
            full_path = Path(WORKSPACE_DIR) / rel_path.split("/", 1)[1]

        if full_path.exists():
            skipped.append(str(full_path))
            continue

        content = r.get(key)
        if not content:
            continue
        full_path.parent.mkdir(parents=True, exist_ok=True)
        if is_binary:
            blob = base64.b64decode(content)
            # New blobs are gzip-compressed; pre-compression blobs restore
            # unchanged (gzip magic sniff keeps this backward compatible).
            if blob[:2] == b"\x1f\x8b":
                blob = gzip.decompress(blob)
            full_path.write_bytes(blob)
        else:
            full_path.write_text(content, encoding="utf-8")
        restored.append(str(full_path))

    if restored:
        print(f"[{agent_name}] Restored {len(restored)} files from Redis memory.")
    else:
        print(f"[{agent_name}] No memory found in Redis (fresh start).")
    if skipped:
        print(f"[{agent_name}] Kept {len(skipped)} existing local files (not overwritten).")
    return restored


def watch_memory(agent_name: str, interval: int = 300):
    """Watch mode: auto-save memory every N seconds."""
    print(f"[{agent_name}] Starting memory watch (saving every {interval}s)...")
    while True:
        try:
            save_memory(agent_name)
        except Exception as e:
            print(f"[{agent_name}] Save error: {e}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Redis Memory Sync for Hermes agents")
    parser.add_argument("--action", choices=["save", "restore", "watch"], required=True)
    parser.add_argument("--agent", required=True, help="Agent name (e.g., samantha)")
    parser.add_argument("--interval", type=int, default=300, help="Watch interval in seconds")
    args = parser.parse_args()

    if args.action == "save":
        save_memory(args.agent)
    elif args.action == "restore":
        restore_memory(args.agent)
    elif args.action == "watch":
        watch_memory(args.agent, args.interval)


if __name__ == "__main__":
    main()
