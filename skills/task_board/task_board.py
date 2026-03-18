#!/usr/bin/env python3
"""
Task Board — Persistent task tracking for the Open Manus agent team.

Stores tasks in Redis so they persist across agent restarts.
Harmony uses this to track delegated work, and agents use it to
report progress and completion.

Usage:
    python3 task_board.py add --title "..." --assignee "agent_name" [--priority high] [--deadline "2026-03-20"] [--details "..."]
    python3 task_board.py update --task-id "TASK-001" --status "in_progress" [--notes "Working on it"]
    python3 task_board.py complete --task-id "TASK-001" [--result "Done, here's the output..."]
    python3 task_board.py list [--assignee "agent_name"] [--status "pending"]
    python3 task_board.py view --task-id "TASK-001"
    python3 task_board.py summary
    python3 task_board.py overdue
    python3 task_board.py delete --task-id "TASK-001"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    import redis
except ImportError:
    print("ERROR: redis package not installed. Run: pip install redis")
    sys.exit(1)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_PREFIX = "taskboard:task:"
TASK_COUNTER_KEY = "taskboard:counter"
TASK_INDEX_KEY = "taskboard:index"  # sorted set of task IDs by creation time


def get_redis():
    """Connect to Redis."""
    return redis.from_url(REDIS_URL, decode_responses=True)


def generate_task_id(r):
    """Generate a sequential task ID like TASK-001."""
    counter = r.incr(TASK_COUNTER_KEY)
    return f"TASK-{counter:03d}"


def add_task(args):
    """Add a new task to the board."""
    r = get_redis()
    task_id = generate_task_id(r)
    now = datetime.now(timezone.utc).isoformat()

    task = {
        "id": task_id,
        "title": args.title,
        "assignee": args.assignee.lower(),
        "status": "pending",
        "priority": getattr(args, "priority", "normal") or "normal",
        "details": getattr(args, "details", "") or "",
        "deadline": getattr(args, "deadline", "") or "",
        "created_at": now,
        "updated_at": now,
        "created_by": getattr(args, "created_by", "harmony") or "harmony",
        "notes": [],
        "result": "",
    }

    r.set(f"{TASK_PREFIX}{task_id}", json.dumps(task))
    r.zadd(TASK_INDEX_KEY, {task_id: datetime.now(timezone.utc).timestamp()})

    print(f"Task created: {task_id}")
    print(f"  Title:    {task['title']}")
    print(f"  Assignee: {task['assignee']}")
    print(f"  Priority: {task['priority']}")
    if task["deadline"]:
        print(f"  Deadline: {task['deadline']}")
    print(f"  Status:   {task['status']}")
    return task_id


def update_task(args):
    """Update a task's status or add notes."""
    r = get_redis()
    key = f"{TASK_PREFIX}{args.task_id}"
    data = r.get(key)
    if not data:
        print(f"ERROR: Task {args.task_id} not found")
        sys.exit(1)

    task = json.loads(data)
    now = datetime.now(timezone.utc).isoformat()

    if args.status:
        old_status = task["status"]
        task["status"] = args.status
        print(f"Status: {old_status} -> {args.status}")

    if args.notes:
        task["notes"].append({
            "text": args.notes,
            "timestamp": now,
            "by": getattr(args, "by", "") or "",
        })
        print(f"Note added: {args.notes}")

    if getattr(args, "assignee", None):
        task["assignee"] = args.assignee.lower()
        print(f"Reassigned to: {args.assignee}")

    task["updated_at"] = now
    r.set(key, json.dumps(task))
    print(f"Task {args.task_id} updated successfully")


def complete_task(args):
    """Mark a task as completed."""
    r = get_redis()
    key = f"{TASK_PREFIX}{args.task_id}"
    data = r.get(key)
    if not data:
        print(f"ERROR: Task {args.task_id} not found")
        sys.exit(1)

    task = json.loads(data)
    now = datetime.now(timezone.utc).isoformat()

    task["status"] = "completed"
    task["updated_at"] = now
    task["completed_at"] = now
    if args.result:
        task["result"] = args.result

    r.set(key, json.dumps(task))
    print(f"Task {args.task_id} marked as COMPLETED")
    if args.result:
        print(f"  Result: {args.result}")


def list_tasks(args):
    """List tasks, optionally filtered by assignee or status."""
    r = get_redis()
    task_ids = r.zrange(TASK_INDEX_KEY, 0, -1)

    if not task_ids:
        print("No tasks on the board.")
        return

    tasks = []
    for tid in task_ids:
        data = r.get(f"{TASK_PREFIX}{tid}")
        if data:
            tasks.append(json.loads(data))

    # Apply filters
    if getattr(args, "assignee", None):
        tasks = [t for t in tasks if t["assignee"] == args.assignee.lower()]
    if getattr(args, "status", None):
        tasks = [t for t in tasks if t["status"] == args.status]

    if not tasks:
        print("No tasks match the filter criteria.")
        return

    # Group by status
    status_order = ["pending", "in_progress", "blocked", "completed", "cancelled"]
    status_emoji = {
        "pending": "⏳",
        "in_progress": "🔄",
        "blocked": "🚫",
        "completed": "✅",
        "cancelled": "❌",
    }
    priority_emoji = {
        "urgent": "🔴",
        "high": "🟠",
        "normal": "🟢",
        "low": "⚪",
    }

    for status in status_order:
        group = [t for t in tasks if t["status"] == status]
        if not group:
            continue

        emoji = status_emoji.get(status, "📋")
        print(f"\n{emoji} {status.upper().replace('_', ' ')} ({len(group)})")
        print("-" * 60)

        for t in group:
            p_emoji = priority_emoji.get(t.get("priority", "normal"), "🟢")
            deadline_str = f" | Due: {t['deadline']}" if t.get("deadline") else ""
            print(f"  {p_emoji} [{t['id']}] {t['title']}")
            print(f"     Assignee: {t['assignee']}{deadline_str}")
            if t.get("notes"):
                latest_note = t["notes"][-1]["text"]
                if len(latest_note) > 80:
                    latest_note = latest_note[:77] + "..."
                print(f"     Latest: {latest_note}")

    print(f"\nTotal: {len(tasks)} task(s)")


def view_task(args):
    """View detailed information about a specific task."""
    r = get_redis()
    data = r.get(f"{TASK_PREFIX}{args.task_id}")
    if not data:
        print(f"ERROR: Task {args.task_id} not found")
        sys.exit(1)

    task = json.loads(data)
    print(f"{'='*60}")
    print(f"Task: {task['id']} — {task['title']}")
    print(f"{'='*60}")
    print(f"  Status:     {task['status']}")
    print(f"  Assignee:   {task['assignee']}")
    print(f"  Priority:   {task.get('priority', 'normal')}")
    print(f"  Created:    {task['created_at']}")
    print(f"  Updated:    {task['updated_at']}")
    if task.get("deadline"):
        print(f"  Deadline:   {task['deadline']}")
    if task.get("created_by"):
        print(f"  Created by: {task['created_by']}")
    if task.get("details"):
        print(f"\n  Details:\n    {task['details']}")
    if task.get("result"):
        print(f"\n  Result:\n    {task['result']}")
    if task.get("notes"):
        print(f"\n  Notes ({len(task['notes'])}):")
        for note in task["notes"]:
            by_str = f" [{note['by']}]" if note.get("by") else ""
            print(f"    - {note['timestamp']}{by_str}: {note['text']}")
    if task.get("completed_at"):
        print(f"\n  Completed:  {task['completed_at']}")


def summary(args):
    """Show a summary of all tasks for Harmony's review."""
    r = get_redis()
    task_ids = r.zrange(TASK_INDEX_KEY, 0, -1)

    if not task_ids:
        print("📋 TASK BOARD SUMMARY: No tasks on the board.")
        return

    tasks = []
    for tid in task_ids:
        data = r.get(f"{TASK_PREFIX}{tid}")
        if data:
            tasks.append(json.loads(data))

    # Count by status
    status_counts = {}
    for t in tasks:
        s = t["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    # Count by assignee (active tasks only)
    active = [t for t in tasks if t["status"] in ("pending", "in_progress", "blocked")]
    assignee_counts = {}
    for t in active:
        a = t["assignee"]
        assignee_counts[a] = assignee_counts.get(a, 0) + 1

    print("📋 TASK BOARD SUMMARY")
    print("=" * 60)
    print(f"Total tasks: {len(tasks)}")
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    if active:
        print(f"\n🔄 ACTIVE TASKS ({len(active)}):")
        print("-" * 60)
        for t in active:
            status_icon = {"pending": "⏳", "in_progress": "🔄", "blocked": "🚫"}.get(t["status"], "📋")
            print(f"  {status_icon} [{t['id']}] {t['title']} → {t['assignee']}")

    if assignee_counts:
        print(f"\n👥 WORKLOAD:")
        for agent, count in sorted(assignee_counts.items(), key=lambda x: -x[1]):
            print(f"  {agent}: {count} active task(s)")

    # Check for overdue
    now = datetime.now(timezone.utc)
    overdue = []
    for t in active:
        if t.get("deadline"):
            try:
                deadline = datetime.fromisoformat(t["deadline"].replace("Z", "+00:00"))
                if deadline < now:
                    overdue.append(t)
            except (ValueError, TypeError):
                pass

    if overdue:
        print(f"\n⚠️ OVERDUE ({len(overdue)}):")
        for t in overdue:
            print(f"  🔴 [{t['id']}] {t['title']} → {t['assignee']} (due: {t['deadline']})")

    # Blocked tasks need attention
    blocked = [t for t in tasks if t["status"] == "blocked"]
    if blocked:
        print(f"\n🚫 BLOCKED ({len(blocked)}):")
        for t in blocked:
            latest_note = t["notes"][-1]["text"] if t.get("notes") else "No details"
            print(f"  [{t['id']}] {t['title']} → {t['assignee']}: {latest_note}")


def overdue_tasks(args):
    """List overdue tasks."""
    r = get_redis()
    task_ids = r.zrange(TASK_INDEX_KEY, 0, -1)
    now = datetime.now(timezone.utc)

    overdue = []
    for tid in task_ids:
        data = r.get(f"{TASK_PREFIX}{tid}")
        if not data:
            continue
        task = json.loads(data)
        if task["status"] in ("completed", "cancelled"):
            continue
        if task.get("deadline"):
            try:
                deadline = datetime.fromisoformat(task["deadline"].replace("Z", "+00:00"))
                if deadline < now:
                    overdue.append(task)
            except (ValueError, TypeError):
                pass

    if not overdue:
        print("No overdue tasks.")
        return

    print(f"⚠️ OVERDUE TASKS ({len(overdue)}):")
    for t in overdue:
        print(f"  🔴 [{t['id']}] {t['title']} → {t['assignee']} (due: {t['deadline']})")


def delete_task(args):
    """Delete a task from the board."""
    r = get_redis()
    key = f"{TASK_PREFIX}{args.task_id}"
    if not r.exists(key):
        print(f"ERROR: Task {args.task_id} not found")
        sys.exit(1)

    r.delete(key)
    r.zrem(TASK_INDEX_KEY, args.task_id)
    print(f"Task {args.task_id} deleted")


def main():
    parser = argparse.ArgumentParser(description="Task Board for Open Manus agents")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # add
    add_parser = subparsers.add_parser("add", help="Add a new task")
    add_parser.add_argument("--title", required=True, help="Task title")
    add_parser.add_argument("--assignee", required=True, help="Agent name to assign to")
    add_parser.add_argument("--priority", default="normal", choices=["urgent", "high", "normal", "low"])
    add_parser.add_argument("--deadline", default="", help="Deadline (ISO format)")
    add_parser.add_argument("--details", default="", help="Detailed task description")
    add_parser.add_argument("--created-by", default="harmony", help="Who created this task")

    # update
    update_parser = subparsers.add_parser("update", help="Update a task")
    update_parser.add_argument("--task-id", required=True, help="Task ID (e.g., TASK-001)")
    update_parser.add_argument("--status", choices=["pending", "in_progress", "blocked", "cancelled"])
    update_parser.add_argument("--notes", help="Add a note to the task")
    update_parser.add_argument("--assignee", help="Reassign to another agent")
    update_parser.add_argument("--by", default="", help="Who is making this update")

    # complete
    complete_parser = subparsers.add_parser("complete", help="Mark a task as completed")
    complete_parser.add_argument("--task-id", required=True, help="Task ID")
    complete_parser.add_argument("--result", default="", help="Result/output of the task")

    # list
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument("--assignee", help="Filter by assignee")
    list_parser.add_argument("--status", help="Filter by status")

    # view
    view_parser = subparsers.add_parser("view", help="View task details")
    view_parser.add_argument("--task-id", required=True, help="Task ID")

    # summary
    subparsers.add_parser("summary", help="Show task board summary")

    # overdue
    subparsers.add_parser("overdue", help="List overdue tasks")

    # delete
    delete_parser = subparsers.add_parser("delete", help="Delete a task")
    delete_parser.add_argument("--task-id", required=True, help="Task ID")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "add": add_task,
        "update": update_task,
        "complete": complete_task,
        "list": list_tasks,
        "view": view_task,
        "summary": summary,
        "overdue": overdue_tasks,
        "delete": delete_task,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
