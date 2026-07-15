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
MEMORY_DIR = "/root/.hermes"

# Files to persist (relative to MEMORY_DIR)
PERSIST_FILES = [
    "MEMORY.md",
    "USER.md",
    "workspace/MEMORY.md",
    "workspace/cron_jobs.json",
    "workspace/tasks.json",
    "workspace/notes.md",
]

# Max size per file (bytes) to prevent Redis bloat
MAX_FILE_SIZE = 512 * 1024  # 512KB


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
        r.set(f"agent:{agent_name}:memory:last_saved", datetime.utcnow().isoformat())
        saved.append(rel_path)

    # Also save any files in workspace directory
    workspace = Path(WORKSPACE_DIR)
    if workspace.exists():
        for f in workspace.iterdir():
            if f.is_file() and f.suffix in (".md", ".txt", ".json", ".yaml", ".yml"):
                rel = f"workspace/{f.name}"
                if rel in PERSIST_FILES:
                    continue  # Already handled
                size = f.stat().st_size
                if size > MAX_FILE_SIZE:
                    skipped.append(f"{rel} (too large)")
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                key = f"agent:{agent_name}:memory:workspace:{f.name}"
                r.set(key, content)
                saved.append(rel)

    print(f"[{agent_name}] Saved {len(saved)} files to Redis memory.")
    if skipped:
        print(f"[{agent_name}] Skipped: {', '.join(skipped)}")
    return saved


def restore_memory(agent_name: str):
    """Restore agent memory files from Redis."""
    r = get_redis()
    restored = []

    # Get all keys for this agent's memory
    pattern = f"agent:{agent_name}:memory:*"
    keys = r.keys(pattern)

    for key in keys:
        if key.endswith(":last_saved"):
            continue

        # Reconstruct file path from key
        suffix = key.replace(f"agent:{agent_name}:memory:", "")
        
        if suffix.startswith("workspace:"):
            filename = suffix.replace("workspace:", "")
            full_path = Path(WORKSPACE_DIR) / filename
        else:
            rel_path = suffix.replace(":", "/")
            full_path = Path(MEMORY_DIR) / rel_path

        # Create parent directories
        full_path.parent.mkdir(parents=True, exist_ok=True)

        content = r.get(key)
        if content:
            full_path.write_text(content, encoding="utf-8")
            restored.append(str(full_path))

    if restored:
        print(f"[{agent_name}] Restored {len(restored)} files from Redis memory.")
    else:
        print(f"[{agent_name}] No memory found in Redis (fresh start).")
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
