#!/usr/bin/env python3
"""
Provision Railway Volumes for Open Manus Agents
Creates a persistent volume for each agent service and mounts it to
/root/.hermes/workspace so memory, cron jobs, and files survive redeploys.
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

# Agent service IDs from the audit
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

MOUNT_PATH = "/root/.hermes/workspace"


def gql(query, variables=None):
    """Execute a GraphQL query."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(RAILWAY_API, headers=HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise Exception(f"GraphQL error: {data['errors']}")
    return data["data"]


def create_volume(agent_name, service_id):
    """Create a Railway volume for an agent and mount it."""
    print(f"\n[{agent_name}] Creating volume...")
    
    mutation = """
    mutation volumeCreate($input: VolumeCreateInput!) {
        volumeCreate(input: $input) {
            id
            name
        }
    }
    """
    variables = {
        "input": {
            "projectId": PROJECT_ID,
            "serviceId": service_id,
            "mountPath": MOUNT_PATH,
        }
    }
    
    try:
        result = gql(mutation, variables)
        volume = result["volumeCreate"]
        print(f"  ✓ Volume created: {volume['id']} ({volume['name']})")
        return volume
    except Exception as e:
        if "already exists" in str(e).lower() or "conflict" in str(e).lower():
            print(f"  ⚠ Volume may already exist for {agent_name}: {e}")
        else:
            print(f"  ✗ Failed to create volume for {agent_name}: {e}")
        return None


def check_existing_volumes():
    """Check what volumes already exist in the project."""
    query = """
    query {
        project(id: "%s") {
            volumes {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
    }
    """ % PROJECT_ID
    
    result = gql(query)
    volumes = [e["node"] for e in result["project"]["volumes"]["edges"]]
    print(f"Existing volumes ({len(volumes)}):")
    for v in volumes:
        print(f"  - {v['name']} ({v['id']})")
    return volumes


def main():
    print("=" * 60)
    print("Open Manus — Persistent Volume Provisioning")
    print("=" * 60)
    
    # Check existing volumes first
    existing = check_existing_volumes()
    existing_names = {v["name"] for v in existing}
    
    results = {}
    for agent_name, service_id in AGENT_SERVICES.items():
        vol_name = f"agent-{agent_name}-workspace"
        if vol_name in existing_names:
            print(f"\n[{agent_name}] Volume already exists, skipping.")
            results[agent_name] = "already_exists"
            continue
        
        volume = create_volume(agent_name, service_id)
        results[agent_name] = "created" if volume else "failed"
        time.sleep(0.5)  # Rate limiting
    
    print("\n" + "=" * 60)
    print("Summary:")
    for agent, status in results.items():
        emoji = "✓" if status in ("created", "already_exists") else "✗"
        print(f"  {emoji} {agent}: {status}")
    
    print("\nDone! Agents will now persist their workspace across redeploys.")
    print(f"Mount path: {MOUNT_PATH}")


if __name__ == "__main__":
    main()
