# Task Board: Hive Mind Orchestration

The **Task Board** is the single source of truth for the entire 13-agent developer cluster. It uses a shared Redis store with concurrency locking to ensure no two agents overwrite each other's updates.

## Key Rules
- **Harmony** is the only agent authorized to *create* tasks (`add_task`).
- **Worker Agents** (Raven, Cora, Sabrina, etc.) are responsible for *updating* their own task status on the board.
- **Pull Before Push**: Always pull the latest version of the task board before making an update to avoid losing data from other agents.
- **Full Output**: When updating, you must output the entire task list, including tasks assigned to other agents.

## Usage Commands

### 1. List All Tasks
```bash
python3 /app/skills/hive_mind/task_board.py --action list
```

### 2. Add a Task (Harmony Only)
```bash
python3 /app/skills/hive_mind/task_board.py --action add --name "Research Social Post" --assignee "Raven" --notes "Focus on summer wedding trends." --agent "Harmony"
```

### 3. Update the Board (Workers)
```bash
# Pull the JSON, modify it locally, and push back
python3 /app/skills/hive_mind/task_board.py --action update --json '[{"id": "TASK-001", "name": "Research Social Post", "assigned_to": "Raven", "status": "Completed", "notes": "Done. See research.txt", "last_updated_by": "Raven"}]' --agent "Raven"
```

## Specializations Reference
- **Raven**: Research & Data.
- **Cora**: Media & Design.
- **Sabrina**: Social & Sales Copy.
- **Victoria**: Website & SEO.
- **Lexi**: Documentation & Process Learning.
- **Valentina**: Automation & Code.
- **Scarlet**: Client Support.
- **Sasha**: Sales.
- **Addison**: Ads.
- **Bianca**: Finance & Crypto.
- **Jade**: DJ Business.
- **Samantha**: Admin.
- **Tatiana**: Real Estate.
