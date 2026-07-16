---
name: Agent workspace sync
description: Design of the per-agent workspace <-> Redis sync and dashboard agent-files API.
---
Key layout (per agent name):
- `agent:{n}:wsync:file:{relpath}` — JSON {b64, hash(sha256), mtime, updated_at, source: agent|dashboard}
- `agent:{n}:wsync:deleted:{relpath}` — ISO tombstone (deletion propagation both ways)
- `agent:{n}:wsync:last_sync` — agent-side heartbeat shown in dashboard
- `agent:{n}:deployfile:{config.yaml|SOUL.md|USER.md}` — dashboard deploy edits pulled into /root/.hermes/ without redeploy

Rules to keep in lockstep between `skills/hive_mind/workspace_sync.py` and the `/api/agent-files/*` endpoints in `hermes_cli/web_server.py`:
- Conflict: if both sides changed, dashboard (`source=dashboard`) wins; otherwise local wins and re-pushes (clears tombstone).
- Limits: 5 MB/file, 100 MB/agent aggregate (API enforces via STRLEN scan; sync script via push budget).
- Exclusions: sensitive basenames/dirs, plus workspace-root files owned by redis_memory_sync (MEMORY.md, cron_jobs.json, tasks.json, notes.md) — the two syncs must never share a file.
- Deploy config.yaml is deliberately exempt from the managed-files sensitive denylist (persona configs, no credentials) — only within the agent-files deploy area allowlist of 3 filenames.

**Why:** dashboard edits must survive agent restarts and reach volume-less containers; two writers over one keyspace need a single documented conflict + quota rule.
**How to apply:** any change to key format, limits, or exclusions must be made in both the sync script and the web API, and bump/migrate existing keys if the JSON shape changes.

Gotcha: entrypoint.sh must NOT `exec` the gateway — EXIT trap (final memory+workspace flush) only runs when the gateway is a child and the script `wait`s.
