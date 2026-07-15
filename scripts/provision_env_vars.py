#!/usr/bin/env python3
"""
Provision Environment Variables for All Open Manus Agents on Railway.
This script sets the correct API keys, credentials, and tool configs
for each agent service so they are fully operational.

Run: python3 scripts/provision_env_vars.py
"""
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
    "WORKSPACE_DIR": "/root/.hermes/workspace",
    "HERMES_WORKSPACE_DIR": "/root/.hermes/workspace",
    
    # Inter-agent communication (n8n task dispatcher)
    "N8N_INSTANCE_URL": os.environ.get("N8N_INSTANCE_URL", ""),
    "N8N_API_KEY": os.environ.get("N8N_API_KEY", ""),
    
    # Discord (for notifications only, not agent-to-agent chat)
    "DISCORD_BOT_API": os.environ.get("DISCORD_BOT_API", ""),
    
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
        "AGENT_ROLE": "tech_ops",
        "AGENT_CAPABILITIES": "hosting,deployment,website_management,technical_support,railway_api",
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
        "AGENT_ROLE": "cana_coordinator",
        "AGENT_CAPABILITIES": "email,lead_management,vendor_coordination,cana_platform",
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
        "AGENT_ROLE": "dj_coordinator",
        "AGENT_CAPABILITIES": "email,booking_management,client_communication,dj_scheduling",
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
        "AGENT_ROLE": "sales_coordinator",
        "AGENT_CAPABILITIES": "email,sales,lead_followup,client_onboarding",
        "BRAND_FOCUS": "cana_collective,vows_vinyl",
    },
    
    "sabrina": {
        # Social media management
        "POSTIZ_URL": "https://postiz-production-14aa.up.railway.app",
        "POSTIZ_EMAIL": "sanctusmm@gmail.com",
        "POSTIZ_API_KEY": "f7f1a1569d2e7714afb7c1c9694b8ed7342eb76ff26cc3095f7c723844168ea3",
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
        "AGENT_ROLE": "researcher",
        "AGENT_CAPABILITIES": "web_research,data_analysis,market_research,competitor_analysis",
    },
    
    "bianca": {
        "AGENT_ROLE": "copywriter",
        "AGENT_CAPABILITIES": "copywriting,content_creation,seo,blog_writing,email_campaigns",
    },
    
    "valentina": {
        "AGENT_ROLE": "real_estate_coordinator",
        "AGENT_CAPABILITIES": "real_estate,client_management,listing_support,first_time_buyers",
        "BRAND_FOCUS": "mcgarry_homes",
        "EMAIL_ACCOUNTS": json.dumps([
            {"name": "McGarry Homes", "email": "garrett@mcgarryhomes.com", "type": "hostinger"},
        ]),
        "HOSTINGER_EMAIL": "garrett@mcgarryhomes.com",
        # Voice: Set to the Discord voice channel ID named "Valentina"
        "DISCORD_VOICE_CHANNEL_ID": "",  # TODO: Set to Valentina's voice channel ID
    },
    
    "addison": {
        "AGENT_ROLE": "analytics",
        "AGENT_CAPABILITIES": "analytics,reporting,metrics,performance_tracking",
    },
    
    "lexi": {
        "AGENT_ROLE": "legal_compliance",
        "AGENT_CAPABILITIES": "contracts,compliance,legal_review,terms_of_service",
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


def main():
    print("=" * 60)
    print("Open Manus — Agent Environment Variable Provisioner")
    print("=" * 60)
    
    success_count = 0
    error_count = 0
    
    for agent_name, service_id in AGENT_SERVICES.items():
        print(f"\n[{agent_name.upper()}] Service ID: {service_id}")
        
        # Merge shared + agent-specific vars
        agent_specific = AGENT_VARS.get(agent_name, {})
        all_vars = {**SHARED_VARS, **agent_specific}
        
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
