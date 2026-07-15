# Inter-Agent Communication Skill

This skill enables agents to communicate with each other through a shared Redis message bus. Use this to delegate tasks to other agents, request information, or report status.

## How It Works

Each agent has a Redis queue named `agent:{agent_name}:inbox`. To send a task to another agent, push a JSON message to their inbox queue. To check for incoming messages, read from your own inbox.

## Usage

### Send a task to another agent

```bash
python3 /app/skills/inter_agent_comm/send_task.py \
  --to harmony \
  --from jade \
  --type task_request \
  --priority normal \
  --message "Please assign someone to create social media graphics for our spring wedding promo"
```

### Check your inbox

```bash
python3 /app/skills/inter_agent_comm/check_inbox.py --agent jade
```

### Report task completion

```bash
python3 /app/skills/inter_agent_comm/send_task.py \
  --to harmony \
  --from valentina \
  --type task_complete \
  --priority normal \
  --message "n8n workflow for DJ lead pipeline is live and tested"
```

## Message Types
- `task_request` — Request another agent to do something
- `task_complete` — Report that a delegated task is done
- `status_update` — Provide a progress update
- `escalation` — Flag an issue that needs immediate attention
- `info_request` — Ask another agent for information

## Priority Levels
- `urgent` — Needs immediate attention
- `high` — Should be handled within the hour
- `normal` — Standard priority
- `low` — Can wait, handle when free

## Important
- Always route cross-agent tasks through **Harmony** unless it's a direct response to a request
- Include enough context in your message for the receiving agent to act without asking clarifying questions
- Use `task_complete` to close the loop on delegated tasks
