#!/usr/bin/env python3
"""Agent-side vault backup loop.

Runs inside an agent container that has a persistent Railway volume
(e.g. agent-lexi), dumping all `vault:*` Redis keys to disk on a schedule.
This is the durable copy: the vault service itself has no volume, so its
own backups don't survive a redeploy.

Enabled by setting VAULT_BACKUP_ENABLED=1 on the agent service; started
from entrypoint.sh. Values stay Fernet-encrypted exactly as in Redis —
useless without the VAULT_MASTER_KEY env var held by the vault service.

Usage:
    python3 scripts/vault_backup_agent.py [--once]

Env:
    REDIS_URL                    (required)
    VAULT_BACKUP_DIR             default /root/.hermes/workspace/vault-backups
    VAULT_BACKUP_INTERVAL        seconds between backups, default 86400
    VAULT_BACKUP_KEEP            backups to retain, default 14
"""

import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s vault-backup %(levelname)s %(message)s")
logger = logging.getLogger("vault-backup")

BACKUP_DIR = os.getenv("VAULT_BACKUP_DIR", "/root/.hermes/workspace/vault-backups")
INTERVAL = int(os.getenv("VAULT_BACKUP_INTERVAL", str(24 * 3600)))
KEEP = int(os.getenv("VAULT_BACKUP_KEEP", "14"))


def backup_once(r) -> str:
    entries = {}
    for key in r.scan_iter(b"vault:*", count=1000):
        payload = r.dump(key)
        if payload is None:
            continue
        entries[key.decode()] = {
            "dump_b64": base64.b64encode(payload).decode(),
            "pttl": max(r.pttl(key), 0),
        }
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"vault-backup-{ts}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"created_at": ts, "keys": entries}, f)
    os.replace(tmp, path)
    # prune
    files = sorted(f for f in os.listdir(BACKUP_DIR)
                   if f.startswith("vault-backup-") and f.endswith(".json"))
    for old in files[:-KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    logger.info("wrote %s (%d keys)", path, len(entries))
    return path


def main():
    url = os.getenv("REDIS_URL")
    if not url:
        logger.error("REDIS_URL not set; exiting")
        sys.exit(1)
    r = redis.from_url(url, decode_responses=False)
    once = "--once" in sys.argv
    while True:
        try:
            backup_once(r)
        except Exception:
            logger.exception("vault backup failed")
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
