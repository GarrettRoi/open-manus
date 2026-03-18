import os
import requests
import json
import sys

def deploy_workflow(name, nodes, connections, settings=None):
    api_url = os.environ.get("N8N_INSTANCE_URL")
    api_key = os.environ.get("N8N_API_KEY")
    
    if not api_url or not api_key:
        print("Error: N8N_INSTANCE_URL and N8N_API_KEY environment variables are required.")
        sys.exit(1)
        
    if not api_url.startswith("http"):
        api_url = f"https://{api_url}"
    
    headers = {
        "X-N8N-API-KEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    payload = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": settings or {"executionOrder": "v1"}
    }
    
    try:
        response = requests.post(f"{api_url}/api/v1/workflows", headers=headers, json=payload)
        if response.status_code in [200, 201]:
            print(f"Successfully deployed workflow: {name}")
            print(json.dumps(response.json(), indent=2))
        else:
            print(f"Failed to deploy workflow. Status: {response.status_code}")
            print(response.text)
            sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python deploy_workflow.py <workflow_json_file>")
        sys.exit(1)
        
    with open(sys.argv[1], 'r') as f:
        workflow_data = json.load(f)
        
    deploy_workflow(
        workflow_data.get("name", "New Workflow"),
        workflow_data.get("nodes", []),
        workflow_data.get("connections", {}),
        workflow_data.get("settings")
    )
