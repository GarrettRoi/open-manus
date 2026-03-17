#!/usr/bin/env python3
"""
Hive Mind Feed — Check your personal knowledge feed.
The Librarian routes approved lessons to each agent's feed based on their goals.
"""
import argparse
import json
import os
import sys

try:
    import redis
except ImportError:
    os.system("pip install redis -q")
    import redis


def get_redis():
    url = os.getenv("REDIS_URL", "redis://localhost:6379")
    return redis.from_url(url, decode_responses=True)


def get_feed(r, agent_name, mark_read=True):
    """Get all unread lessons from this agent's feed."""
    feed_key = f"hive:feed:{agent_name}"
    entries = r.lrange(feed_key, 0, -1)

    if not entries:
        return []

    lessons = []
    for entry_json in entries:
        try:
            entry = json.loads(entry_json)
            lessons.append(entry)
        except json.JSONDecodeError:
            continue

    if mark_read:
        # Move read items to a read-feed archive
        read_key = f"hive:feed:{agent_name}:read"
        for entry_json in entries:
            r.rpush(read_key, entry_json)
        r.delete(feed_key)

    return lessons


def main():
    parser = argparse.ArgumentParser(description="Check your Hive Mind knowledge feed")
    parser.add_argument("--agent", required=True, help="Your agent name")
    parser.add_argument("--no-mark-read", action="store_true", help="Don't mark entries as read")
    args = parser.parse_args()

    r = get_redis()
    lessons = get_feed(r, args.agent, mark_read=not args.no_mark_read)

    if not lessons:
        print(f"No new lessons in {args.agent}'s feed.")
        return

    print(f"{len(lessons)} new lesson(s) in your feed:\n")
    for i, lesson in enumerate(lessons, 1):
        print(f"--- New Lesson {i} ---")
        print(f"Title:      {lesson.get('title', 'Untitled')}")
        print(f"From:       {lesson.get('submitted_by', 'unknown')}")
        print(f"Goal Match: {lesson.get('matching_goal', 'general')}")
        print(f"Content:    {lesson.get('content', '')}")
        if lesson.get("adaptation_note"):
            print(f"Note:       {lesson.get('adaptation_note')}")
        print()


if __name__ == "__main__":
    main()
