# Open Manus — 12-Agent Autonomous Team

A multi-agent AI system built on [Hermes-Agent](https://github.com/NousResearch/hermes-agent) by Nous Research. Each agent runs as an independent Discord bot with specialized capabilities, communicating through a shared Redis message bus and coordinated by a central orchestrator.

## Architecture

The system consists of 12 specialized AI agents, each running as a separate Railway service with its own Discord bot interface. All agents share a common codebase (Hermes-Agent) but are configured with unique personas, tool sets, and LLM routing strategies.

| Agent | Role | Default Model | Focus Area |
|-------|------|---------------|------------|
| **Harmony** | Orchestrator | grok-4 | Task routing, project management, cross-agent coordination |
| **Samantha** | Admin | gemini-2.5-pro | Scheduling, email, document management, calendar |
| **Raven** | Research | kimi-k2.5 | Market research, competitive intelligence, data analysis |
| **Scarlet** | Client Relations | kimi-k2.5 | Client communications, follow-ups, relationship nurturing |
| **Bianca** | Investments | grok-4 | Stock analysis, crypto, portfolio tracking |
| **Valentina** | Development | kimi-k2.5 | Automation, n8n workflows, website management, APIs |
| **Sasha** | Sales | kimi-k2.5 | Lead qualification, outreach, proposals, closing |
| **Jade** | DJ Business | kimi-k2.5 | Vows & Vinyl DJ Co. operations and growth |
| **Tatiana** | Real Estate | grok-4 | McGarry Homes transaction coordination |
| **Sabrina** | Social Media | kimi-k2.5 | Organic social content, community management |
| **Addison** | Advertising | grok-4 | Paid ads (Meta, Google, YouTube, Reddit) |
| **Cora** | Media Creation | gemini-2.5-pro | Graphics, video, audio, print materials |

## Deployment

Each agent runs in its own Railway service container. The `AGENT_NAME` environment variable determines which agent configuration is loaded at startup.

### Environment Variables (per service)

```
AGENT_NAME=harmony          # Which agent to run
DISCORD_BOT_TOKEN=...       # Unique Discord bot token
OPENROUTER_API_KEY=...      # Shared OpenRouter key
REDIS_URL=...               # Shared Redis URL
```

### Directory Structure

```
deploy/
  harmony/
    config.yaml    # Hermes config with LLM settings
    SOUL.md        # Agent persona and instructions
    USER.md        # User context (shared across all agents)
  samantha/
    ...
  (12 agent directories total)
  shared/
    USER.md           # Master user profile
    model_router.py   # Dynamic LLM routing utility
skills/
  inter_agent_comm/   # Redis-based inter-agent messaging
entrypoint.sh         # Selects agent config and starts Hermes
```

### How It Works

1. Railway starts a container with `AGENT_NAME=jade` (for example)
2. `entrypoint.sh` copies `deploy/jade/config.yaml`, `SOUL.md`, and `USER.md` into the Hermes config directory
3. Environment variables (Discord token, API keys) are written to `.env`
4. `hermes gateway` starts, connecting to Discord as the Jade bot
5. Jade can communicate with other agents via the shared Redis message bus

## LLM Routing

Each agent has a default model, but specific task types are routed to specialized models for optimal cost and performance:

| Task Type | Model | Cost Tier |
|-----------|-------|-----------|
| Default / general | moonshotai/kimi-k2.5 | Low |
| Web search | qwen/qwen3.5-397b-a17b | Medium |
| Code generation | moonshotai/kimi-k2.5 | Low |
| Software engineering | minimax/minimax-m2.5 | Medium |
| Finance / orchestration | x-ai/grok-4 | Medium |
| Multimodal / vision | google/gemini-2.5-pro | Medium |
| Background / cron | google/gemini-2.5-flash | Very Low |
| Escalation | anthropic/claude-opus-4.5 | High |

## Inter-Agent Communication

Agents communicate through Redis queues. Each agent has an inbox at `agent:{name}:inbox`. Messages include type, priority, sender, and context.

```bash
# Send a task to another agent
python3 skills/inter_agent_comm/send_task.py \
  --to harmony --from jade --type task_request \
  --message "Need social media graphics for spring wedding promo"

# Check inbox
python3 skills/inter_agent_comm/check_inbox.py --agent harmony
```

## Business Context

This system manages three interconnected businesses:

1. **Vows & Vinyl DJ Co.** — Catholic wedding DJ service in Oklahoma
2. **McGarry Homes** — Catholic-oriented real estate brokerage
3. **Cana Wedding Collective** — Catholic wedding vendor marketing platform

The businesses feed each other in a flywheel: Cana generates leads for the DJ business, DJ clients become real estate leads 3-5 months post-wedding, and real estate clients refer back to the wedding services.

## Credits

Built on [Hermes-Agent](https://github.com/NousResearch/hermes-agent) by Nous Research.
