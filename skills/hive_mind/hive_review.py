#!/usr/bin/env python3
"""
Hive Mind Review — Librarian's curation tool.
Manages the intake queue: approve, update, archive, reject lessons.
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


def list_inbox(r):
    """List all pending lessons in the Librarian's inbox."""
    entries = r.lrange("hive:inbox:librarian", 0, -1)
    if not entries:
        print("Inbox is empty. No pending lessons.")
        return

    print(f"{len(entries)} pending lesson(s):\n")
    for i, entry_json in enumerate(entries, 1):
        try:
            entry = json.loads(entry_json)
            print(f"--- Pending {i} ---")
            print(f"  ID:      {entry.get('id')}")
            print(f"  Title:   {entry.get('title')}")
            print(f"  From:    {entry.get('submitted_by')}")
            print(f"  Tags:    {entry.get('tags')}")
            print(f"  Date:    {entry.get('created_at')}")
            print(f"  Content: {entry.get('content', '')[:200]}...")
            print()
        except json.JSONDecodeError:
            print(f"  [Malformed entry at position {i}]")


def approve_lesson(r, lesson_id, targets, adapted_content=None):
    """Approve a lesson and distribute to target agents' feeds."""
    lesson_key = f"hive:lessons:{lesson_id}"
    lesson_data = r.hgetall(lesson_key)

    if not lesson_data:
        print(f"ERROR: Lesson {lesson_id} not found.")
        return False

    now = datetime.utcnow().isoformat()

    # Update lesson status
    r.hset(lesson_key, "status", "approved")
    r.hset(lesson_key, "approved_at", now)
    r.hset(lesson_key, "distributed_to", ",".join(targets))

    # Distribute to each target agent's feed
    for agent in targets:
        agent = agent.strip()
        feed_entry = {
            "lesson_id": lesson_id,
            "title": lesson_data.get("title", ""),
            "content": adapted_content if adapted_content else lesson_data.get("content", ""),
            "submitted_by": lesson_data.get("submitted_by", ""),
            "tags": lesson_data.get("tags", ""),
            "delivered_at": now,
        }
        if adapted_content:
            feed_entry["adaptation_note"] = "This lesson was adapted for your specific goals by the Librarian."
            feed_entry["original_content"] = lesson_data.get("content", "")

        r.rpush(f"hive:feed:{agent}", json.dumps(feed_entry))

    # Remove from inbox
    _remove_from_inbox(r, lesson_id)

    print(f"Lesson {lesson_id} approved and distributed to: {', '.join(targets)}")
    return True


def update_lesson(r, lesson_id, new_content):
    """Update an existing lesson with new/merged content instead of creating a duplicate."""
    lesson_key = f"hive:lessons:{lesson_id}"
    lesson_data = r.hgetall(lesson_key)

    if not lesson_data:
        print(f"ERROR: Lesson {lesson_id} not found.")
        return False

    now = datetime.utcnow().isoformat()
    old_content = lesson_data.get("content", "")

    # Store update history
    update_count = int(lesson_data.get("update_count", "0")) + 1
    r.hset(lesson_key, "content", new_content)
    r.hset(lesson_key, "updated_at", now)
    r.hset(lesson_key, "update_count", str(update_count))
    r.hset(lesson_key, f"previous_content_{update_count}", old_content)

    print(f"Lesson {lesson_id} updated (revision {update_count}).")
    print(f"  Previous content preserved as revision history.")
    return True


def archive_lesson(r, lesson_id):
    """Archive a lesson — remove from active use but preserve it."""
    lesson_key = f"hive:lessons:{lesson_id}"
    lesson_data = r.hgetall(lesson_key)

    if not lesson_data:
        print(f"ERROR: Lesson {lesson_id} not found.")
        return False

    now = datetime.utcnow().isoformat()
    r.hset(lesson_key, "status", "archived")
    r.hset(lesson_key, "archived_at", now)

    # Also store in archive set for easy listing
    r.sadd("hive:archive", lesson_id)

    _remove_from_inbox(r, lesson_id)

    print(f"Lesson {lesson_id} archived.")
    return True


def reject_lesson(r, lesson_id, reason):
    """Reject a lesson with feedback to the submitting agent."""
    lesson_key = f"hive:lessons:{lesson_id}"
    lesson_data = r.hgetall(lesson_key)

    if not lesson_data:
        print(f"ERROR: Lesson {lesson_id} not found.")
        return False

    now = datetime.utcnow().isoformat()
    r.hset(lesson_key, "status", "rejected")
    r.hset(lesson_key, "rejected_at", now)
    r.hset(lesson_key, "rejection_reason", reason)

    # Notify the submitting agent via their feed
    submitter = lesson_data.get("submitted_by", "unknown")
    feedback = {
        "lesson_id": lesson_id,
        "title": f"[REJECTED] {lesson_data.get('title', '')}",
        "content": f"Your submitted lesson was not approved. Reason: {reason}",
        "submitted_by": "librarian",
        "delivered_at": now,
    }
    r.rpush(f"hive:feed:{submitter}", json.dumps(feedback))

    _remove_from_inbox(r, lesson_id)

    print(f"Lesson {lesson_id} rejected. Feedback sent to {submitter}.")
    return True


def list_all_lessons(r, status_filter=None):
    """List all lessons in the hive mind, optionally filtered by status."""
    count = 0
    for key in r.scan_iter("hive:lessons:*"):
        data = r.hgetall(key)
        if not data:
            continue
        if status_filter and data.get("status") != status_filter:
            continue
        count += 1
        print(f"  [{data.get('status', 'unknown')}] {data.get('id', key)} — {data.get('title', 'Untitled')} (by {data.get('submitted_by', '?')}, tags: {data.get('tags', 'none')})")

    if count == 0:
        print(f"No lessons found{' with status ' + status_filter if status_filter else ''}.")
    else:
        print(f"\nTotal: {count} lesson(s)")


def _remove_from_inbox(r, lesson_id):
    """Remove a specific lesson from the Librarian's inbox."""
    entries = r.lrange("hive:inbox:librarian", 0, -1)
    for entry_json in entries:
        try:
            entry = json.loads(entry_json)
            if entry.get("id") == lesson_id:
                r.lrem("hive:inbox:librarian", 1, entry_json)
                return
        except json.JSONDecodeError:
            continue


def main():
    parser = argparse.ArgumentParser(description="Librarian's Hive Mind Review Tool")
    parser.add_argument("--action", required=True,
                        choices=["list", "list-all", "approve", "update", "archive", "reject"],
                        help="Action to perform")
    parser.add_argument("--lesson-id", help="Lesson ID to act on")
    parser.add_argument("--targets", help="Comma-separated agent names for distribution")
    parser.add_argument("--adapted-content", help="Reframed content for specific agents")
    parser.add_argument("--content", help="New/updated content for the lesson")
    parser.add_argument("--reason", help="Reason for rejection")
    parser.add_argument("--status", help="Filter by status (approved, pending, archived, rejected)")
    args = parser.parse_args()

    r = get_redis()

    if args.action == "list":
        list_inbox(r)
    elif args.action == "list-all":
        list_all_lessons(r, args.status)
    elif args.action == "approve":
        if not args.lesson_id or not args.targets:
            print("ERROR: --lesson-id and --targets required for approve")
            sys.exit(1)
        approve_lesson(r, args.lesson_id, args.targets.split(","), args.adapted_content)
    elif args.action == "update":
        if not args.lesson_id or not args.content:
            print("ERROR: --lesson-id and --content required for update")
            sys.exit(1)
        update_lesson(r, args.lesson_id, args.content)
    elif args.action == "archive":
        if not args.lesson_id:
            print("ERROR: --lesson-id required for archive")
            sys.exit(1)
        archive_lesson(r, args.lesson_id)
    elif args.action == "reject":
        if not args.lesson_id or not args.reason:
            print("ERROR: --lesson-id and --reason required for reject")
            sys.exit(1)
        reject_lesson(r, args.lesson_id, args.reason)


if __name__ == "__main__":
    main()
