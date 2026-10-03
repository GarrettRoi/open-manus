"""Bounded read-only owner/agent views over the existing authoritative records."""
import heapq
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from crm.contracts import BUSINESSES, STATUSES
from crm.store import PREFIX

MAX_LEADS = 5000
MAX_BYTES = 32 * 1024 * 1024
MAX_EVENTS = 100000


class ReadLimitError(Exception):
    pass


def calendar():
    name = os.environ.get("CRM_TIMEZONE", "UTC")
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ReadLimitError("CRM_TIMEZONE must name a valid IANA timezone.")
    return {"timezone": name, "today": datetime.fromtimestamp(time.time(), zone).date().isoformat()}


def urgency(lead, today):
    due = lead.get("next_action_date")
    active = not lead.get("archived") and lead.get("status") not in {"won", "lost"}
    return {"ready_now": bool(active and due and due <= today),
            "overdue": bool(active and due and due < today)}


def records(redis):
    """Bound work and never present a silently truncated total as complete.

    HSCAN may repeat fields during concurrent writes; deduplicate by ID. As with
    existing CRM reads this is a live view, not a transactional export snapshot.
    """
    key = PREFIX + "leads"
    if redis.hlen(key) > MAX_LEADS:
        raise ReadLimitError("CRM view exceeds 5000 leads; indexed reporting is required.")
    seen, size = set(), 0
    for ident, raw in redis.hscan_iter(key, count=50):
        size += len(raw.encode("utf-8") if isinstance(raw, str) else raw)
        if size > MAX_BYTES:
            raise ReadLimitError("CRM view exceeds its read budget; indexed reporting is required.")
        if ident in seen:
            continue
        seen.add(ident)
        if len(seen) > MAX_LEADS:
            raise ReadLimitError("CRM view exceeds 5000 leads; indexed reporting is required.")
        yield json.loads(raw)


def matches(lead, args):
    if lead.get("archived", False) != args.get("archived", False):
        return False
    if any(key in args and lead.get(key) != args[key]
           for key in ("business", "status", "source_id", "assigned_agent")):
        return False
    query = args.get("query", "").casefold()
    return not query or query in " ".join(str(lead.get(k) or "") for k in
        ("name", "email", "phone", "company", "external_id")).casefold()


def view(redis, action, args):
    meta = calendar()
    rows = [r for r in records(redis) if matches(r, args)]
    if action == "activity":
        count = 0

        def events():
            nonlocal count
            for lead in rows:
                for event in lead.get("history", []):
                    count += 1
                    if count > MAX_EVENTS:
                        raise ReadLimitError("CRM activity exceeds its read budget; indexed reporting is required.")
                    yield {"lead_id": lead["id"], "lead_name": lead.get("name") or lead.get("email") or lead.get("phone") or lead["id"],
                           "business": lead["business"], "action": event["action"],
                           "actor": event["actor"], "at": event["at"], "revision": event["revision"]}
        page, limit = args.get("page", 1), args.get("limit", 50)
        latest = heapq.nlargest(page * limit, events(),
                               key=lambda e: (e["at"], e["lead_id"], e["revision"]))
        return {**meta, "items": latest[(page - 1) * limit:], "total": count, "page": page, "limit": limit}
    selected = args.get("urgency", "all")
    if selected != "all":
        rows = [r for r in rows if urgency(r, meta["today"])[selected]]
    if action == "summary":
        return {**meta, "total": len(rows),
                "by_business": {k: sum(r["business"] == k for r in rows) for k in BUSINESSES},
                "by_status": {k: sum(r["status"] == k for r in rows) for k in STATUSES},
                "urgency": {k: sum(urgency(r, meta["today"])[k] for r in rows) for k in ("ready_now", "overdue")}}
    if args.get("sort") == "next_action" or (selected != "all" and "sort" not in args):
        rows.sort(key=lambda r: (r.get("next_action_date") or "9999-12-31", r["id"]))
    else:
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    page, limit = args.get("page", 1), args.get("limit", 50)
    return {**meta, "items": [{k: v for k, v in r.items() if k not in {"notes", "history"}}
                            for r in rows[(page - 1) * limit:page * limit]],
            "total": len(rows), "page": page, "limit": limit}