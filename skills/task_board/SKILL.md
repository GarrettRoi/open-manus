# Task Board — Persistent Task Tracking for Open Manus

> **Boundary:** this is task state, not live communication. The canonical
> agent-to-agent communication mechanism is the native `agent_dispatch` tool
> (`/app/skills/harmony_communication/SKILL.md`). Do not use task-board
> commands to notify an agent, and do not post instructions in the automatic
> per-agent Discord board threads.

This skill provides optional task record tracking backed by Redis. It is not
the request-board delivery or result channel. Only create a separate task
record when tracking beyond the request ticket is explicitly needed; never
require Harmony to create one before another agent can submit a ticket.

## When to Use

- **Any agent**: Use this to track an independently useful task record when
  requested. Use the canonical request-board ticket for agent handoff and
  completion, not the legacy board status.

## Commands

### Add an optional task record

```bash
python3 /app/skills/task_board/task_board.py add \
  --title "Create spring wedding promo graphics" \
  --assignee "cora" \
  --priority high \
  --deadline "2026-03-25" \
  --details "Need 3 Instagram posts and 1 Facebook cover for Vows & Vinyl spring promo"
```

### Update task status (Any agent)

```bash
python3 /app/skills/task_board/task_board.py update \
  --task-id "TASK-001" \
  --status "in_progress" \
  --notes "Started working on the first Instagram post" \
  --by "cora"
```

### Mark task as completed (Any agent)

```bash
python3 /app/skills/task_board/task_board.py complete \
  --task-id "TASK-001" \
  --result "All 3 Instagram posts and Facebook cover created. Files in shared drive."
```

### List all tasks

```bash
# All tasks
python3 /app/skills/task_board/task_board.py list

# Filter by assignee
python3 /app/skills/task_board/task_board.py list --assignee "cora"

# Filter by status
python3 /app/skills/task_board/task_board.py list --status "in_progress"
```

### View task details

```bash
python3 /app/skills/task_board/task_board.py view --task-id "TASK-001"
```

### Get board summary (task tracking only)

```bash
python3 /app/skills/task_board/task_board.py summary
```

This shows: total counts by status, active tasks, workload per agent, overdue items, and blocked tasks.

### Check for overdue tasks

```bash
python3 /app/skills/task_board/task_board.py overdue
```

### Delete a task

```bash
python3 /app/skills/task_board/task_board.py delete --task-id "TASK-001"
```

## Task Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Task created but not yet started |
| `in_progress` | Agent is actively working on it |
| `blocked` | Agent is stuck and needs help |
| `completed` | Task is done |
| `cancelled` | Task was cancelled |

## Priority Levels

| Priority | When to Use |
|----------|-------------|
| `urgent` | Needs immediate attention (client-facing, time-sensitive) |
| `high` | Should be handled today |
| `normal` | Standard priority, handle in order |
| `low` | Can wait, handle when free |

## Workflow

1. An agent identifies a task that benefits from a separate tracking record.
2. Optionally create that record with `add`. It neither delivers nor accepts
   work; use the native request-board ticket for any agent handoff.
3. Keep the tracking record current if its extra status or deadline details
   matter. The request board, not this record, carries the assignee's outcome
   and any missing required inputs.
4. Inspect `summary` when a board-level task-tracking overview is requested.

## Important Notes

- Task data persists in Redis across agent restarts
- Task IDs are sequential (TASK-001, TASK-002, etc.)
- `summary` gives a legacy task-record overview, not authoritative ticket status
- Avoid duplicating private ticket inputs in task-record details
