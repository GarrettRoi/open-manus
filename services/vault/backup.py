"""Vault Redis backup & restore.

Dumps every `vault:*` key (binary DUMP payload, base64-encoded, with TTL)
to a JSON file on local disk (a Railway volume in production), so a Redis
wipe becomes a bounded setback instead of total loss.

Values are stored exactly as they live in Redis — i.e. still Fernet-encrypted
where the vault encrypts them.  A backup file alone is useless without the
master key (which lives in the VAULT_MASTER_KEY env var, not in Redis).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("vault.backup")

BACKUP_DIR = os.getenv("VAULT_BACKUP_DIR", "/backups")
KEEP_BACKUPS = int(os.getenv("VAULT_BACKUP_KEEP", "14"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("VAULT_BACKUP_INTERVAL", str(24 * 3600)))


def _raw_client(r):
    """Return a non-decoding client on the same connection pool (DUMP is binary)."""
    import redis as redis_mod
    return redis_mod.Redis(
        connection_pool=redis_mod.ConnectionPool(
            connection_class=r.connection_pool.connection_class,
            **{**r.connection_pool.connection_kwargs, "decode_responses": False},
        )
    )


def backup_now(r, backup_dir: str = BACKUP_DIR) -> dict:
    """Dump all vault:* keys to a timestamped JSON file. Returns a summary."""
    raw = _raw_client(r)
    entries = {}
    for key in raw.scan_iter(b"vault:*", count=1000):
        # Never include the encryption master key: a backup file must not be
        # sufficient to decrypt the credentials it contains.
        if key == b"vault:master_key":
            continue
        payload = raw.dump(key)
        if payload is None:
            continue
        entries[key.decode()] = {
            "dump_b64": base64.b64encode(payload).decode(),
            "pttl": max(raw.pttl(key), 0),  # 0 = no expiry
        }
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, f"vault-backup-{ts}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"created_at": ts, "keys": entries}, f)
    os.replace(tmp, path)
    _prune(backup_dir)
    logger.info("Backup written: %s (%d keys)", path, len(entries))
    return {"file": os.path.basename(path), "keys": len(entries)}


def _prune(backup_dir: str, keep: int = KEEP_BACKUPS):
    files = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("vault-backup-") and f.endswith(".json")
    )
    for old in files[:-keep]:
        try:
            os.remove(os.path.join(backup_dir, old))
        except OSError:
            pass


def list_backups(backup_dir: str = BACKUP_DIR) -> list:
    if not os.path.isdir(backup_dir):
        return []
    out = []
    for f in sorted(os.listdir(backup_dir), reverse=True):
        if f.startswith("vault-backup-") and f.endswith(".json"):
            p = os.path.join(backup_dir, f)
            out.append({"file": f, "size": os.path.getsize(p)})
    return out


def restore_from_file(r, filename: str, backup_dir: str = BACKUP_DIR,
                      overwrite: bool = False) -> dict:
    """Restore keys from a backup file. Skips existing keys unless overwrite."""
    if "/" in filename or ".." in filename:
        raise ValueError("invalid filename")
    path = os.path.join(backup_dir, filename)
    with open(path) as f:
        data = json.load(f)
    raw = _raw_client(r)
    restored = skipped = 0
    for key, entry in data["keys"].items():
        if key == "vault:master_key":
            continue  # defense in depth — never restore a master key from file
        payload = base64.b64decode(entry["dump_b64"])
        try:
            raw.restore(key.encode(), entry.get("pttl", 0), payload,
                        replace=overwrite)
            restored += 1
        except Exception as exc:
            if "BUSYKEY" in str(exc):
                skipped += 1
            else:
                raise
    logger.info("Restore from %s: %d restored, %d skipped", filename, restored, skipped)
    return {"restored": restored, "skipped_existing": skipped}


async def backup_loop(r):
    """Run backup_now every BACKUP_INTERVAL_SECONDS forever (call from startup).

    Backup I/O runs in a worker thread so it never blocks the event loop.
    """
    import asyncio
    while True:
        try:
            await asyncio.to_thread(backup_now, r)
        except Exception:
            logger.exception("Scheduled vault backup failed")
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
