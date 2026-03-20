# n8n Automation - Agent Guide

**Last Updated:** March 20, 2026  
**Author:** Valentina  
**Status:** ✅ Tested and Working

---

## Quick Start

### Step 1: Get API Key from Vault

```bash
# List available n8n keys
python3 /app/skills/vault_client/vault_client.py list

# Fetch the key (name may vary - check list first)
python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API
```

### Step 2: Set Environment Variables

```bash
export N8N_INSTANCE_URL="https://primary-production-38b8.up.railway.app"
export N8N_API_KEY="$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)"
```

### Step 3: Test the API

```bash
# Quick test
curl -s https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_API_KEY"
```

**Expected:** JSON response with list of workflows (31 currently exist).

---

## Working Code Examples

### Example 1: List Workflows (Bash)

```bash
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)

curl -s https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_KEY" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); \
  [print(f"- {w['name']} (Active: {w['active']})") for w in d.get('data',[])][:10]"
```

### Example 2: Deploy Workflow (Python)

```python
import requests
import json
import subprocess

# Get API key from vault
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'NEWEST_N8N_API'],
    capture_output=True, text=True
)
api_key = result.stdout.strip()

api_url = "https://primary-production-38b8.up.railway.app"

# Workflow definition
workflow = {
    "name": "My Gmail Automation",
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
            "id": "webhook-1",
            "name": "Webhook"
        },
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
    ],
    "connections": {
        "Webhook": {
            "main": [[{"node": "Send Gmail", "type": "main", "index": 0}]]
        }
    },
    "settings": {"executionOrder": "v1"}
}

# Deploy
headers = {
    "X-N8N-API-KEY": api_key,
    "Content-Type": "application/json"
}

response = requests.post(
    f"{api_url}/api/v1/workflows",
    headers=headers,
    json=workflow
)

if response.status_code in [200, 201]:
    print(f"✅ Created! ID: {response.json().get('id')}")
else:
    print(f"❌ Error: {response.status_code}")
    print(response.text)
```

### Example 3: Using Deploy Script

```bash
# Create workflow JSON
cat > /tmp/workflow.json << 'EOF'
{
  "name": "Test Automation",
  "nodes": [...],
  "connections": {},
  "settings": {"executionOrder": "v1"}
}
EOF

# Set env vars and deploy
export N8N_INSTANCE_URL="https://primary-production-38b8.up.railway.app"
export N8N_API_KEY="$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)"

python3 /app/skills/n8n-automation-builder/scripts/deploy_workflow.py /tmp/workflow.json
```

---

## Known Credential IDs

Use these IDs when assigning credentials to nodes:

| Service | Credential ID | Name |
|---------|---------------|------|
| Gmail | `3qNKDN7I1OYPhVhz` | Gmail account |
| Twilio | `bkjlCKgkMsRw7dzY` | Twilio account |
| OpenAI | `w6c69SQykBvm99DE` | OpenAI account |
| OpenRouter | `KFnSADDGaPa7RW09` | OpenRouter account |
| Google Calendar | `pKXg9ZaFAalac7QU` | OAuth |

---

## Troubleshooting

### 401 Unauthorized

**Cause:** Invalid or missing API key

**Solution:**
```bash
# Verify key exists in vault
python3 /app/skills/vault_client/vault_client.py list

# Test with curl directly
N8N_KEY=$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)
curl -v https://primary-production-38b8.up.railway.app/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_KEY" 2>&1 | grep "HTTP"

# Should return: HTTP/2 200
```

### Key Name Variations

The n8n API key name in vault may change. Check with:
```bash
python3 /app/skills/vault_client/vault_client.py list | grep -i n8n
```

Current valid names: `NEWEST_N8N_API`, `N8N_API_ON_RAILWAY`

### Python Requests Not Working

If Python `requests` library gives 401 but curl works:

1. Use `subprocess` to call curl instead
2. Or write to file and use `requests` with file
3. Check for hidden characters in the key

---

## Important Notes

1. **ALWAYS use the vault** - Never hardcode API keys
2. **Test with curl first** - If Python fails, curl will help debug
3. **Check credentials exist** - Before creating workflows
4. **Key names may change** - Always run `vault list` first
5. **Instance URL is fixed** - `https://primary-production-38b8.up.railway.app`

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/workflows` | GET | List workflows |
| `/api/v1/workflows` | POST | Create workflow |
| `/api/v1/credentials` | GET | List credentials |
| `/api/v1/executions` | GET | Execution history |

---

## Valentina's Testing Log

**Date:** March 20, 2026

Tests performed:
- ✅ Vault client list: 3 keys accessible
- ✅ Vault fetch n8n key: Success (207 chars)
- ✅ n8n API list workflows: 31 workflows found
- ✅ n8n API create workflow: Gmail automation created (ID: 2dFz4zSMPaWG9Kk0)
- ✅ Credentials verified: Gmail, Twilio, OpenAI all accessible

**Status:** Fully functional. No issues with vault or n8n integration.
