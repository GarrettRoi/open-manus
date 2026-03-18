# Channel-Based Routing, Task Board & Harmony Cron — Implementation Summary

## What Was Built

This update adds three interconnected systems that make the 13-agent team self-managing:

### 1. Channel-Based Routing (discord.py)

The Discord adapter now enforces a structured communication hierarchy:

| Rule | Enforcement |
|------|-------------|
| Only Harmony can @mention worker agents | Workers reject `[REQUEST]` from non-Harmony bots with a no-entry reaction |
| Worker agents can only @mention Harmony | Enforced via SOUL.md instructions (soft enforcement) |
| `[END]` tag signals task completion | Recipient reacts with emoji, no reply chain starts |
| `[NOTIFY]` tag is read-only | Recipient reacts, does not reply |
| `[REQUEST]` tag triggers one response | Anti-doom-loop: tracked via cooldown set |
| Harmony receives `[END]` as awareness | Falls through to LLM so Harmony can update the task board |
| Garrett bypasses all restrictions | Owner ID always gets through |

**New environment variables** (set on all 13 services):
- `HARMONY_BOT_ID` = `1481029359757299922`
- `HARMONY_CHANNEL_ID` = `1483488835928064110`
- `TASK_BOARD_CHANNEL_ID` = `1481406227153031372`

### 2. Task Board System (skills/task_board/)

A Redis-backed persistent task tracking system with three files:

| File | Purpose |
|------|---------|
| `task_board.py` | CLI tool for adding, updating, completing, listing, and summarizing tasks |
| `harmony_cron_check.py` | Periodic review script that identifies overdue, blocked, stale, and unstarted tasks |
| `SKILL.md` | Documentation for all agents on how to use the task board |

**Task lifecycle:**
```
Harmony adds task → Agent starts (in_progress) → Agent updates notes → Agent completes → Harmony reviews
```

**Statuses:** pending, in_progress, blocked, completed, cancelled
**Priorities:** urgent, high, normal, low
**Task IDs:** Sequential (TASK-001, TASK-002, etc.)

### 3. Harmony Cron Check (harmony_cron_check.py)

Designed to run every 30 minutes. Detects:

| Issue Type | Threshold | Suggested Action |
|------------|-----------|------------------|
| OVERDUE | Past deadline | Follow up immediately, consider reassigning |
| BLOCKED | Status = blocked | Help resolve the blocker |
| STALE | No update in 2+ hours | Check in for progress update |
| NOT_STARTED | Pending for 1+ hour | Remind or reassign |

### 4. SOUL.md Updates (All 13 Agents)

Every agent's SOUL.md now includes:
- Channel architecture table (harmony-communication, home channel, task-board)
- Message tag reference ([REQUEST], [END], [NOTIFY])
- Mention rules (who can @mention whom)
- Anti-doom-loop rules
- Task board update instructions with exact CLI commands

Harmony's SOUL.md was fully rewritten with:
- Complete delegation workflow
- Task board management commands
- Periodic review instructions
- Decision framework for agent assignment

## How It Works Together

```
Garrett says "Create spring wedding promo"
    ↓
Harmony receives in #harmony-communication
    ↓
Harmony adds TASK-001 to task board
    ↓
Harmony sends [REQUEST] @Cora in #harmony-communication
    ↓
Cora acknowledges, updates task to "in_progress"
    ↓
Cora works in her home channel
    ↓
Cora sends [END] in #harmony-communication with results
    ↓
Harmony receives completion, updates task board
    ↓
Every 30 min: Harmony cron checks for stuck/overdue tasks
    ↓
If Cora was stuck: Harmony sends [REQUEST] asking for status
```

## Files Changed

| File | Change |
|------|--------|
| `gateway/platforms/discord.py` | Channel routing, [END] tag, mention enforcement |
| `entrypoint.sh` | New env vars (HARMONY_BOT_ID, HARMONY_CHANNEL_ID, TASK_BOARD_CHANNEL_ID) |
| `skills/task_board/task_board.py` | NEW — Task board CLI tool |
| `skills/task_board/harmony_cron_check.py` | NEW — Periodic review script |
| `skills/task_board/SKILL.md` | NEW — Task board documentation |
| `deploy/harmony/SOUL.md` | Full rewrite with task board + routing protocol |
| `deploy/*/SOUL.md` (12 workers) | Added communication protocol section |

## Voice Channel Note

Voice channel ID `1479781561812647969` has been noted for future voice integration. This would allow Garrett to talk to agents from the car using speech-to-text → agent processing → text-to-speech responses.
