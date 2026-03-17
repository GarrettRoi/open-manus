#!/usr/bin/env python3
"""
Hive Mind Submit — Submit a lesson to the Librarian for review.
All agents use this when they discover something useful during a task.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis


def get_redis():
    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.from_url(url, decode_responses=True)


def submit_lesson(r, agent_name, title, content, tags):
    """Submit a new lesson to the Librarian's intake queue."""
    lesson_id = f"lesson_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()

    lesson = {
        "id": lesson_id,
        "title": title,
        "content": content,
        "tags": tags,
        "submitted_by": agent_name,
        "created_at": now,
        "status": "pending",
    }

    # Store the lesson data
    r.hset(f"hive:lessons:{lesson_id}", mapping=lesson)

    # Add to Librarian's inbox
    r.rpush("hive:inbox:librarian", json.dumps(lesson))

    return lesson_id


def main():
    parser = argparse.ArgumentParser(description="Submit a lesson to the Hive Mind")
    parser.add_argument("--agent", required=True, help="Your agent name")
    parser.add_argument("--title", required=True, help="Short descriptive title")
    parser.add_argument("--content", required=True, help="The full lesson content")
    parser.add_argument("--tags", default="", help="Comma-separated tags (e.g., sales,real_estate,leads)")
    args = parser.parse_args()

    r = get_redis()
    lesson_id = submit_lesson(r, args.agent, args.title, args.content, args.tags)

    print(f"Lesson submitted successfully.")
    print(f"  ID:    {lesson_id}")
    print(f"  Title: {args.title}")
    print(f"  From:  {args.agent}")
    print(f"  Tags:  {args.tags}")
    print(f"  Status: pending (awaiting Librarian review)")


if __name__ == "__main__":
    main()
