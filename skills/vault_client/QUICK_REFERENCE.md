# Vault & n8n Quick Reference

## Vault Client - Essential Commands

```bash
# List keys
python3 /app/skills/vault_client/vault_client.py list

# Get key
python3 /app/skills/vault_client/vault_client.py get KEY_NAME

# Get skill docs
python3 /app/skills/vault_client/vault_client.py skill KEY_NAME
```

## Python Usage (Recommended)

```python
import subprocess

result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'KEY_NAME'],
    capture_output=True, text=True
)
api_key = result.stdout.strip()
```

## n8n API - Essential Commands

```bash
# Set env
export N8N_INSTANCE_URL="https://primary-production-38b8.up.railway.app"
export N8N_API_KEY="$(python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API)"

# List workflows
curl -s $N8N_INSTANCE_URL/api/v1/workflows -H "X-N8N-API-KEY: $N8N_API_KEY"

# Deploy workflow
curl -X POST $N8N_INSTANCE_URL/api/v1/workflows \
  -H "X-N8N-API-KEY: $N8N_API_KEY" \
  -H "Content-Type: application/json" \
  -d @workflow.json
```

## Common Credential IDs

- Gmail: `3qNKDN7I1OYPhVhz`
- Twilio: `bkjlCKgkMsRw7dzY`
- OpenAI: `w6c69SQykBvm99DE`
- OpenRouter: `KFnSADDGaPa7RW09`
- Google Calendar: `pKXg9ZaFAalac7QU`

## Troubleshooting

**401 Unauthorized?**
→ Verify key: `python3 /app/skills/vault_client/vault_client.py get NEWEST_N8N_API`

**VAULT_TOKEN not set?**
→ Use subprocess method instead of direct import

**Key name not found?**
→ Run: `python3 /app/skills/vault_client/vault_client.py list`

---
Full documentation: `skill_view('vault_client')` or `skill_view('n8n-automation-builder')`
