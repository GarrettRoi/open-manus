#!/usr/bin/env python3
"""
Workspace Sync — Bidirectional sync of an agent's real working directory
(/root/.hermes/workspace, i.e. terminal.cwd) with Redis, so the dashboard can
browse/edit each agent's working folder and the folder survives container
restarts even without a Railway volume.

Also pulls dashboard-edited deploy files (config.yaml / SOUL.md / USER.md)
from Redis down into /root/.hermes/ so running agents pick them up on next
reload without a redeploy.

Redis keys (per agent):
    agent:{name}:wsync:file:{relpath}   JSON {b64, hash, mtime, updated_at, source}
    agent:{name}:wsync:deleted:{relpath}  ISO timestamp tombstone
    agent:{name}:wsync:last_sync        ISO timestamp of last agent-side sync
    agent:{name}:deployfile:{filename}  JSON {content, hash, updated_at}

Shared folder (one copy for ALL agents + the dashboard):
    Files under workspace/shared/ map to global keys instead:
    shared:wsync:file:{relpath}     same JSON shape; source = agent name or "dashboard"
    shared:wsync:deleted:{relpath}  tombstone
    Every agent pulls the shared folder on sync/restore; any agent (or the
    dashboard) adding/editing/deleting a file there propagates to everyone.
    Conflicts on shared files resolve in favor of the remote (last writer wins).

Usage:
    python3 workspace_sync.py --action restore --agent lexi   # on boot
    python3 workspace_sync.py --action sync    --agent lexi   # one push+pull round
    python3 workspace_sync.py --action watch   --agent lexi --interval 300
"""
import argparse
import base64
import fnmatch
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
WORKSPACE_DIR = Path(os.getenv("HERMES_WORKSPACE_DIR", "/root/.hermes/workspace"))
HERMES_HOME = Path(os.getenv("HERMES_HOME", "/root/.hermes"))
STATE_FILE = HERMES_HOME / ".wsync_state.json"

MAX_FILE_SIZE = 5 * 1024 * 1024          # 5 MB per file
MAX_TOTAL_SIZE = 100 * 1024 * 1024       # 100 MB per agent

DEPLOY_FILES = ("config.yaml", "SOUL.md", "USER.md")

# workspace/shared/ is a single folder shared by all agents + the dashboard.
SHARED_PREFIX = "shared/"

# Files redis_memory_sync.py already persists at the workspace root — leave
# them to that script so the two syncs never fight over the same file.
# Note: cron_jobs.json here refers to the LEGACY workspace copy
# (workspace/cron_jobs.json). The active cron job store is
# cron/jobs.json (persisted via redis_memory_sync.py PERSIST_FILES).
MEMORY_SYNC_OWNED = {"MEMORY.md", "cron_jobs.json", "tasks.json", "notes.md"}

# Never sync: credentials, transient/tool dirs, sync state.
EXCLUDE_BASENAME_GLOBS = (
    ".env", ".env.*", ".envrc", "*.pyc", ".DS_Store",
    "auth.json", "auth.lock", "credentials", ".git-credentials",
    ".anthropic_oauth.json", "google_token.json", "google_oauth.json",
    "google_oauth_pending.json", "webhook_subscriptions.json", "bws_cache.json",
    ".sync_state.json", ".wsync_state.json", "*.upload",
)
EXCLUDE_DIR_NAMES = {
    ".git", ".hg", ".svn", ".cache", "__pycache__", "node_modules",
    ".venv", "venv", "mcp-tokens", "pairing", ".npm", ".local",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXCLUDE_DIR_NAMES for p in parts[:-1]):
        return True
    base = parts[-1]
    if len(parts) == 1 and base in MEMORY_SYNC_OWNED:
        return True
    return any(fnmatch.fnmatch(base.lower(), pat) for pat in EXCLUDE_BASENAME_GLOBS)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _tomb_blocks_pull(r, agent: str, rel: str, remote: dict) -> bool:
    """True if a tombstone newer than the remote file exists (deletion wins).
    Clears the tombstone when the file copy is newer (stale tombstone)."""
    tomb_key = _tomb(agent, rel)
    tomb_raw = r.get(tomb_key)
    if not tomb_raw:
        return False
    tomb_ts = _parse_ts(tomb_raw)
    file_ts = _parse_ts(remote.get("updated_at"))
    if tomb_ts is None:
        r.delete(tomb_key)
        return False
    if file_ts is None or tomb_ts >= file_ts:
        return True
    r.delete(tomb_key)  # file was re-created after the deletion — tomb is stale
    return False


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"files": {}, "deploy": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def walk_workspace() -> dict:
    """Return {relpath: Path} of syncable local files."""
    out = {}
    if not WORKSPACE_DIR.exists():
        return out
    for root, dirs, files in os.walk(WORKSPACE_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
        for name in files:
            p = Path(root) / name
            try:
                rel = str(p.relative_to(WORKSPACE_DIR)).replace(os.sep, "/")
            except ValueError:
                continue
            if is_excluded(rel):
                continue
            try:
                if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            out[rel] = p
    return out


def _key(agent: str, rel: str) -> str:
    if rel.startswith(SHARED_PREFIX):
        return f"shared:wsync:file:{rel[len(SHARED_PREFIX):]}"
    return f"agent:{agent}:wsync:file:{rel}"


def _tomb(agent: str, rel: str) -> str:
    if rel.startswith(SHARED_PREFIX):
        return f"shared:wsync:deleted:{rel[len(SHARED_PREFIX):]}"
    return f"agent:{agent}:wsync:deleted:{rel}"


def push_file(r, agent: str, rel: str, path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    h = file_hash(data)
    r.set(_key(agent, rel), json.dumps({
        "b64": base64.b64encode(data).decode("ascii"),
        "hash": h,
        "mtime": path.stat().st_mtime,
        "updated_at": utcnow(),
        "source": agent if rel.startswith(SHARED_PREFIX) else "agent",
    }))
    r.delete(_tomb(agent, rel))
    return h


def pull_file(agent: str, rel: str, remote: dict) -> bool:
    target = WORKSPACE_DIR / rel
    if is_excluded(rel):
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(remote["b64"]))
        return True
    except (OSError, KeyError, ValueError) as e:
        print(f"[wsync] pull failed for {rel}: {e}")
        return False


def remote_index(r, agent: str) -> dict:
    """{relpath: redis_key} — values loaded lazily."""
    prefix = f"agent:{agent}:wsync:file:"
    out = {}
    for key in r.scan_iter(f"{prefix}*"):
        out[key[len(prefix):]] = key
    shared_prefix = "shared:wsync:file:"
    for key in r.scan_iter(f"{shared_prefix}*"):
        out[SHARED_PREFIX + key[len(shared_prefix):]] = key
    return out


def migrate_legacy_shared(r, agent: str):
    """Move any pre-shared-folder keys (agent:{name}:wsync:file:shared/*) to
    the global shared:* namespace so old mirrors don't shadow the shared view."""
    prefix = f"agent:{agent}:wsync:file:{SHARED_PREFIX}"
    for key in list(r.scan_iter(f"{prefix}*")):
        rel = key[len(prefix):]
        skey = f"shared:wsync:file:{rel}"
        val = r.get(key)
        if val is not None:
            existing = r.get(skey)
            if existing is None:
                r.set(skey, val)
            else:
                # both a legacy and a global copy exist — keep the newer one
                try:
                    new_ts = _parse_ts(json.loads(val).get("updated_at"))
                    old_ts = _parse_ts(json.loads(existing).get("updated_at"))
                except ValueError:
                    new_ts = old_ts = None
                if new_ts is not None and (old_ts is None or new_ts > old_ts):
                    r.set(skey, val)
        r.delete(key)
    tomb_prefix = f"agent:{agent}:wsync:deleted:{SHARED_PREFIX}"
    for key in list(r.scan_iter(f"{tomb_prefix}*")):
        r.delete(key)


def sync(agent: str, verbose: bool = True):
    r = get_redis()
    state = load_state()
    fstate = state.setdefault("files", {})
    migrate_legacy_shared(r, agent)
    local = walk_workspace()
    remote_keys = remote_index(r, agent)

    pushed = pulled = deleted = 0
    total_budget = MAX_TOTAL_SIZE

    # ---- local scan: pushes and local deletions ----
    for rel, path in local.items():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        total_budget -= len(data)
        if total_budget < 0:
            print(f"[wsync] total size cap reached; skipping {rel} and beyond")
            break
        lh = file_hash(data)
        if fstate.get(rel) != lh:
            # local changed (or new) since last sync
            remote_raw = r.get(remote_keys[rel]) if rel in remote_keys else None
            remote = json.loads(remote_raw) if remote_raw else None
            if remote and remote.get("hash") != fstate.get(rel) and (
                remote.get("source") == "dashboard" or rel.startswith(SHARED_PREFIX)
            ):
                # both changed; dashboard edit wins (shared files: remote/last
                # writer wins so all agents converge on one copy)
                if pull_file(agent, rel, remote):
                    fstate[rel] = remote["hash"]
                    pulled += 1
                continue
            h = push_file(r, agent, rel, path)
            if h:
                fstate[rel] = h
                pushed += 1

    # files we knew about that vanished locally -> agent deleted them
    for rel in [x for x in list(fstate) if x not in local]:
        if rel.startswith("__"):
            continue
        if rel in remote_keys:
            raw = r.get(remote_keys[rel])
            remote = None
            if raw:
                try:
                    remote = json.loads(raw)
                except ValueError:
                    remote = None
            if remote and remote.get("hash") != fstate.get(rel):
                # someone else (another agent / the dashboard) updated this
                # file since our last sync — their write wins over our stale
                # deletion; forget it so the remote scan pulls it back down.
                del fstate[rel]
                continue
            r.delete(remote_keys[rel])
        r.set(_tomb(agent, rel), utcnow())
        del fstate[rel]
        deleted += 1

    # ---- remote scan: pulls and dashboard deletions ----
    for rel, key in remote_keys.items():
        if rel in local and fstate.get(rel):
            # compare remote hash to state; pull if dashboard changed it
            raw = r.get(key)
            if not raw:
                continue
            remote = json.loads(raw)
            if remote.get("hash") != fstate.get(rel):
                lh = None
                try:
                    lh = file_hash((WORKSPACE_DIR / rel).read_bytes())
                except OSError:
                    pass
                if lh == fstate.get(rel):  # local unchanged -> safe pull
                    if pull_file(agent, rel, remote):
                        fstate[rel] = remote["hash"]
                        pulled += 1
        elif rel not in local:
            raw = r.get(key)
            if not raw:
                continue
            remote = json.loads(raw)
            if rel in fstate:
                continue  # handled above as local deletion
            if _tomb_blocks_pull(r, agent, rel, remote):
                continue  # deleted more recently than it was written
            if pull_file(agent, rel, remote):
                fstate[rel] = remote["hash"]
                pulled += 1

    # remote deletions (tombstones for files we still have) — per-agent
    # tombstones from the dashboard, plus shared-folder tombstones from
    # anyone (dashboard or another agent).
    tomb_scan = [
        (f"agent:{agent}:wsync:deleted:", ""),
        ("shared:wsync:deleted:", SHARED_PREFIX),
    ]
    for tomb_prefix, rel_prefix in tomb_scan:
      for key in r.scan_iter(f"{tomb_prefix}*"):
        rel = rel_prefix + key[len(tomb_prefix):]
        # stale tombstone? if the file key exists and is newer, the
        # re-creation wins — drop the tombstone and keep the file.
        file_raw = r.get(_key(agent, rel))
        if file_raw:
            try:
                remote = json.loads(file_raw)
            except ValueError:
                remote = {}
            tomb_ts = _parse_ts(r.get(key))
            file_ts = _parse_ts(remote.get("updated_at"))
            if tomb_ts is None or (file_ts is not None and file_ts > tomb_ts):
                r.delete(key)
                continue
        target = WORKSPACE_DIR / rel
        if target.exists() and fstate.get(rel):
            try:
                lh = file_hash(target.read_bytes())
            except OSError:
                continue
            if lh == fstate.get(rel):  # unchanged since sync -> honor deletion
                try:
                    target.unlink()
                except OSError:
                    continue
                fstate.pop(rel, None)
                deleted += 1

    # ---- deploy-file overrides from dashboard ----
    dstate = state.setdefault("deploy", {})
    for fname in DEPLOY_FILES:
        raw = r.get(f"agent:{agent}:deployfile:{fname}")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if payload.get("hash") and payload["hash"] != dstate.get(fname):
            try:
                (HERMES_HOME / fname).write_text(payload.get("content", ""), encoding="utf-8")
                dstate[fname] = payload["hash"]
                print(f"[wsync] applied dashboard {fname} update")
            except OSError as e:
                print(f"[wsync] could not apply {fname}: {e}")

    r.set(f"agent:{agent}:wsync:last_sync", utcnow())
    save_state(state)
    if verbose:
        print(f"[wsync:{agent}] pushed={pushed} pulled={pulled} deleted={deleted}")


def restore(agent: str):
    """Boot-time restore: pull the whole workspace mirror from Redis."""
    r = get_redis()
    state = load_state()
    fstate = state.setdefault("files", {})
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    restored = 0
    for rel, key in remote_index(r, agent).items():
        target = WORKSPACE_DIR / rel
        if target.exists():
            continue  # volume-backed agents keep their local copy
        raw = r.get(key)
        if not raw:
            continue
        try:
            remote = json.loads(raw)
        except ValueError:
            continue
        if _tomb_blocks_pull(r, agent, rel, remote):
            continue
        if pull_file(agent, rel, remote):
            fstate[rel] = remote.get("hash")
            restored += 1
    save_state(state)
    print(f"[wsync:{agent}] restored {restored} workspace files from Redis.")
    # apply any pending dashboard deploy-file edits too
    sync(agent, verbose=False)


def watch(agent: str, interval: int):
    print(f"[wsync:{agent}] watching (sync every {interval}s)...")
    while True:
        try:
            sync(agent)
        except Exception as e:
            print(f"[wsync:{agent}] sync error: {e}")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="Agent workspace <-> Redis sync")
    ap.add_argument("--action", choices=["restore", "sync", "watch"], required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--interval", type=int, default=300)
    args = ap.parse_args()
    if args.action == "restore":
        restore(args.agent)
    elif args.action == "sync":
        sync(args.agent)
    else:
        watch(args.agent, args.interval)


if __name__ == "__main__":
    main()
