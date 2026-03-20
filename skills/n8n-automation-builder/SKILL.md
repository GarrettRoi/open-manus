---
name: n8n-automation-builder
description: Build and deploy n8n workflows from scratch. Use for creating n8n automation workflows, exploring credentials, and deploying to the user's n8n instance.
version: 2.0.0
author: Valentina
license: MIT
metadata:
  hermes:
    tags: [n8n, automation, workflows, integration]
    related_skills: [vault_client]
---

# n8n Automation Builder

This skill enables you to design, build, and deploy automation workflows directly to an n8n instance.

**Status:** ✅ Fully tested and working (March 20, 2026)
**Instance URL:** `https://primary-production-38b8.up.railway.app`

---

## Prerequisites

### 1. Get n8n API Key from Vault

```bash
# List available keys
python3 /app/skills/vault_client/vault_client.py list

# Fetch the n8n API key (name may vary - check list first)
python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API
```

### 2. Set Environment Variables

```bash
export N8N_INSTANCE_URL="https://primary-production-38b8.up.railway.app"
export N8N_API_KEY="$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)"
```

### 3. Test the Connection

```bash
# Quick test
curl -s https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_API_KEY"
```

**Expected:** JSON response with list of workflows.

---

## Workflow Development Process

### Step 1: Discovery

Check available credentials before building:

```bash
# Get key from vault
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)

# List credentials
curl -s https://primary-production-38b8.up.railway.app/api/v1/credentials \
  -H "X-N8N-API-KEY: $N8N_KEY"
```

**Known Credential IDs:**
| Service | ID | Name |
|---------|-----|------|
| Gmail | `3qNKDN7I1OYPhVhz` | Gmail account |
| Twilio | `bkjlCKgkMsRw7dzY` | Twilio account |
| OpenAI | `w6c69SQykBvm99DE` | OpenAI account |
| OpenRouter | `KFnSADDGaPa7RW09` | OpenRouter account |
| Google Calendar | `pKXg9ZaFAalac7QU` | Google Calendar |

### Step 2: Design

Read the node reference:
```bash
cat /app/skills/n8n-automation-builder/references/nodes_reference.md
```

### Step 3: Build

Create workflow JSON with:
- `name`: Workflow name
- `nodes`: Array of node objects
- `connections`: Node flow definition
- `settings`: Usually `{"executionOrder": "v1"}`

### Step 4: Deploy

See deployment methods below.

---

## Deployment Methods

### Method 1: Using the Deploy Script

```bash
# Create workflow JSON file
cat > /tmp/my_workflow.json << 'EOF'
{
  "name": "My Automation",
  "nodes": [...],
  "connections": {...},
  "settings": {"executionOrder": "v1"}
}
EOF

# Set environment and deploy
export N8N_INSTANCE_URL="https://primary-production-38b8.up.railway.app"
export N8N_API_KEY="$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)"

python3 /app/skills/n8n-automation-builder/scripts/deploy_workflow.py /tmp/my_workflow.json
```

### Method 2: Direct API Call with curl

```bash
# Get key from vault
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)

# Deploy workflow
curl -X POST https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/my_workflow.json
```

### Method 3: Using Python requests

```python
import requests
import subprocess

# Get API key from vault
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'NEWEST_N8N_API'],
    capture_output=True, text=True
)
api_key = result.stdout.strip()

api_url = "https://primary-production-38b8.up.railway.app"

headers = {
    "X-N8N-API-KEY": api_key,
    "Content-Type": "application/json",
    "Accept": "application/json"
}

workflow = {
    "name": "My Automation",
    "nodes": [...],
    "connections": {...},
    "settings": {"executionOrder": "v1"}
}

response = requests.post(
    f"{api_url}/api/v1/workflows",
    headers=headers,
    json=workflow
)

if response.status_code in [200, 201]:
    print(f"Created! ID: {response.json().get('id')}")
else:
    print(f"Error: {response.status_code}")
```

---

## Common Node Examples

### Webhook Trigger
```json
{
  "parameters": {
    "httpMethod": "POST",
    "path": "my-webhook",
    "options": {}
  },
  "type": "n8n-nodes-base.webhook",
  "typeVersion": 2.1,
  "position": [0, 0],
  "id": "webhook-1",
  "name": "Webhook"
}
```

### Gmail Send
```json
{
  "parameters": {
    "sendTo": "={{ $json.email }}",
    "subject": "={{ $json.subject }}",
    "message": "={{ $json.body }}",
    "options": {}
  },
  "type": "n8n-nodes-base.gmail",
  "typeVersion": 2.1,
  "position": [250, 0],
  "id": "gmail-1",
  "name": "Send Gmail",
  "credentials": {
    "gmailOAuth2": {
      "id": "3qNKDN7I1OYPhVhz",
      "name": "Gmail account"
    }
  }
}
```

### HTTP Request
```json
{
  "parameters": {
    "method": "POST",
    "url": "https://api.example.com/endpoint",
    "sendBody": true,
    "bodyParameters": {
      "parameters": [
        {"name": "key", "value": "={{ $json.value }}"}
      ]
    }
  },
  "type": "n8n-nodes-base.httpRequest",
  "typeVersion": 4.2,
  "position": [250, 0],
  "id": "http-1",
  "name": "HTTP Request"
}
```

---

## Troubleshooting

### 401 Unauthorized

**Cause:** Invalid API key

**Solution:**
```bash
# Verify key from vault
python3 /app/skills/vault_client/vault_client.py list
python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API

# Test with curl
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)
curl -v https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_KEY" 2>&1 | grep "HTTP"
# Should return: HTTP/2 200
```

### Key Name Variations

Key names in vault may change. Always check:
```bash
python3 /app/skills/vault_client/vault_client.py list | grep -i n8n
```

### Python requests fails but curl works

**Cause:** Python environment issues

**Solution:** Use subprocess to call curl, or write to file first:
```python
import subprocess
result = subprocess.run([
    'curl', '-s', 
    'https://primary-production-38b8.up.railway.app/api/v1/workflows',
    '-H', f'X-N8N-API-KEY: {api_key}'
], capture_output=True, text=True)
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/workflows` | GET | List all workflows |
| `/api/v1/workflows` | POST | Create new workflow |
| `/api/v1/workflows/{id}` | GET | Get workflow by ID |
| `/api/v1/workflows/{id}` | DELETE | Delete workflow |
| `/api/v1/credentials` | GET | List credentials |
| `/api/v1/executions` | GET | Execution history |

---

## Best Practices

1. **Always use the vault** — Never hardcode API keys
2. **Check credentials first** — Verify Gmail/Twilio/etc. exist before using
3. **Test with curl first** — If Python fails, curl helps debug
4. **Use descriptive names** — Include purpose and date
5. **Position nodes visually** — Use `[x, y]` coordinates for clean layouts
6. **Generate unique IDs** — Use `uuid.uuid4().hex[:16]` for node IDs

---

## Testing

Quick validation:
```bash
# Should return workflow count
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)
curl -s https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_KEY" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(f"Found {len(d.get('data',[]))} workflows")"
```

**Current status:** 31 workflows exist, API fully functional.
