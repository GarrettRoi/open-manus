# Vault Client — Secure API Key Access

This skill provides secure access to API keys stored in the centralized Key Vault. You never need to store raw API keys in your environment — instead, fetch them on-demand from the vault.

## Quick Start

### List your available keys
```bash
python3 /app/skills/vault_client/vault_client.py list
```

### Fetch a key value
```bash
python3 /app/skills/vault_client/vault_client.py get OPENAI_API_KEY
```

### Get the skill/tool usage guide for a key
```bash
python3 /app/skills/vault_client/vault_client.py skill OPENAI_API_KEY
```

### Export all your keys as environment variables
```bash
python3 /app/skills/vault_client/vault_client.py export
```

## Using in Python Code

```python
# Import the vault client
import sys
sys.path.insert(0, '/app/skills/vault_client')
from vault_client import vault

# Fetch a key
api_key = vault.get("OPENAI_API_KEY")

# Use it with a library
from openai import OpenAI
client = OpenAI(api_key=api_key)

# List all keys you have access to
keys = vault.list_keys()
for k in keys:
    print(f"{k['key_name']}: {k['description']}")

# Get skill description for how to use a key
info = vault.get_skill("STRIPE_SECRET_KEY")
print(info['skill_description'])

# Export all keys as env vars (useful for tools that read from env)
vault.export_env()
```

## How It Works

1. Your agent has two environment variables: `VAULT_URL` and `VAULT_TOKEN`
2. When you call `vault.get("KEY_NAME")`, it makes an authenticated request to the vault
3. The vault checks if you have a grant for that key
4. If granted, it decrypts and returns the key value
5. If denied, you get a clear error message
6. Every access is logged in the audit trail

## Key Features

- **Cached**: Keys are cached in memory after first fetch (within a session)
- **Audited**: Every fetch is logged — Garrett can see who accessed what and when
- **Permissioned**: You can only access keys that Garrett has explicitly granted to you
- **Skill Docs**: Each key can have a usage guide explaining how to use the API it unlocks

## If You Get "Access Denied"

You don't have a grant for that key. Ask Harmony to request access from Garrett via the vault dashboard.

## If You Get "Cannot Reach Vault"

The vault service may be down or restarting. Wait a minute and try again. If it persists, report to Harmony.
