#!/usr/bin/env python3
"""
Harmony Cron Check — Periodic task board review.

This script is designed to be run by Harmony's cron job every 30 minutes.
It checks the task board for:
  1. Overdue tasks that need escalation
  2. Blocked tasks that need intervention
  3. Stale in-progress tasks (no update in 2+ hours)
  4. Pending tasks that haven't been started

Output is formatted for Harmony to read and act on.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

try:
    import redis
except ImportError:
    print("ERROR: redis package not installed. Run: pip install redis")
    sys.exit(1)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_PREFIX = "taskboard:task:"
TASK_INDEX_KEY = "taskboard:index"

# Thresholds
STALE_HOURS = 2  # Tasks in_progress with no update for this long
PENDING_HOURS = 1  # Tasks pending for this long without being started


def get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


def check_board():
    """Review the task board and generate action items for Harmony."""
    r = get_redis()
    task_ids = r.zrange(TASK_INDEX_KEY, 0, -1)

    if not task_ids:
        print("TASK BOARD CHECK: No tasks on the board. Nothing to review.")
        return

    now = datetime.now(timezone.utc)
    tasks = []
    for tid in task_ids:
        data = r.get(f"{TASK_PREFIX}{tid}")
        if data:
            tasks.append(json.loads(data))

    active = [t for t in tasks if t["status"] in ("pending", "in_progress", "blocked")]

    if not active:
        completed_today = [
            t for t in tasks
            if t["status"] == "completed"
            and t.get("completed_at")
            and (now - datetime.fromisoformat(t["completed_at"].replace("Z", "+00:00"))).total_seconds() < 86400
        ]
        print(f"TASK BOARD CHECK: All clear! No active tasks.")
        if completed_today:
            print(f"  {len(completed_today)} task(s) completed in the last 24 hours.")
        return

    action_items = []

    # 1. Overdue tasks
    for t in active:
        if t.get("deadline"):
            try:
                deadline = datetime.fromisoformat(t["deadline"].replace("Z", "+00:00"))
                if not deadline.tzinfo:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline < now:
                    days_overdue = (now - deadline).days
                    action_items.append({
                        "type": "OVERDUE",
                        "severity": "urgent",
                        "task": t,
                        "detail": f"Overdue by {days_overdue} day(s)",
                    })
            except (ValueError, TypeError):
                pass

    # 2. Blocked tasks
    for t in active:
        if t["status"] == "blocked":
            latest_note = t["notes"][-1]["text"] if t.get("notes") else "No details provided"
            action_items.append({
                "type": "BLOCKED",
                "severity": "high",
                "task": t,
                "detail": latest_note,
            })

    # 3. Stale in-progress tasks
    for t in active:
        if t["status"] == "in_progress":
            updated = datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00"))
            if not updated.tzinfo:
                updated = updated.replace(tzinfo=timezone.utc)
            hours_since = (now - updated).total_seconds() / 3600
            if hours_since > STALE_HOURS:
                action_items.append({
                    "type": "STALE",
                    "severity": "normal",
                    "task": t,
                    "detail": f"No update in {hours_since:.1f} hours",
                })

    # 4. Pending tasks not yet started
    for t in active:
        if t["status"] == "pending":
            created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            if not created.tzinfo:
                created = created.replace(tzinfo=timezone.utc)
            hours_since = (now - created).total_seconds() / 3600
            if hours_since > PENDING_HOURS:
                action_items.append({
                    "type": "NOT_STARTED",
                    "severity": "normal",
                    "task": t,
                    "detail": f"Pending for {hours_since:.1f} hours",
                })

    # Generate report
    print(f"TASK BOARD CHECK — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Active tasks: {len(active)}")
    print()

    if not action_items:
        print("All active tasks are progressing normally. No action needed.")
        print()
        print("Active task summary:")
        for t in active:
            print(f"  [{t['id']}] {t['title']} -> {t['assignee']} ({t['status']})")
        return

    severity_order = {"urgent": 0, "high": 1, "normal": 2}
    action_items.sort(key=lambda x: severity_order.get(x["severity"], 99))

    print(f"ACTION ITEMS ({len(action_items)}):")
    print("=" * 60)

    for item in action_items:
        t = item["task"]
        severity_icon = {"urgent": "🔴", "high": "🟠", "normal": "🟡"}.get(item["severity"], "⚪")
        print(f"\n{severity_icon} {item['type']}: [{t['id']}] {t['title']}")
        print(f"   Assignee: {t['assignee']}")
        print(f"   Detail:   {item['detail']}")

        # Suggest action
        if item["type"] == "OVERDUE":
            print(f"   ACTION:   Follow up with {t['assignee']} immediately. Consider reassigning if no response.")
        elif item["type"] == "BLOCKED":
            print(f"   ACTION:   Help {t['assignee']} resolve the blocker. May need to involve another agent.")
        elif item["type"] == "STALE":
            print(f"   ACTION:   Check in with {t['assignee']} for a progress update.")
        elif item["type"] == "NOT_STARTED":
            print(f"   ACTION:   Remind {t['assignee']} to start this task or reassign if they're overloaded.")

    # Workload summary
    print(f"\n{'='*60}")
    print("WORKLOAD:")
    assignee_counts = {}
    for t in active:
        a = t["assignee"]
        assignee_counts[a] = assignee_counts.get(a, 0) + 1
    for agent, count in sorted(assignee_counts.items(), key=lambda x: -x[1]):
        print(f"  {agent}: {count} active task(s)")


if __name__ == "__main__":
    check_board()
