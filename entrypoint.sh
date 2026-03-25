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

# Start the Hermes gateway in foreground mode
cd /app
exec hermes gateway
