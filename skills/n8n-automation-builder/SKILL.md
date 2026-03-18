---
name: n8n-automation-builder
description: "Build and deploy n8n workflows from scratch. Use for: creating n8n automation workflows, exploring available n8n credentials, and deploying workflows to the user's n8n instance."
---

# n8n Automation Builder

This skill enables Manus to design, build, and deploy automation workflows directly to an n8n instance.

## Workflow

### 1. Discovery
Before building, always check the available credentials on the n8n instance to ensure the workflow can be authenticated.
- Run `python /home/ubuntu/skills/n8n-automation-builder/scripts/get_credentials.py` to list available credentials.

### 2. Design
Consult the node reference for proper JSON structure and parameter naming.
- Read `/home/ubuntu/skills/n8n-automation-builder/references/nodes_reference.md`.

### 3. Implementation
Generate the workflow JSON. A standard n8n workflow JSON includes:
- `name`: String
- `nodes`: Array of node objects
- `connections`: Object defining the flow between nodes
- `settings`: Object (usually `{"executionOrder": "v1"}`)

### 4. Deployment
Save the workflow JSON to a temporary file and use the deployment script.
- Run `python /home/ubuntu/skills/n8n-automation-builder/scripts/deploy_workflow.py <path_to_json>`

## Node Structure Example
```json
{
  "name": "My Automation",
  "nodes": [
    {
      "parameters": {
        "httpMethod": "POST",
        "path": "my-webhook",
        "options": {}
      },
      "type": "n8n-nodes-base.webhook",
      "typeVersion": 2.1,
      "position": [0, 0],
      "id": "webhook-id",
      "name": "Webhook"
    }
  ],
  "connections": {},
  "settings": {
    "executionOrder": "v1"
  }
}
```

## Best Practices
- **Credential Matching**: Use the `id` from `get_credentials.py` when assigning credentials to nodes.
- **Node Positioning**: Use `position` arrays (e.g., `[250, 0]`) to keep the workflow visually organized in the n8n UI.
- **Testing**: After deployment, provide the user with the webhook URL or instructions on how to trigger the workflow.
