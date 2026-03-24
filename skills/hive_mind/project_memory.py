#!/usr/bin/env python3
"""
Project Memory — Shared real-time context for the Developer Cluster.
Allows Valentina, Victoria, and Vivian to stay "joined at the hip."
"""
import argparse
import json
import os
import sys
from datetime import datetime

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis


def get_redis():
    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.from_url(url, decode_responses=True)


def log_update(r, agent_name, update_text, project_id="default"):
    """Log a real-time update to the shared project memory."""
    memory_key = f"hive:project_memory:{project_id}"
    now = datetime.utcnow().isoformat()
    
    entry = {
        "agent": agent_name,
        "timestamp": now,
        "content": update_text
    }
    
    # Push to a list and keep only the last 50 updates
    r.lpush(memory_key, json.dumps(entry))
    r.ltrim(memory_key, 0, 49)
    
    print(f"Update logged to project '{project_id}' by {agent_name}.")


def get_memory(r, project_id="default", limit=10):
    """Retrieve the latest context from the shared project memory."""
    memory_key = f"hive:project_memory:{project_id}"
    entries = r.lrange(memory_key, 0, limit - 1)
    
    if not entries:
        print(f"No memory found for project '{project_id}'.")
        return
        
    print(f"=== Shared Project Memory: {project_id} ===\n")
    for entry_json in reversed(entries):
        entry = json.loads(entry_json)
        print(f"[{entry['timestamp']}] {entry['agent']}: {entry['content']}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Hive Mind Project Memory")
    parser.add_argument("--action", required=True, choices=["log", "get"], help="Action to perform")
    parser.add_argument("--agent", help="Agent name (for log)")
    parser.add_argument("--content", help="Update content (for log)")
    parser.add_argument("--project", default="default", help="Project ID")
    parser.add_argument("--limit", type=int, default=10, help="Number of entries to retrieve")
    args = parser.parse_args()

    r = get_redis()

    if args.action == "log":
        if not args.agent or not args.content:
            print("ERROR: --agent and --content required for log")
            sys.exit(1)
        log_update(r, args.agent, args.content, args.project)
    elif args.action == "get":
        get_memory(r, args.project, args.limit)


if __name__ == "__main__":
    main()
