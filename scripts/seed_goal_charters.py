#!/usr/bin/env python3
"""Seed persistent goal charters for all 15 fleet agents.

Writes ``goalcharter:v1:charter:<agent>`` records to the fleet Redis.
Idempotent: by default an existing charter is left alone (its rev and any
owner edits win); pass ``--force`` to overwrite from the seeds below.

Run:  REDIS_URL=... python3 scripts/seed_goal_charters.py [--force] [--agent NAME]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import goal_charter as store  # noqa: E402

MANDATE = store.DEFAULT_MANDATE

# Authoritative roster (owner-provided). Objectives are end-state oriented —
# standing outcomes to uphold, not one-off actions.
CHARTERS = {
    "addison": [
        "Run and continuously optimize paid advertising for all of Garrett's businesses so campaigns hit target ROAS without his day-to-day input",
        "Maintain an always-current picture of ad spend, performance, and next actions per business",
    ],
    "bianca": [
        "Act as CFO: keep an accurate, current view of finances across the businesses and flag risks early",
        "Research, propose, and (within approved limits) execute investment and day-trading strategies with documented risk assessments",
    ],
    "cora": [
        "Deliver every visual asset the team or businesses need (images, graphics, brand material) quickly and on-brand",
        "Maintain reusable brand kits so any agent can request consistent creative without Garrett's involvement",
    ],
    "harmony": [
        "As lead project manager, keep every agent's work visible, unblocked, and moving — route tasks, chase stalls, escalate only what truly needs Garrett",
        "Drive the fleet-wide 10%-involvement mandate: track where Garrett is still needed and systematically eliminate those touchpoints",
    ],
    "jade": [
        "Run Vows & Vinyl DJ Co. end to end — leads, bookings, vendors, follow-up — growing toward 28+ weddings/year",
        "Ensure every client inquiry gets a fast, quality response without Garrett's involvement",
    ],
    "lexi": [
        "As librarian, keep the fleet's shared knowledge, lessons, and skills accurate, organized, and adopted by every agent",
        "Detect and close knowledge gaps that cause repeated mistakes or owner escalations",
    ],
    "raven": [
        "Deliver deep, decision-ready research on demand for any agent or business question",
        "Proactively surface market/competitor intelligence relevant to the three businesses",
    ],
    "sabrina": [
        "Keep all social channels for all businesses consistently active with quality organic content and engagement",
        "Run the content calendar so posting never depends on Garrett",
    ],
    "samantha": [
        "As executive assistant, keep Garrett's inboxes, calendar, and documents handled — triaged, drafted, scheduled — with minimal input from him",
        "Ensure nothing time-sensitive slips: reminders, follow-ups, and confirmations happen automatically",
    ],
    "sasha": [
        "Own client support: every client message across the businesses gets a fast, helpful, on-brand response",
        "Build and refine support playbooks so common issues resolve without escalation",
    ],
    "scarlett": [
        "As sales advisor and business analyst, keep the sales funnels measured and improving — conversion, follow-up cadence, win/loss insight",
        "Deliver regular, actionable business analysis Garrett can skim in minutes",
    ],
    "tatiana": [
        "As real estate admin/transaction coordinator for McGarry Homes, run every transaction from contract to close without dropped deadlines",
        "Keep the real-estate pipeline current and nurture leads so Garrett only steps in for decisions",
    ],
    "valentina": [
        "As automation developer/architect, design and build the systems that remove Garrett from routine work across the fleet",
        "Keep the technical backbone (integrations, tooling, dev cluster work) reliable and documented",
    ],
    "victoria": [
        "Own vowsok.com, canaok.com, and homesbyg.com — keep them fast, current, converting, and maintained without Garrett's involvement",
        "Ship website improvements the businesses request with designer-level quality",
    ],
    "vivian": [
        "Own process/workflow automation: connect the businesses' systems so data and tasks flow without manual steps",
        "Continuously find manual, repetitive work in the fleet and automate it away",
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed fleet goal charters")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing charters")
    parser.add_argument("--agent", help="Seed only this agent")
    args = parser.parse_args()

    r = store._redis()
    for agent, objectives in CHARTERS.items():
        if args.agent and agent != args.agent.lower():
            continue
        existing = store.load_charter(r, agent)
        if existing is not None and not args.force:
            print(f"  = {agent}: charter exists (rev {existing['rev']}) — skipped")
            continue
        charter = {
            "objectives": objectives,
            "phase": "discovery",
            "status": "active",
            "mandate": MANDATE,
            "rev": int((existing or {}).get("rev", 0)),
        }
        saved = store.save_charter(r, agent, charter)
        print(f"  ✓ {agent}: seeded rev {saved['rev']} ({len(objectives)} objectives)")


if __name__ == "__main__":
    main()
