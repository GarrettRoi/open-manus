# [Project name]

_Replace the heading above with the project's name, and this line with one sentence describing what this app does for users._

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

### `open-manus/` — separate Python system (not a pnpm artifact)

`open-manus/` at the project root holds the full "Open Manus" agent codebase, imported from the `deploy` branch of `GarrettRoi/open-manus` (a heavily customized fork of Nous Research's Hermes Agent). It is **not** part of the pnpm workspace — `pnpm-workspace.yaml` only globs `artifacts/*`, `lib/*`, `lib/integrations/*`, and `scripts`, so this directory is safe from pnpm tooling and lives independently.

- **Production stays on Railway.** This copy is for development/editing only; nothing here is wired up to run live yet.
- **Runtime:** Python 3.12 (module `python-3.12`, satisfies the project's `>=3.11,<3.14` requirement). Dependencies are installed into the project's shared `.pythonlibs` env via `uv sync` (from `pyproject.toml`/`uv.lock`) plus `uv pip install -r requirements.txt` (messaging/voice/misc extras: discord.py, python-telegram-bot, redis, stripe, elevenlabs, playwright, etc.).
- **To run commands:** `cd open-manus && python3.12 hermes <command>` (or `python3.12 cli.py`, `python3.12 run_agent.py`). Verified working: `hermes --version`, `hermes doctor`, `hermes --help`, `cli.py --help`, `run_agent.py --help`.
- **Two git submodules were intentionally NOT pulled in** (`mini-swe-agent`, `tinker-atropos`) — they're empty placeholder dirs. `run_agent.py` prints a harmless warning about a missing `tinker-atropos/logs` path on startup; this doesn't affect `--help` or core functionality.
- **Not yet configured (needed before any agent runs end-to-end):**
  - Per-agent Discord bot tokens for the 14 personas under `deploy/<agent>/` (addison, bianca, cora, harmony, jade, lexi, raven, sabrina, samantha, sasha, scarlett, tatiana, valentina, victoria)
  - An LLM provider API key (OpenRouter or similar)
  - A Redis connection URL (used for skill sync and cross-restart memory persistence) — get this via the environment-secrets flow when Redis work starts, not pasted in chat
  - Telegram/Slack/WhatsApp/Signal credentials, if those gateways are used
- Real secrets/config for this system should go through Replit's environment-secrets flow, not `open-manus/.env` (gitignored) or hardcoded values.

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

_Describe the high-level user-facing capabilities of this app once they exist._

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
