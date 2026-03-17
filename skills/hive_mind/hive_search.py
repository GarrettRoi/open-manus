#!/usr/bin/env python3
"""
Hive Mind Search — Query the shared knowledge base.
All agents use this to find relevant lessons before starting a task.
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


def search_lessons(r, query, agent_name=None, limit=10):
    """Search all lessons by keyword matching against title, content, and tags."""
    query_lower = query.lower()
    query_terms = query_lower.split()
    results = []

    # Scan all lesson keys
    for key in r.scan_iter("hive:lessons:*"):
        data = r.hgetall(key)
        if not data or data.get("status") == "archived":
            continue

        # Build searchable text
        searchable = " ".join([
            data.get("title", ""),
            data.get("content", ""),
            data.get("tags", ""),
            data.get("category", ""),
        ]).lower()

        # Score by number of matching terms
        score = sum(1 for term in query_terms if term in searchable)
        if score > 0:
            results.append((score, data, key))

    # Sort by score descending
    results.sort(key=lambda x: x[0], reverse=True)

    return results[:limit]


def main():
    parser = argparse.ArgumentParser(description="Search the Hive Mind")
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--agent", default="unknown", help="Agent performing the search")
    parser.add_argument("--limit", type=int, default=10, help="Max results")
    args = parser.parse_args()

    r = get_redis()
    results = search_lessons(r, args.query, args.agent, args.limit)

    if not results:
        print(f"No lessons found matching: {args.query}")
        return

    print(f"Found {len(results)} relevant lesson(s) for: {args.query}\n")
    for i, (score, data, key) in enumerate(results, 1):
        print(f"--- Lesson {i} (relevance: {score}) ---")
        print(f"Title:    {data.get('title', 'Untitled')}")
        print(f"Category: {data.get('category', 'uncategorized')}")
        print(f"Tags:     {data.get('tags', 'none')}")
        print(f"From:     {data.get('submitted_by', 'unknown')}")
        print(f"Date:     {data.get('created_at', 'unknown')}")
        print(f"Content:  {data.get('content', '')}")
        if data.get("updated_at"):
            print(f"Updated:  {data.get('updated_at')}")
        print()


if __name__ == "__main__":
    main()
