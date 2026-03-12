#!/usr/bin/env python3
"""
Open Manus Task Planner
Replicates the Manus.im `plan` tool functionality.
Manages a structured, multi-phase task plan stored in a JSON file.
"""
import json
import argparse
import os
import sys

WORKSPACE_DIR = os.path.expanduser(os.environ.get("HERMES_WORKSPACE_DIR", "~/.hermes/workspace"))
PLAN_FILE = os.path.join(WORKSPACE_DIR, "plan.json")

def get_plan():
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    if not os.path.exists(PLAN_FILE):
        return {"goal": "", "phases": [], "current_phase_id": 0}
    with open(PLAN_FILE, 'r') as f:
        return json.load(f)

def save_plan(plan):
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    with open(PLAN_FILE, 'w') as f:
        json.dump(plan, f, indent=2)

def update_plan(goal, phases_json, current_phase_id):
    plan = get_plan()
    plan['goal'] = goal
    plan['phases'] = json.loads(phases_json)
    plan['current_phase_id'] = int(current_phase_id)
    save_plan(plan)
    print("✓ Plan updated.")
    show_plan()

def advance_plan(next_phase_id):
    plan = get_plan()
    old_id = plan['current_phase_id']
    plan['current_phase_id'] = int(next_phase_id)
    save_plan(plan)
    print(f"✓ Advanced from phase {old_id} to phase {next_phase_id}.")
    show_plan()

def show_plan():
    plan = get_plan()
    if not plan['goal']:
        print("No active plan.")
        return
    print("\n╔══════════════════════════════════════════════════╗")
    print(f"║  TASK PLAN                                       ║")
    print(f"╠══════════════════════════════════════════════════╣")
    goal_lines = [plan['goal'][i:i+48] for i in range(0, len(plan['goal']), 48)]
    for line in goal_lines:
        print(f"║  Goal: {line:<42} ║")
    print(f"╠══════════════════════════════════════════════════╣")
    for phase in plan['phases']:
        is_current = phase['id'] == plan['current_phase_id']
        marker = "▶" if is_current else " "
        status = "(CURRENT)" if is_current else "        "
        title = phase['title'][:35]
        print(f"║  {marker} [{phase['id']:>2}] {title:<35} {status} ║")
    print("╚══════════════════════════════════════════════════╝\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Open Manus Task Planner")
    subparsers = parser.add_subparsers(dest='command')

    update_parser = subparsers.add_parser('update', help='Create or update the task plan')
    update_parser.add_argument('--goal', required=True, help='The overall goal of the task')
    update_parser.add_argument('--phases', required=True, help='JSON array of phase objects [{id, title}]')
    update_parser.add_argument('--current', required=True, help='ID of the current phase')

    advance_parser = subparsers.add_parser('advance', help='Advance to the next phase')
    advance_parser.add_argument('--next', required=True, help='ID of the next phase')

    show_parser = subparsers.add_parser('show', help='Display the current plan')

    args = parser.parse_args()

    if args.command == 'update':
        update_plan(args.goal, args.phases, args.current)
    elif args.command == 'advance':
        advance_plan(args.next)
    elif args.command == 'show':
        show_plan()
    else:
        parser.print_help()
        sys.exit(1)
