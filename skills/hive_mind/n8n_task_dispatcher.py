#!/usr/bin/env python3
"""
N8N Task Dispatcher — Fire-and-forget task routing via n8n webhooks.

This replaces Discord @mention chains with a proper async task queue.
Harmony uses this to dispatch tasks to agents WITHOUT waiting for a reply.
Agents use this to report completion WITHOUT triggering a reply chain.

The n8n workflow receives the webhook, logs it to the task board,
and posts a single formatted message to the appropriate Discord channel
via the Discord bot — with NO expectation of a reply.

Usage:
    # Harmony dispatches a task to Cora
    python3 n8n_task_dispatcher.py dispatch \
        --to cora \
        --task-id TASK-001 \
        --message "Create 3 Instagram posts for spring wedding promo" \
        --priority high

    # Cora reports completion
    python3 n8n_task_dispatcher.py complete \
        --task-id TASK-001 \
        --result "Done. Files saved to /workspace/spring-promo/" \
        --from cora

    # Any agent reports a blocker
    python3 n8n_task_dispatcher.py blocked \
        --task-id TASK-001 \
        --reason "Need Garrett's approval on color palette" \
        --from cora
"""
import argparse
import json
import os
import sys
import requests
from datetime import datetime, timezone

# N8N webhook URL for task dispatch
N8N_TASK_WEBHOOK = os.getenv(
    "N8N_TASK_DISPATCH_WEBHOOK",
    os.getenv("N8N_INSTANCE_URL", "").rstrip("/") + "/webhook/agent-task-dispatch"
)

# Redis for task board updates
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def send_webhook(payload: dict) -> bool:
    """Send payload to n8n webhook. Returns True on success."""
    if not N8N_TASK_WEBHOOK or N8N_TASK_WEBHOOK.endswith("/webhook/agent-task-dispatch"):
        # Fallback: use Redis directly if n8n isn't configured
        return send_via_redis(payload)
    
    try:
        resp = requests.post(N8N_TASK_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"[dispatcher] Sent via n8n webhook: {resp.status_code}")
        return True
    except Exception as e:
        print(f"[dispatcher] n8n webhook failed: {e}. Falling back to Redis.")
        return send_via_redis(payload)


def send_via_redis(payload: dict) -> bool:
    """Fallback: push task notification to Redis queue for Harmony to process."""
    try:
        import redis as redis_lib
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        
        # Push to Harmony's inbox if it's a completion/blocked notification
        if payload.get("type") in ("task_complete", "task_blocked"):
            r.lpush("agent:harmony:inbox", json.dumps({
                "type": payload["type"],
                "from": payload.get("from", "unknown"),
                "task_id": payload.get("task_id"),
                "message": payload.get("message", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "id": f"msg-{int(datetime.now().timestamp())}"
            }))
            print(f"[dispatcher] Pushed to Harmony's Redis inbox.")
        
        # Push to target agent's inbox if it's a task dispatch
        elif payload.get("type") == "task_dispatch":
            target = payload.get("to", "").lower()
            if target:
                r.lpush(f"agent:{target}:inbox", json.dumps({
                    "type": "task_request",
                    "from": "harmony",
                    "task_id": payload.get("task_id"),
                    "message": payload.get("message", ""),
                    "priority": payload.get("priority", "normal"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "id": f"msg-{int(datetime.now().timestamp())}"
                }))
                print(f"[dispatcher] Pushed to {target}'s Redis inbox.")
        
        return True
    except Exception as e:
        print(f"[dispatcher] Redis fallback also failed: {e}")
        return False


def dispatch_task(to: str, task_id: str, message: str, priority: str = "normal", sender: str = "harmony"):
    """Dispatch a task to an agent."""
    payload = {
        "type": "task_dispatch",
        "to": to.lower(),
        "from": sender.lower(),
        "task_id": task_id,
        "message": message,
        "priority": priority,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    success = send_webhook(payload)
    if success:
        print(f"[dispatcher] Task {task_id} dispatched to {to}.")
    else:
        print(f"[dispatcher] Failed to dispatch task {task_id} to {to}.")
    return success


def report_complete(task_id: str, result: str, sender: str):
    """Report task completion to Harmony."""
    payload = {
        "type": "task_complete",
        "from": sender.lower(),
        "task_id": task_id,
        "message": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    success = send_webhook(payload)
    if success:
        print(f"[dispatcher] Task {task_id} completion reported.")
    return success


def report_blocked(task_id: str, reason: str, sender: str):
    """Report a task blocker to Harmony."""
    payload = {
        "type": "task_blocked",
        "from": sender.lower(),
        "task_id": task_id,
        "message": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    success = send_webhook(payload)
    if success:
        print(f"[dispatcher] Task {task_id} blocker reported.")
    return success


def main():
    parser = argparse.ArgumentParser(description="N8N Task Dispatcher")
    subparsers = parser.add_subparsers(dest="command")

    # dispatch
    p_dispatch = subparsers.add_parser("dispatch", help="Dispatch a task to an agent")
    p_dispatch.add_argument("--to", required=True)
    p_dispatch.add_argument("--task-id", required=True)
    p_dispatch.add_argument("--message", required=True)
    p_dispatch.add_argument("--priority", default="normal", choices=["urgent", "high", "normal", "low"])
    p_dispatch.add_argument("--from", dest="sender", default="harmony")

    # complete
    p_complete = subparsers.add_parser("complete", help="Report task completion")
    p_complete.add_argument("--task-id", required=True)
    p_complete.add_argument("--result", required=True)
    p_complete.add_argument("--from", dest="sender", required=True)

    # blocked
    p_blocked = subparsers.add_parser("blocked", help="Report a task blocker")
    p_blocked.add_argument("--task-id", required=True)
    p_blocked.add_argument("--reason", required=True)
    p_blocked.add_argument("--from", dest="sender", required=True)

    args = parser.parse_args()

    if args.command == "dispatch":
        dispatch_task(args.to, args.task_id, args.message, args.priority, args.sender)
    elif args.command == "complete":
        report_complete(args.task_id, args.result, args.sender)
    elif args.command == "blocked":
        report_blocked(args.task_id, args.reason, args.sender)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
