#!/usr/bin/env python3
"""
Provision Environment Variables for All Open Manus Agents on Railway.
This script sets the correct API keys, credentials, and tool configs
for each agent service so they are fully operational.

Run: python3 scripts/provision_env_vars.py
     python3 scripts/provision_env_vars.py --vault-tokens   # also sync VAULT_TOKEN
                                                            # values from the vault
"""
import argparse
import os
import json
import time
import requests

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
RAILWAY_TOKEN = os.environ["RAILWAY_ACCOUNT_API"]
PROJECT_ID = "ea6649cb-ac92-44fd-bea9-3fbf6ad5e473"
ENVIRONMENT_ID = "e57f146e-e0b8-4d5c-a443-c30e0baf016f"

HEADERS = {
    "Authorization": f"Bearer {RAILWAY_TOKEN}",
    "Content-Type": "application/json",
}

# Agent service IDs
AGENT_SERVICES = {
    "harmony":   "fb56002a-09d9-48c5-87ab-6453bae2b325",
    "samantha":  "55729960-9915-4b58-be4b-0502418e5f60",
    "tatiana":   "35016475-6a1e-42a2-95be-1c3ef62982cb",
    "jade":      "5e296395-c8f8-451b-ab2d-5d46e9cf9699",
    "sasha":     "52155bb0-e561-4e58-9c1e-13ae5b359943",
    "scarlett":  "f4a3cad5-3328-4bf5-aab5-ce185ebb99ff",
    "sabrina":   "85b08450-d0f7-4454-a3b8-2118bd30cd6c",
    "cora":      "144238cf-424d-4e4c-af6b-b8ebdd25cebe",
    "raven":     "333c04b2-a264-429c-a11c-343b7eca19b7",
    "bianca":    "03310b9d-eb82-48d1-aef9-b206c358e85a",
    "valentina": "ffe6a337-2475-47ab-83f0-8fceb80312b0",
    "addison":   "4fbd8c66-944b-46b5-83b2-ce2f1c8b6bd9",
    "lexi":      "08006723-2b99-4fa5-aec0-f4afe96a242c",
    "victoria":  "f52fbe26-83e8-48e1-8397-969f84c467c8",
    "vivian":    "ff1f2730-3eaf-43c6-8a27-4c176b354e8f",
}

# ============================================================
# SHARED VARS — Applied to ALL agents
# ============================================================
SHARED_VARS = {
    # LLM
    "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
    "LLM_MODEL": "google/gemini-2.5-flash",
    
    # Memory & Storage
    "MEMORY_BACKEND": "redis",

    # Qdrant fleet vector memory (long-term semantic recall).
    # Activated per agent via memory.provider: qdrant in deploy/<agent>/config.yaml.
    "QDRANT_URL": "http://qdrant.railway.internal:6333",
    "QDRANT_API_KEY": os.environ.get("QDRANT_API_KEY", ""),
    "WORKSPACE_DIR": "/root/.hermes/workspace",
    "HERMES_WORKSPACE_DIR": "/root/.hermes/workspace",
    
    # Inter-agent communication (n8n task dispatcher)
    "N8N_INSTANCE_URL": os.environ.get("N8N_INSTANCE_URL", ""),
    "N8N_API_KEY": os.environ.get("N8N_API_KEY", ""),
    
    # Discord (for notifications only, not agent-to-agent chat)
    "DISCORD_BOT_API": os.environ.get("DISCORD_BOT_API", ""),

    # Dedicated private status channel: gateway shutdown/restart broadcasts
    # route here (rate-limited fleet-wide) instead of each agent's home channel.
    "DISCORD_STATUS_CHANNEL_ID": "1537420503663378492",
    
    # Voice: auto-join/leave when users enter/exit the agent's designated channel
    "DISCORD_VOICE_AUTO_JOIN": "true",
    
    # Google Drive (shared access)
    "GDRIVE_ACCOUNT": "vowsok@gmail.com",
    
    # Cal.com (for scheduling links)
    "CALCOM_URL": "https://cal-production-1e5d.up.railway.app",
    "CALCOM_API_KEY": "",  # TODO: Set after Cal.com account setup
    
    # Communication protocol
    "AGENT_COMM_MODE": "n8n_task_queue",  # Use n8n, not Discord @mentions
    "AGENT_RESPONSE_TIMEOUT": "30",       # Max seconds to wait for agent response
    "AGENT_MAX_HANDOFFS": "3",            # Max task handoffs before escalating to Harmony
}

# ============================================================
# AGENT-SPECIFIC VARS
# ============================================================
AGENT_VARS = {
    "samantha": {
        # Email access - ALL inboxes
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "Personal Gmail", "email": "sanctusmm@gmail.com", "type": "gmail"},
            {"name": "Cana Collective", "email": "garrett@canaok.com", "type": "gmail"},
            {"name": "Vows & Vinyl", "email": "garrett@vowsok.com", "type": "gmail"},
            {"name": "McGarry Homes", "email": "garrett@mcgarryhomes.com", "type": "hostinger",
             "imap_host": "imap.hostinger.com", "smtp_host": "smtp.hostinger.com",
             "imap_port": "993", "smtp_port": "465"},
        ]),
        "PRIMARY_EMAIL": "sanctusmm@gmail.com",
        "GMAIL_ACCOUNTS": "sanctusmm@gmail.com,garrett@canaok.com,garrett@vowsok.com",
        "HOSTINGER_EMAIL": "garrett@mcgarryhomes.com",
        "HOSTINGER_IMAP": "imap.hostinger.com",
        "HOSTINGER_SMTP": "smtp.hostinger.com",
        # Calendar
        "GOOGLE_CALENDAR_ACCOUNT": "sanctusmm@gmail.com",
        # Role
        "AGENT_ROLE": "executive_assistant",
        "AGENT_CAPABILITIES": "email,calendar,scheduling,reminders,document_management,google_drive",
    },
    
    "tatiana": {
        # Hosting/tech access
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "McGarry Homes", "email": "garrett@mcgarryhomes.com", "type": "hostinger"},
            {"name": "Vows & Vinyl", "email": "garrett@vowsok.com", "type": "gmail"},
        ]),
        "HOSTINGER_EMAIL": "garrett@mcgarryhomes.com",
        "HOSTINGER_IMAP": "imap.hostinger.com",
        "HOSTINGER_SMTP": "smtp.hostinger.com",
        "AGENT_ROLE": "real_estate_admin",
        "AGENT_CAPABILITIES": "real_estate_admin,transaction_coordination,listing_paperwork,deadline_tracking,mcgarry_homes",
        "RAILWAY_ACCOUNT_API": os.environ.get("RAILWAY_ACCOUNT_API", ""),
    },
    
    "jade": {
        # Cana Collective + Vows & Vinyl email
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "Cana Collective", "email": "garrett@canaok.com", "type": "gmail"},
            {"name": "Vows & Vinyl", "email": "garrett@vowsok.com", "type": "gmail"},
        ]),
        "PRIMARY_EMAIL": "garrett@canaok.com",
        "GMAIL_ACCOUNTS": "garrett@canaok.com,garrett@vowsok.com",
        "AGENT_ROLE": "vows_vinyl_gm",
        "AGENT_CAPABILITIES": "dj_company_management,booking_management,vendor_coordination,event_operations,vows_vinyl",
        "BRAND_FOCUS": "cana_collective",
    },
    
    "sasha": {
        # Cana + Vows email
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "Cana Collective", "email": "garrett@canaok.com", "type": "gmail"},
            {"name": "Vows & Vinyl", "email": "garrett@vowsok.com", "type": "gmail"},
        ]),
        "PRIMARY_EMAIL": "garrett@vowsok.com",
        "GMAIL_ACCOUNTS": "garrett@canaok.com,garrett@vowsok.com",
        "AGENT_ROLE": "client_support",
        "AGENT_CAPABILITIES": "client_support,client_communication,issue_resolution,relationship_nurture",
        "BRAND_FOCUS": "vows_vinyl",
    },
    
    "scarlett": {
        # Cana + Vows email
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "Cana Collective", "email": "garrett@canaok.com", "type": "gmail"},
            {"name": "Vows & Vinyl", "email": "garrett@vowsok.com", "type": "gmail"},
        ]),
        "PRIMARY_EMAIL": "garrett@canaok.com",
        "GMAIL_ACCOUNTS": "garrett@canaok.com,garrett@vowsok.com",
        "AGENT_ROLE": "sales_advisor",
        "AGENT_CAPABILITIES": "sales,business_analysis,lead_followup,client_onboarding,proposals",
        "BRAND_FOCUS": "cana_collective,vows_vinyl",
    },
    
    "sabrina": {
        # Social media management
        "POSTIZ_URL": "https://postiz-production-14aa.up.railway.app",
        "POSTIZ_EMAIL": "sanctusmm@gmail.com",
        "POSTIZ_API_KEY": os.environ.get("POSTIZ_API_KEY", ""),
        "AGENT_ROLE": "social_media_manager",
        "AGENT_CAPABILITIES": "social_media,content_scheduling,postiz,brand_content",
        "BRAND_FOCUS": "vows_vinyl,cana_collective,mcgarry_homes",
    },
    
    "cora": {
        # Image generation
        "IMAGE_OUTPUT_DIR": "/root/.hermes/workspace/images",
        "IMAGE_MODEL_DEFAULT": "black-forest-labs/flux-1.1-pro",
        "IMAGE_MODEL_FAST": "black-forest-labs/flux-schnell",
        "AGENT_ROLE": "creative_director",
        "AGENT_CAPABILITIES": "image_generation,visual_content,brand_design,openrouter_images",
    },
    
    "raven": {
        "AGENT_ROLE": "deep_researcher",
        "AGENT_CAPABILITIES": "web_research,data_analysis,market_research,competitor_analysis",
    },
    
    "bianca": {
        "AGENT_ROLE": "investment_cfo",
        "AGENT_CAPABILITIES": "day_trading,investing,portfolio_management,crypto_analysis,alpaca",
    },
    
    "valentina": {
        "AGENT_ROLE": "automation_developer",
        "AGENT_CAPABILITIES": "automation_development,architecture,n8n_workflows,integrations,scripting",
        "BRAND_FOCUS": "mcgarry_homes",
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "McGarry Homes", "email": "garrett@mcgarryhomes.com", "type": "hostinger"},
        ]),
        "HOSTINGER_EMAIL": "garrett@mcgarryhomes.com",
        # Voice: Set to the Discord voice channel ID named "Valentina"
        "DISCORD_VOICE_CHANNEL_ID": "",  # TODO: Set to Valentina's voice channel ID
    },
    
    "addison": {
        "AGENT_ROLE": "advertising_manager",
        "AGENT_CAPABILITIES": "paid_advertising,campaign_management,ad_analytics,facebook_google_youtube_ads",
    },
    
    "lexi": {
        "AGENT_ROLE": "librarian",
        "AGENT_CAPABILITIES": "knowledge_management,hive_mind_curation,skill_registry,lesson_routing",
    },

    "victoria": {
        "AGENT_ROLE": "web_developer",
        "AGENT_CAPABILITIES": "web_development,vowsok.com,canaok.com,homesbyg.com,site_maintenance",
    },

    "vivian": {
        "AGENT_ROLE": "workflow_automation",
        "AGENT_CAPABILITIES": "process_automation,workflow_design,sop_automation,integration_glue",
    },
    
    "harmony": {
        # Harmony is the orchestrator - gets everything
        "AGENT_ROLE": "orchestrator",
        "AGENT_CAPABILITIES": "task_routing,agent_coordination,priority_management,escalation",
        "IS_ORCHESTRATOR": "true",
        "ALL_AGENT_IDS": json.dumps(AGENT_SERVICES),
        # Voice: Set to the Discord voice channel ID named "Harmony"
        "DISCORD_VOICE_CHANNEL_ID": "",  # TODO: Set to Harmony's voice channel ID
        "RAILWAY_ACCOUNT_API": os.environ.get("RAILWAY_ACCOUNT_API", ""),
    },
}


def gql(query, variables=None):
    """Execute a Railway GraphQL query."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(RAILWAY_API, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL error: {data['errors']}")
    return data["data"]


def set_agent_vars(agent_name: str, service_id: str, variables: dict):
    """Set environment variables for an agent service."""
    print(f"\n  Setting {len(variables)} vars for {agent_name}...")
    
    mutation = """
    mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
        variableCollectionUpsert(input: $input)
    }
    """
    variables_input = {
        "input": {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": service_id,
            "variables": variables
        }
    }
    
    result = gql(mutation, variables_input)
    return result


def fetch_vault_tokens() -> dict:
    """Fetch each agent's current vault token (token_plain) from Redis —
    the same store the vault service uses. Requires REDIS_URL."""
    import redis as redis_lib
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        raise SystemExit("--vault-tokens requires REDIS_URL to be set")
    r = redis_lib.from_url(redis_url, decode_responses=True)
    tokens = {}
    for agent_name in AGENT_SERVICES:
        data = r.hgetall(f"vault:agent:{agent_name}")
        token = (data or {}).get("token_plain", "")
        if token:
            tokens[agent_name] = token
        else:
            print(f"  ! No vault token found for {agent_name}")
    return tokens


def main():
    parser = argparse.ArgumentParser(description="Provision agent env vars on Railway")
    parser.add_argument("--vault-tokens", action="store_true",
                        help="Also fetch each agent's current vault token and "
                             "set it as VAULT_TOKEN on the agent's service")
    args = parser.parse_args()

    print("=" * 60)
    print("Open Manus — Agent Environment Variable Provisioner")
    print("=" * 60)

    vault_tokens = fetch_vault_tokens() if args.vault_tokens else {}

    success_count = 0
    error_count = 0
    
    for agent_name, service_id in AGENT_SERVICES.items():
        if not service_id:
            print(f"\n[{agent_name.upper()}] No Railway service ID yet — skipping")
            continue
        print(f"\n[{agent_name.upper()}] Service ID: {service_id}")
        
        # Merge shared + agent-specific vars
        agent_specific = AGENT_VARS.get(agent_name, {})
        all_vars = {**SHARED_VARS, **agent_specific}
        if agent_name in vault_tokens:
            all_vars["VAULT_TOKEN"] = vault_tokens[agent_name]
        
        # Remove empty values
        all_vars = {k: v for k, v in all_vars.items() if v}
        
        try:
            set_agent_vars(agent_name, service_id, all_vars)
            print(f"  ✓ {agent_name}: {len(all_vars)} variables set")
            success_count += 1
        except Exception as e:
            print(f"  ✗ {agent_name}: ERROR - {e}")
            error_count += 1
        
        time.sleep(0.5)  # Rate limiting
    
    print("\n" + "=" * 60)
    print(f"Done! {success_count} agents configured, {error_count} errors")
    print("=" * 60)
    
    if error_count == 0:
        print("\nAll agents are now configured with:")
        print("  ✓ Shared: OpenRouter, n8n, Discord, Cal.com, Google Drive")
        print("  ✓ Samantha: All 4 email inboxes + calendar")
        print("  ✓ Tatiana: Hosting + Railway API access")
        print("  ✓ Jade/Sasha/Scarlett: Cana + Vows email")
        print("  ✓ Sabrina: Postiz social media API")
        print("  ✓ Cora: OpenRouter image generation")
        print("  ✓ Valentina: McGarry Homes email")
        print("  ✓ Harmony: Full orchestrator access")
        print("\nNext steps:")
        print("  1. Set Gmail OAuth tokens (requires browser login)")
        print("  2. Set Hostinger email passwords in Railway secrets")
        print("  3. Configure Cal.com API key after account setup")
        print("  4. Connect social media accounts in Postiz UI")


if __name__ == "__main__":
    main()
