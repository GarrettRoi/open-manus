#!/usr/bin/env python3
"""
Task Board — Centralized task management for the agent hive mind.
Uses Redis for shared state and a mutex lock for concurrency safety.
"""
import os
import json
import time
import redis
import argparse
from typing import List, Dict, Optional

# Configuration from environment
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_BOARD_KEY = "hive:task_board"
LOCK_KEY = "hive:task_board_lock"
LOCK_TIMEOUT = 10  # Seconds

class TaskBoard:
    def __init__(self):
        self.r = redis.from_url(REDIS_URL, decode_responses=True)

    def _acquire_lock(self) -> bool:
        """Acquire a simple mutex lock in Redis."""
        start_time = time.time()
        while time.time() - start_time < LOCK_TIMEOUT:
            if self.r.set(LOCK_KEY, "locked", nx=True, ex=LOCK_TIMEOUT):
                return True
            time.sleep(0.1)
        return False

    def _release_lock(self):
        """Release the mutex lock."""
        self.r.delete(LOCK_KEY)

    def get_tasks(self) -> List[Dict]:
        """Fetch the entire task board."""
        data = self.r.get(TASK_BOARD_KEY)
        return json.loads(data) if data else []

    def update_tasks(self, tasks: List[Dict], updater: str):
        """Overwrite the task board with a new version."""
        if self._acquire_lock():
            try:
                # Add timestamp to the update
                for task in tasks:
                    task['last_updated_by'] = updater
                    task['updated_at'] = time.ctime()
                
                self.r.set(TASK_BOARD_KEY, json.dumps(tasks))
                print(f"Task board updated by {updater}.")
            finally:
                self._release_lock()
        else:
            print("Error: Could not acquire task board lock. Please try again.")

    def add_task(self, name: str, assigned_to: str, notes: str = "", updater: str = "Harmony"):
        """Add a single task to the board."""
        if self._acquire_lock():
            try:
                tasks = self.get_tasks()
                new_id = f"TASK-{len(tasks) + 1:03d}"
                tasks.append({
                    "id": new_id,
                    "name": name,
                    "assigned_to": assigned_to,
                    "status": "Pending",
                    "notes": notes,
                    "created_at": time.ctime(),
                    "last_updated_by": updater
                })
                self.r.set(TASK_BOARD_KEY, json.dumps(tasks))
                print(f"Task {new_id} added by {updater}.")
            finally:
                self._release_lock()
        else:
            print("Error: Could not acquire task board lock.")

def main():
    parser = argparse.ArgumentParser(description="Manage the Hive Mind Task Board.")
    parser.add_argument("--action", choices=["list", "add", "update"], required=True)
    parser.add_argument("--name", help="Task name (for add)")
    parser.add_argument("--assignee", help="Agent name (for add)")
    parser.add_argument("--notes", help="Task notes (for add)")
    parser.add_argument("--agent", default="Harmony", help="Agent performing the action")
    parser.add_argument("--json", help="Full JSON string for update action")

    args = parser.parse_args()
    board = TaskBoard()

    if args.action == "list":
        tasks = board.get_tasks()
        print(json.dumps(tasks, indent=2))
    elif args.action == "add":
        board.add_task(args.name, args.assignee, args.notes, args.agent)
    elif args.action == "update":
        if args.json:
            board.update_tasks(json.loads(args.json), args.agent)
        else:
            print("Error: --json required for update action.")

if __name__ == "__main__":
    main()
