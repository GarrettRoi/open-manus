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

# Write the .env file for Hermes from environment variables
cat > /root/.hermes/.env << EOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
OPENAI_API_KEY=${OPENAI_API_KEY:-}
ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY:-}
DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN:-}
DISCORD_HOME_CHANNEL=${DISCORD_HOME_CHANNEL:-}
DISCORD_REQUIRE_MENTION=${DISCORD_REQUIRE_MENTION:-true}
DISCORD_ALLOW_BOTS=${DISCORD_ALLOW_BOTS:-mentions}
AGENT_DISCORD_IDS=${AGENT_DISCORD_IDS:-}
DISCORD_FREE_RESPONSE_CHANNELS=${DISCORD_FREE_RESPONSE_CHANNELS:-}
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-}
FRED_API=${FRED_API:-}
MASSIVE_API=${MASSIVE_API:-}
N8N_INSTANCE_URL=${N8N_INSTANCE_URL:-}
N8N_API_KEY=${N8N_API_KEY:-}
DOCUMENSO_API=${DOCUMENSO_API:-}
REDIS_URL=${REDIS_URL:-}
GATEWAY_ALLOW_ALL_USERS=${GATEWAY_ALLOW_ALL_USERS:-true}
RAILWAY_TOKEN=${RAILWAY_TOKEN:-}
RAILWAY_ACCOUNT_API=${RAILWAY_TOKEN:-}
QUO_API_KEY=${QUO_API_KEY:-}
GH_TOKEN=${GH_TOKEN:-}
HARMONY_BOT_ID=${HARMONY_BOT_ID:-1481029359757299922}
HARMONY_CHANNEL_ID=${HARMONY_CHANNEL_ID:-1483488835928064110}
TASK_BOARD_CHANNEL_ID=${TASK_BOARD_CHANNEL_ID:-1481406227153031372}
EOF

echo "[entrypoint] Environment configured. Starting Hermes gateway..."

# Start the Hermes gateway in foreground mode
cd /app
exec hermes gateway
