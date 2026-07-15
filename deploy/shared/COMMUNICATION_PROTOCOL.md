# Agent Communication Protocol — Anti-Death-Spiral Rules

## The Core Problem
When agents @mention each other in Discord, they trigger each other's LLM response loop.
This creates infinite acknowledgment chains: "Got it" → "I see you got it" → "Yes I see you see I got it" → ...

## The Solution: Async Task Dispatch (No @Mentions Between Agents)

### Rule 1: NEVER @mention another agent
- You may ONLY @mention Harmony (`<@1481029359757299922>`) and ONLY in `#harmony-communication`
- You may NEVER @mention other worker agents (Cora, Samantha, Raven, etc.)
- Garrett always bypasses this rule — always respond to Garrett

### Rule 2: Use the Task Dispatcher for all cross-agent communication
Instead of @mentioning, use the task dispatcher tool:

```bash
# Report completion to Harmony (NO Discord message, just Redis queue)
python3 /app/skills/hive_mind/n8n_task_dispatcher.py complete \
    --task-id "TASK-001" \
    --result "Completed. Files at /workspace/output/" \
    --from "[your_name]"

# Report a blocker to Harmony
python3 /app/skills/hive_mind/n8n_task_dispatcher.py blocked \
    --task-id "TASK-001" \
    --reason "Need Garrett's approval on color palette" \
    --from "[your_name]"
```

### Rule 3: [END] tag = STOP. No reply expected, no reply given.
When you see `[END]` in a message, it means the task is done.
- Do NOT reply with "Great job!" or "Thanks for the update!"
- React with ✅ emoji only
- Update the task board silently

### Rule 4: [NOTIFY] tag = Read-only. No reply.
- React with 👀 emoji only
- Do NOT reply

### Rule 5: [REQUEST] tag = ONE response only
- Respond ONCE with your acknowledgment + plan
- Then go work in your home channel
- Do NOT send progress updates to #harmony-communication unless asked
- When done, use the task dispatcher (Rule 2) or send `[END]` ONCE

### Rule 6: Harmony checks Redis inbox, not Discord
Harmony's cron job checks the Redis task board every 30 minutes.
You do NOT need to ping Harmony in Discord when you finish a task.
The task dispatcher handles it automatically.

## Message Tag Quick Reference

| Tag | Sender | Receiver Action | Reply? |
|-----|--------|-----------------|--------|
| `[REQUEST]` | Harmony | Acknowledge + work | Once only |
| `[END]` | Any agent | React ✅, update board | NEVER |
| `[NOTIFY]` | Any agent | React 👀 | NEVER |
| `[BLOCKED]` | Any agent | Harmony investigates | Harmony only |

## The Golden Rule
**If you're about to type a message that starts with "I see that you..." or "Got it, I'll..." or "Understood, I will..." — STOP. Use the task dispatcher instead.**
