#!/usr/bin/env python3
"""Publish videos found online to the shared dashboard video gallery.

Agents use this to share video URLs with the human operator. Every published
entry shows up in the dashboard's "Videos" page, where it can be watched in
an embedded player.

Storage layout (shared across all agents):
  videos:item:{id}   -> JSON {id, url, title, description, source_page, agent,
                              tags, kind, added_at}
  videos:index       -> zset id -> unix timestamp (newest first listing)

Usage:
  python3 publish_video.py --action add --url URL [--title T] [--description D]
                           [--source-page URL] [--tags a,b,c] [--agent NAME]
  python3 publish_video.py --action add-batch --file videos.json [--agent NAME]
      (file: JSON list of {url, title?, description?, source_page?, tags?})
  python3 publish_video.py --action list [--limit N]
  python3 publish_video.py --action remove --id VIDEO_ID

The gallery stores links/metadata only — never video file bytes — so there is
no meaningful Redis size concern, but entries are capped at MAX_ITEMS (oldest
evicted first).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

MAX_ITEMS = 500
MAX_TEXT = 2000

INDEX_KEY = "videos:index"
ITEM_PREFIX = "videos:item:"

_DIRECT_EXTS = (".mp4", ".webm", ".ogv", ".ogg", ".mov", ".m4v", ".m3u8")


def _redis():
    import redis

    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        print("ERROR: REDIS_URL is not set", file=sys.stderr)
        sys.exit(2)
    return redis.from_url(url, decode_responses=True, socket_connect_timeout=10)


def classify_url(url: str) -> str:
    """Return playback kind: youtube | vimeo | direct | page."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "page"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com"):
        return "youtube"
    if host in ("vimeo.com", "player.vimeo.com"):
        return "vimeo"
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in _DIRECT_EXTS):
        return "direct"
    return "page"


def _video_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _clean(text: str | None) -> str:
    return (text or "").strip()[:MAX_TEXT]


def add_video(r, url: str, title: str, description: str, source_page: str,
              tags: list[str], agent: str) -> dict:
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise ValueError(f"Not an http(s) URL: {url!r}")
    vid = _video_id(url)
    source_page = _clean(source_page)
    if source_page and not re.match(r"^https?://", source_page):
        raise ValueError(f"source_page must be an http(s) URL: {source_page!r}")
    entry = {
        "id": vid,
        "url": url,
        "title": _clean(title) or url.rsplit("/", 1)[-1] or url,
        "description": _clean(description),
        "source_page": source_page,
        "agent": _clean(agent) or os.environ.get("AGENT_NAME", "unknown"),
        "tags": [t.strip() for t in tags if t.strip()][:20],
        "kind": classify_url(url),
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    pipe = r.pipeline()
    pipe.set(ITEM_PREFIX + vid, json.dumps(entry))
    pipe.zadd(INDEX_KEY, {vid: time.time()})
    pipe.execute()
    # Evict oldest beyond cap
    excess = r.zcard(INDEX_KEY) - MAX_ITEMS
    if excess > 0:
        old = r.zrange(INDEX_KEY, 0, excess - 1)
        if old:
            pipe = r.pipeline()
            pipe.zrem(INDEX_KEY, *old)
            pipe.delete(*[ITEM_PREFIX + o for o in old])
            pipe.execute()
    return entry


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--action", required=True,
                    choices=["add", "add-batch", "list", "remove"])
    ap.add_argument("--url")
    ap.add_argument("--title", default="")
    ap.add_argument("--description", default="")
    ap.add_argument("--source-page", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--agent", default="")
    ap.add_argument("--file")
    ap.add_argument("--id")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    r = _redis()

    if args.action == "add":
        if not args.url:
            ap.error("--url is required for add")
        entry = add_video(r, args.url, args.title, args.description,
                          args.source_page, args.tags.split(","), args.agent)
        print(json.dumps({"ok": True, "id": entry["id"], "kind": entry["kind"]}))
        return 0

    if args.action == "add-batch":
        if not args.file:
            ap.error("--file is required for add-batch")
        with open(args.file, "r", encoding="utf-8") as fh:
            items = json.load(fh)
        added, failed = [], []
        for item in items:
            try:
                entry = add_video(
                    r, item["url"], item.get("title", ""),
                    item.get("description", ""), item.get("source_page", ""),
                    item.get("tags", []) if isinstance(item.get("tags"), list)
                    else str(item.get("tags", "")).split(","),
                    args.agent,
                )
                added.append(entry["id"])
            except Exception as exc:  # keep going; report at end
                failed.append({"url": item.get("url"), "error": str(exc)})
        print(json.dumps({"ok": not failed, "added": added, "failed": failed}))
        return 0 if not failed else 1

    if args.action == "list":
        ids = r.zrevrange(INDEX_KEY, 0, max(args.limit - 1, 0))
        for vid in ids:
            raw = r.get(ITEM_PREFIX + vid)
            if raw:
                print(raw)
        return 0

    if args.action == "remove":
        if not args.id:
            ap.error("--id is required for remove")
        r.zrem(INDEX_KEY, args.id)
        removed = r.delete(ITEM_PREFIX + args.id)
        print(json.dumps({"ok": bool(removed)}))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
