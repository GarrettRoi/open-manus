#!/bin/bash
# ============================================================
# Open Manus — Multi-Agent Entrypoint
# Selects the correct agent config based on AGENT_NAME env var
# and starts the Hermes gateway with Discord integration.
# ============================================================

set -e

AGENT_NAME="${AGENT_NAME:-harmony}"
AGENT_DIR="/app/deploy/${AGENT_NAME}"

echo "============================================"
echo " Starting Agent: ${AGENT_NAME}"
echo " Config Dir:     ${AGENT_DIR}"
echo "============================================"

# Ensure the hermes config directory exists
mkdir -p /root/.hermes/workspace /root/.hermes/skills /root/.hermes/memory /root/.hermes/sessions

# Copy agent-specific config files
if [ -f "${AGENT_DIR}/config.yaml" ]; then
    cp "${AGENT_DIR}/config.yaml" /root/.hermes/config.yaml
    echo "[entrypoint] Loaded config.yaml for ${AGENT_NAME}"
else
    echo "[entrypoint] ERROR: No config.yaml found for agent '${AGENT_NAME}'"
    exit 1
fi

if [ -f "${AGENT_DIR}/SOUL.md" ]; then
    cp "${AGENT_DIR}/SOUL.md" /root/.hermes/SOUL.md
    echo "[entrypoint] Loaded SOUL.md for ${AGENT_NAME}"
fi

if [ -f "${AGENT_DIR}/USER.md" ]; then
    cp "${AGENT_DIR}/USER.md" /root/.hermes/USER.md
    echo "[entrypoint] Loaded USER.md for ${AGENT_NAME}"
fi

# Copy shared skills
cp -r /app/skills/* /root/.hermes/skills/ 2>/dev/null || true

# Pull latest dynamic skills from Redis Skill Store
if [ -n "$REDIS_URL" ]; then
    echo "[entrypoint] Syncing dynamic skills from Redis Skill Store..."
    python3 /app/skills/hive_mind/skill_sync.py --action pull --target /root/.hermes/skills/ || true
fi

# ============================================================
# PERSISTENT MEMORY RESTORE
# Restores memory files from Redis on startup.
# For agents WITH a Railway volume, this is a no-op if files already exist.
# For agents WITHOUT a volume, this restores from the last Redis save.
# ============================================================
if [ -n "$REDIS_URL" ]; then
    echo "[entrypoint] Restoring persistent memory for ${AGENT_NAME}..."
    python3 /app/skills/hive_mind/redis_memory_sync.py --action restore --agent "${AGENT_NAME}" || true
fi

# ============================================================
# WORKSPACE RESTORE + SYNC
# Mirrors the agent's real working directory (/root/.hermes/workspace)
# to Redis so it's browsable/editable from the dashboard and survives
# restarts. Also pulls dashboard-edited deploy files (config.yaml,
# SOUL.md, USER.md) down without needing a redeploy.
# ============================================================
if [ -n "$REDIS_URL" ]; then
    echo "[entrypoint] Restoring workspace for ${AGENT_NAME}..."
    python3 /app/skills/hive_mind/workspace_sync.py --action restore --agent "${AGENT_NAME}" || true
fi

# Start background memory auto-save (every 5 minutes)
if [ -n "$REDIS_URL" ]; then
    echo "[entrypoint] Starting background memory auto-save..."
    python3 /app/skills/hive_mind/redis_memory_sync.py --action watch --agent "${AGENT_NAME}" --interval 300 &
    MEMORY_SYNC_PID=$!
    echo "[entrypoint] Memory sync PID: ${MEMORY_SYNC_PID}"
    echo "[entrypoint] Starting background workspace sync..."
    python3 /app/skills/hive_mind/workspace_sync.py --action watch --agent "${AGENT_NAME}" --interval 300 &
    WORKSPACE_SYNC_PID=$!
    echo "[entrypoint] Workspace sync PID: ${WORKSPACE_SYNC_PID}"
fi

# Nightly vault backup to this agent's persistent volume (enabled per-service
# via VAULT_BACKUP_ENABLED=1 — lexi only; protects vault data from Redis loss)
if [ "$VAULT_BACKUP_ENABLED" = "1" ] && [ -n "$REDIS_URL" ]; then
    echo "[entrypoint] Starting vault backup loop..."
    python3 /app/scripts/vault_backup_agent.py &
    VAULT_BACKUP_PID=$!
    echo "[entrypoint] Vault backup PID: ${VAULT_BACKUP_PID}"
fi

# Graceful shutdown handler — save memory before exit
cleanup() {
    echo "[entrypoint] Shutting down ${AGENT_NAME}..."
    if [ -n "$REDIS_URL" ]; then
        echo "[entrypoint] Saving memory to Redis before shutdown..."
        python3 /app/skills/hive_mind/redis_memory_sync.py --action save --agent "${AGENT_NAME}" || true
        echo "[entrypoint] Saving workspace to Redis before shutdown..."
        python3 /app/skills/hive_mind/workspace_sync.py --action sync --agent "${AGENT_NAME}" || true
    fi
    if [ -n "$MEMORY_SYNC_PID" ]; then
        kill "$MEMORY_SYNC_PID" 2>/dev/null || true
    fi
    if [ -n "$WORKSPACE_SYNC_PID" ]; then
        kill "$WORKSPACE_SYNC_PID" 2>/dev/null || true
    fi
    if [ -n "$VAULT_BACKUP_PID" ]; then
        kill "$VAULT_BACKUP_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT SIGTERM SIGINT

# Dynamically write environment variables to .env
# Excludes Railway internal variables (RAILWAY_*) and transient shell vars
echo "[entrypoint] Writing environment variables to .env..."
: > /root/.hermes/.env
for varname in $(compgen -e); do
    # Skip Railway internal variables
    [[ "$varname" == RAILWAY_* ]] && continue
    # Skip transient shell variables
    case "$varname" in
        PWD|OLDPWD|SHLVL|_|HOSTNAME) continue ;;
    esac
    printf '%s=%s\n' "$varname" "${!varname}" >> /root/.hermes/.env
done
echo "[entrypoint] Wrote $(wc -l < /root/.hermes/.env) variables to .env"

echo "[entrypoint] Environment configured. Starting Hermes gateway..."

# Optionally start the web dashboard (pre-built into the image). Guarded by
# HERMES_DASHBOARD; on a non-loopback bind the dashboard's auth gate requires
# HERMES_DASHBOARD_BASIC_AUTH_USERNAME/_PASSWORD (or OAuth) and fails closed
# without them.
case "${HERMES_DASHBOARD:-}" in
    1|true|TRUE|True|yes|YES|Yes)
        dash_host="${HERMES_DASHBOARD_HOST:-0.0.0.0}"
        dash_port="${HERMES_DASHBOARD_PORT:-9119}"
        echo "[entrypoint] Starting dashboard on ${dash_host}:${dash_port}..."
        (cd /app && hermes dashboard --skip-build --no-open \
            --host "$dash_host" --port "$dash_port" \
            >> /tmp/dashboard.log 2>&1) &
        DASHBOARD_PID=$!
        ;;
esac

# Start the Hermes gateway as a child (NOT exec) so the EXIT/SIGTERM trap
# still runs and can flush memory + workspace state to Redis on shutdown.
cd /app
hermes gateway &
GATEWAY_PID=$!

# Forward termination signals to the gateway, then let the trap do cleanup.
forward_signal() {
    kill -TERM "$GATEWAY_PID" 2>/dev/null || true
}
trap forward_signal SIGTERM SIGINT

wait "$GATEWAY_PID"
GATEWAY_EXIT=$?
exit "$GATEWAY_EXIT"
