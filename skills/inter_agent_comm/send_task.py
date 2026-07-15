#!/usr/bin/env python3
"""Send a task/message to another agent via Redis."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

def main():
    parser = argparse.ArgumentParser(description="Send a task to another agent")
    parser.add_argument("--to", required=True, help="Target agent name")
    parser.add_argument("--from", dest="sender", required=True, help="Sending agent name")
    parser.add_argument("--type", default="task_request",
                        choices=["task_request", "task_complete", "status_update", "escalation", "info_request"],
                        help="Message type")
    parser.add_argument("--priority", default="normal",
                        choices=["urgent", "high", "normal", "low"],
                        help="Priority level")
    parser.add_argument("--message", required=True, help="Message content")
    parser.add_argument("--context", default="", help="Additional context (JSON string)")
    args = parser.parse_args()

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

    try:
        import redis
        r = redis.from_url(redis_url)
    except ImportError:
        print("ERROR: redis package not installed. Run: pip install redis")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not connect to Redis at {redis_url}: {e}")
        sys.exit(1)

    message = {
        "id": f"{args.sender}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        "from": args.sender,
        "to": args.to,
        "type": args.type,
        "priority": args.priority,
        "message": args.message,
        "context": args.context,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    queue_name = f"agent:{args.to}:inbox"
    r.lpush(queue_name, json.dumps(message))

    # Also publish to a channel for real-time listeners
    r.publish(f"agent:{args.to}:notifications", json.dumps(message))

    print(f"✅ Message sent to {args.to} (queue: {queue_name})")
    print(f"   Type: {args.type} | Priority: {args.priority}")
    print(f"   ID: {message['id']}")


if __name__ == "__main__":
    main()
