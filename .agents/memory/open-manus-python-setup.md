---
name: Open Manus Python setup in this pnpm monorepo
description: How the open-manus/ Python codebase coexists with the pnpm workspace, and how its deps get installed.
---

- `open-manus/` at project root is a standalone Python system (Hermes Agent fork), not a pnpm artifact. `pnpm-workspace.yaml` only globs `artifacts/*`, `lib/*`, `lib/integrations/*`, `scripts` — a root-level non-glob directory is safe from pnpm tooling by construction.
- Requires Python >=3.11,<3.14 (upper bound is load-bearing — some Rust-backed transitives have no cp314 wheel yet). Installed `python-3.12` module.
- `uv sync` / `uv pip install` in this Replit environment install into the **project-wide** `/home/runner/workspace/.pythonlibs` venv, not a per-subdirectory `.venv` — even when run from inside a subdirectory like `open-manus/`. Creating a local `.venv` there is unused/misleading; delete it to avoid confusion.
- This project's `pyproject.toml` splits deps into a small core `dependencies` list plus many `[project.optional-dependencies]` extras (messaging, voice, tts-premium, etc.) — core install via `uv sync` will NOT pull in discord.py/telegram/redis/stripe/elevenlabs/playwright. Those come from the separate checked-in `requirements.txt`, which must be installed too (`uv pip install -r requirements.txt`) to get a fully working environment.
- Two git submodules declared in `.gitmodules` (`mini-swe-agent`, `tinker-atropos`) were intentionally left uninitialized (empty dirs) for a code-only import — `run_agent.py` prints a harmless warning about a missing `tinker-atropos/logs` path on startup but still runs fine.
- **Why:** the user's production system stays on Railway; this Replit copy is for development only, and keeping it out of the pnpm workspace avoids polluting `pnpm install`/typecheck across the monorepo.
