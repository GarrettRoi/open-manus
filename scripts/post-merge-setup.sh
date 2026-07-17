#!/bin/bash
# Post-merge setup: keep the environment in sync after task merges.
# Idempotent, non-interactive, fast.
set -e

cd "$(dirname "$0")/.."

PY=".pythonlibs/bin/python3"
[ -x "$PY" ] || PY="python3"

# Python deps (quiet, no prompts; pip skips already-satisfied packages)
if [ -f requirements.txt ]; then
  "$PY" -m pip install -q -r requirements.txt --disable-pip-version-check --no-input || true
fi

# Dashboard frontend deps (only if node_modules is missing or lockfile changed)
if [ -f web/package.json ]; then
  export PATH="/nix/store/1lagpgadaybvs1n2312gysg2phjk89y8-nodejs-20.20.0-wrapped/bin:$PATH"
  if command -v npm >/dev/null 2>&1; then
    if [ ! -d web/node_modules ] || [ web/package-lock.json -nt web/node_modules ]; then
      (cd web && npm install --no-audit --no-fund) || true
    fi
  fi
fi

echo "post-merge setup complete"
