# Task Board — Persistent Task Tracking for Open Manus

This skill provides a shared task board backed by Redis. Harmony uses it to track delegated work across all agents, and agents use it to report progress and completion.

## When to Use

- **Harmony**: Use this whenever you delegate a task to an agent. Add the task to the board, then check the board periodically to follow up.
- **Worker Agents**: Use this to update your task status or mark tasks as complete.

## Commands

### Add a new task (Harmony)

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

### Get board summary (Harmony's periodic check)

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

1. **Harmony** receives a task from Garrett or identifies work to do
2. **Harmony** breaks it into sub-tasks and adds each to the board with `add`
3. **Harmony** delegates via Discord @mention with `[REQUEST]` tag
4. **Agent** updates status to `in_progress` when starting
5. **Agent** adds notes for progress updates
6. **Agent** marks `completed` when done, or `blocked` if stuck
7. **Harmony** checks `summary` periodically and follows up on stale tasks

## Important Notes

- Task data persists in Redis across agent restarts
- Task IDs are sequential (TASK-001, TASK-002, etc.)
- The `summary` command is designed for Harmony's periodic review
- Always include enough context in the `--details` field so the assignee can work independently
