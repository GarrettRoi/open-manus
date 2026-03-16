#!/usr/bin/env python3
"""Check an agent's inbox for pending messages from other agents."""

import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description="Check agent inbox")
    parser.add_argument("--agent", required=True, help="Agent name to check inbox for")
    parser.add_argument("--count", type=int, default=10, help="Max messages to retrieve")
    parser.add_argument("--pop", action="store_true", help="Remove messages after reading")
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

    queue_name = f"agent:{args.agent}:inbox"
    queue_length = r.llen(queue_name)

    if queue_length == 0:
        print(f"📭 No messages in {args.agent}'s inbox.")
        return

    print(f"📬 {queue_length} message(s) in {args.agent}'s inbox:")
    print("=" * 60)

    count = min(args.count, queue_length)
    for i in range(count):
        if args.pop:
            raw = r.rpop(queue_name)
        else:
            raw = r.lindex(queue_name, -(i + 1))

        if raw is None:
            break

        try:
            msg = json.loads(raw)
            priority_emoji = {
                "urgent": "🔴",
                "high": "🟠",
                "normal": "🟢",
                "low": "⚪"
            }.get(msg.get("priority", "normal"), "🟢")

            print(f"\n{priority_emoji} [{msg.get('type', 'unknown')}] from {msg.get('from', '?')}")
            print(f"   Time: {msg.get('timestamp', 'unknown')}")
            print(f"   Message: {msg.get('message', '')}")
            if msg.get("context"):
                print(f"   Context: {msg['context']}")
            print(f"   ID: {msg.get('id', 'unknown')}")
        except json.JSONDecodeError:
            print(f"  [raw] {raw}")

    if queue_length > count:
        print(f"\n... and {queue_length - count} more message(s)")


if __name__ == "__main__":
    main()
