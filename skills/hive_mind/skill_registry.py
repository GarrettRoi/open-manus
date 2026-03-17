#!/usr/bin/env python3
"""
Skill Registry — Manage and query what tools/skills each agent has access to.
The Librarian maintains this registry. Any agent can query it.
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


# Default skill assignments based on agent roles
DEFAULT_SKILLS = {
    "harmony": ["hive_mind", "inter_agent_comm", "task_routing", "daily_summary"],
    "samantha": ["hive_mind", "inter_agent_comm", "calendar_mgmt", "document_drafting", "email_mgmt"],
    "raven": ["hive_mind", "inter_agent_comm", "web_research", "data_analysis", "report_writing"],
    "scarlet": ["hive_mind", "inter_agent_comm", "client_comms", "follow_up_sequences", "crm_mgmt"],
    "bianca": ["hive_mind", "inter_agent_comm", "stock_analysis", "crypto_research", "portfolio_tracking"],
    "valentina": ["hive_mind", "inter_agent_comm", "n8n_automation", "api_integration", "web_development"],
    "sasha": ["hive_mind", "inter_agent_comm", "lead_qualification", "sales_outreach", "proposal_generation"],
    "jade": ["hive_mind", "inter_agent_comm", "dj_booking_mgmt", "event_coordination", "photo_booth_ops"],
    "tatiana": ["hive_mind", "inter_agent_comm", "transaction_coordination", "webinar_mgmt", "compliance_tracking"],
    "sabrina": ["hive_mind", "inter_agent_comm", "social_media_posting", "community_mgmt", "content_scheduling"],
    "addison": ["hive_mind", "inter_agent_comm", "ad_campaign_mgmt", "audience_targeting", "attribution_tracking"],
    "cora": ["hive_mind", "inter_agent_comm", "image_generation", "video_production", "brand_assets"],
    "librarian": ["hive_mind", "skill_registry", "knowledge_curation", "goal_review"],
}


def initialize_registry(r):
    """Initialize the skill registry with default assignments if empty."""
    registry_key = "hive:skills:registry"
    if r.exists(registry_key):
        return False

    registry = {}
    for agent, skills in DEFAULT_SKILLS.items():
        agent_key = f"hive:skills:{agent}"
        for skill in skills:
            r.sadd(agent_key, skill)
        registry[agent] = skills

    r.set(registry_key, json.dumps(registry))
    return True


def list_agent_skills(r, agent_name):
    """List all skills assigned to a specific agent."""
    agent_key = f"hive:skills:{agent_name}"
    skills = r.smembers(agent_key)

    if not skills:
        # Try initializing defaults
        if agent_name in DEFAULT_SKILLS:
            for skill in DEFAULT_SKILLS[agent_name]:
                r.sadd(agent_key, skill)
            skills = set(DEFAULT_SKILLS[agent_name])
        else:
            print(f"No skills found for agent: {agent_name}")
            return

    print(f"Skills for {agent_name} ({len(skills)} total):\n")
    for skill in sorted(skills):
        # Try to get skill metadata
        meta = r.hgetall(f"hive:skill_meta:{skill}")
        desc = meta.get("description", "No description available")
        print(f"  - {skill}: {desc}")


def list_all_skills(r):
    """List all skills across all agents."""
    all_agents = list(DEFAULT_SKILLS.keys())

    # Also check for any agents in Redis not in defaults
    for key in r.scan_iter("hive:skills:*"):
        if key == "hive:skills:registry":
            continue
        agent = key.split(":")[-1]
        if agent not in all_agents:
            all_agents.append(agent)

    print("=== Skill Registry — All Agents ===\n")
    for agent in sorted(all_agents):
        agent_key = f"hive:skills:{agent}"
        skills = r.smembers(agent_key)
        if not skills and agent in DEFAULT_SKILLS:
            skills = set(DEFAULT_SKILLS[agent])
        if skills:
            print(f"{agent}: {', '.join(sorted(skills))}")
    print()


def assign_skill(r, agent_name, skill_name, description=None):
    """Assign a skill to an agent."""
    agent_key = f"hive:skills:{agent_name}"
    r.sadd(agent_key, skill_name)

    if description:
        r.hset(f"hive:skill_meta:{skill_name}", "description", description)
        r.hset(f"hive:skill_meta:{skill_name}", "updated_at", datetime.utcnow().isoformat())

    print(f"Skill '{skill_name}' assigned to {agent_name}.")


def remove_skill(r, agent_name, skill_name):
    """Remove a skill from an agent."""
    agent_key = f"hive:skills:{agent_name}"
    removed = r.srem(agent_key, skill_name)

    if removed:
        print(f"Skill '{skill_name}' removed from {agent_name}.")
    else:
        print(f"Skill '{skill_name}' was not assigned to {agent_name}.")


def register_skill(r, skill_name, description, created_by="librarian"):
    """Register a new skill in the master registry with metadata."""
    meta_key = f"hive:skill_meta:{skill_name}"
    now = datetime.utcnow().isoformat()

    r.hset(meta_key, mapping={
        "name": skill_name,
        "description": description,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "status": "active",
    })

    print(f"Skill '{skill_name}' registered: {description}")


def retire_skill(r, skill_name):
    """Retire a skill — mark as inactive and remove from all agents."""
    meta_key = f"hive:skill_meta:{skill_name}"
    r.hset(meta_key, "status", "retired")
    r.hset(meta_key, "retired_at", datetime.utcnow().isoformat())

    # Remove from all agents
    removed_from = []
    for key in r.scan_iter("hive:skills:*"):
        if key == "hive:skills:registry":
            continue
        if r.srem(key, skill_name):
            agent = key.split(":")[-1]
            removed_from.append(agent)

    print(f"Skill '{skill_name}' retired. Removed from: {', '.join(removed_from) if removed_from else 'no agents'}.")


def main():
    parser = argparse.ArgumentParser(description="Hive Mind Skill Registry")
    parser.add_argument("--action", required=True,
                        choices=["list", "list-all", "assign", "remove", "register", "retire", "init"],
                        help="Action to perform")
    parser.add_argument("--agent", help="Agent name")
    parser.add_argument("--skill", help="Skill name")
    parser.add_argument("--description", help="Skill description")
    parser.add_argument("--created-by", default="librarian", help="Who created this skill")
    args = parser.parse_args()

    r = get_redis()

    if args.action == "init":
        if initialize_registry(r):
            print("Skill registry initialized with defaults.")
        else:
            print("Skill registry already exists.")
    elif args.action == "list":
        if not args.agent:
            print("ERROR: --agent required for list")
            sys.exit(1)
        list_agent_skills(r, args.agent)
    elif args.action == "list-all":
        list_all_skills(r)
    elif args.action == "assign":
        if not args.agent or not args.skill:
            print("ERROR: --agent and --skill required for assign")
            sys.exit(1)
        assign_skill(r, args.agent, args.skill, args.description)
    elif args.action == "remove":
        if not args.agent or not args.skill:
            print("ERROR: --agent and --skill required for remove")
            sys.exit(1)
        remove_skill(r, args.agent, args.skill)
    elif args.action == "register":
        if not args.skill or not args.description:
            print("ERROR: --skill and --description required for register")
            sys.exit(1)
        register_skill(r, args.skill, args.description, args.created_by)
    elif args.action == "retire":
        if not args.skill:
            print("ERROR: --skill required for retire")
            sys.exit(1)
        retire_skill(r, args.skill)


if __name__ == "__main__":
    main()
