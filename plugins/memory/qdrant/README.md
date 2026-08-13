# Qdrant Fleet Memory

Long-term vector memory for the Open Manus fleet. Facts are distilled from
conversations by a cheap auxiliary LLM, embedded (OpenAI
`text-embedding-3-small` by default), and stored in one shared Qdrant
collection (`fleet_memory`) with per-agent isolation:

- `scope: agent` — private to the writing agent (default)
- `scope: shared` — visible fleet-wide (client identity, owner policies)

Reads always see: own agent-scoped points + all shared points, excluding
superseded ones, re-ranked by similarity × recency (90-day half-life) ×
importance.

## Write paths
- **Per-turn sync** — every completed turn is queued for background LLM fact
  extraction + dedup (near-duplicate similarity ≥ 0.90 is skipped).
- **Pre-compression hook** — messages about to be discarded by context
  compression are extracted first, so detail survives compaction.
- **Session end** — final extraction pass over the whole conversation.
- **`memory_bank` tool** — explicit verbatim store (synchronous, with dedup
  feedback).

## Read paths
- **Automatic prefetch** — background recall on each turn, injected as
  fenced memory context (max ~1.5 s wait; skipped when slow).
- **`memory_recall` tool** — explicit multi-query search.
- **`memory_forget` tool** — delete wrong/obsolete entries.

Cron and subagent contexts are read-only (no memory pollution).

## Enabling an agent
1. Ensure the service has env vars (fleet-provisioned): `QDRANT_URL`,
   `QDRANT_API_KEY`, plus `OPENAI_API_KEY` (embeddings).
2. In `deploy/<agent>/config.yaml` add under `memory:`:
   ```yaml
   memory:
     enabled: true
     backend: core
     auto_capture: true
     provider: qdrant
   ```
3. Push to `deploy`; verify startup log line
   `Memory provider 'qdrant' registered (3 tools)`.

Pilot agents: samantha, bianca, lexi. Fleet-wide flip-on = repeat step 2 per
agent (env vars are already fleet-wide via `scripts/provision_env_vars.py`).

## Ops
- Qdrant runs as the `qdrant` Railway service (internal URL
  `http://qdrant.railway.internal:6333`, API key in service env
  `QDRANT__SERVICE__API_KEY`). Storage should be on a Railway volume at
  `/qdrant/storage`.
- Backup: Qdrant snapshot API — `POST /collections/fleet_memory/snapshots`
  then download via `GET /collections/fleet_memory/snapshots/{name}`.
- Failure monitoring: agent logs — grep `qdrant memory:` (store failures,
  circuit breaker trips are WARNING level).
- Everything is fail-soft: if Qdrant or embeddings are down, agents keep
  answering; a circuit breaker (5 failures → 120 s pause) stops hammering.
