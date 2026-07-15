---
name: vault_client
description: Secure API key access from the centralized vault. Fetch keys on-demand without storing them in environment variables.
version: 2.0.0
author: Valentina
license: MIT
metadata:
  hermes:
    tags: [vault, security, api-keys, credentials]
    related_skills: [n8n-automation-builder, github-auth]
---

# Vault Client — Secure API Key Access

This skill provides secure access to API keys stored in the centralized Key Vault. You never need to store raw API keys in your environment — instead, fetch them on-demand from the vault.

**Status:** ✅ Fully tested and working (March 20, 2026)

---

## Quick Start

### List your available keys
```bash
python3 /app/skills/vault_client/vault_client.py list
```

### Fetch a key value
```bash
python3 /app/skills/vault_client/vault_client.py get GITHUB_API
```

### Get the skill/tool usage guide for a key
```bash
python3 /app/skills/vault_client/vault_client.py skill GITHUB_API
```

### Export all your keys as environment variables
```bash
python3 /app/skills/vault_client/vault_client.py export
```

---

## Using in Python Code

### Method 1: Subprocess (Recommended - Most Reliable)

Use this method when:
- Running in sandboxed environments
- Environment variables may not be inherited
- You want the simplest, most reliable approach

```python
import subprocess
import os

# Fetch a single key
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'GITHUB_API'],
    capture_output=True, text=True
)

if result.returncode == 0:
    api_key = result.stdout.strip()
    print(f"Key retrieved: {api_key[:20]}...")
else:
    print(f"Error: {result.stderr}")

# List all available keys
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'list'],
    capture_output=True, text=True
)
print(result.stdout)

# Get skill documentation
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'skill', 'N8N_API'],
    capture_output=True, text=True
)
print(result.stdout)
```

### Method 2: Direct Import (When Environment is Set)

Use this method when:
- Running in main agent process (not sandboxed)
- `VAULT_URL` and `VAULT_TOKEN` environment variables are confirmed set
- You need repeated key access (benefits from caching)

```python
import sys
sys.path.insert(0, '/app/skills/vault_client')
from vault_client import vault

# Fetch a key
api_key = vault.get("GITHUB_API")
print(f"Key: {api_key[:20]}...")

# List all keys you have access to
keys = vault.list_keys()
for k in keys:
    print(f"{k['key_name']}: {k['description']}")

# Get skill description for how to use a key
info = vault.get_skill("GITHUB_API")
print(info['skill_description'])

# Export all keys as env vars
vault.export_env()
```

**Note:** Direct import requires `VAULT_URL` and `VAULT_TOKEN` to be set in the environment. If you get "VAULT_TOKEN not set", use Method 1 (subprocess).

---

## Real-World Examples

### Example 1: Using with GitHub API

```python
import subprocess
import requests

# Get GitHub token from vault
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'GITHUB_API'],
    capture_output=True, text=True
)
github_token = result.stdout.strip()

# Use with GitHub API
headers = {"Authorization": f"token {github_token}"}
response = requests.get("https://api.github.com/user/repos", headers=headers)
print(f"Repos: {len(response.json())}")
```

### Example 2: Using with n8n

```python
import subprocess
import requests

# Get n8n key from vault
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'NEWEST_N8N_API'],
    capture_output=True, text=True
)
n8n_key = result.stdout.strip()

# Use with n8n API
headers = {"X-N8N-API-KEY": n8n_key}
response = requests.get(
    "https://primary-production-38b8.up.railway.app/api/v1/workflows",
    headers=headers
)
print(f"Workflows: {len(response.json().get('data', []))}")
```

### Example 3: Bash Script Integration

```bash
#!/bin/bash

# Get key from vault
export GITHUB_API_KEY=$(python3 /app/skills/vault_client/vault_client.py get GITHUB_API)

# Use in subsequent commands
curl -H "Authorization: token $GITHUB_API_KEY"      https://api.github.com/user
```

---

## How It Works

1. **Your agent has two environment variables:** `VAULT_URL` and `VAULT_TOKEN`
2. **When you call `vault.get("KEY_NAME")`,** it makes an authenticated request to the vault
3. **The vault checks if you have a grant for that key**
4. **If granted,** it decrypts and returns the key value
5. **If denied,** you get a clear error message
6. **Every access is logged** in the audit trail

---

## Key Features

- **Cached**: Keys are cached in memory after first fetch (within a session)
- **Audited**: Every fetch is logged — Garrett can see who accessed what and when
- **Permissioned**: You can only access keys that Garrett has explicitly granted to you
- **Skill Docs**: Each key can have a usage guide explaining how to use the API it unlocks

---

## Troubleshooting

### "VAULT_TOKEN not set"

**Cause:** Running Python code in a sandbox/subprocess that doesn't inherit environment variables.

**Solution:** Use the subprocess method:
```python
import subprocess
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'KEY_NAME'],
    capture_output=True, text=True
)
key = result.stdout.strip()
```

### "Access Denied" or 403 Error

**Cause:** You don't have a grant for that key.

**Solution:** Ask Harmony to request access from Garrett via the vault dashboard.

### "Cannot Reach Vault"

**Cause:** The vault service may be down or restarting.

**Solution:** Wait a minute and try again. Check if `VAULT_URL` environment variable is set:
```bash
echo $VAULT_URL
# Should output: https://vault-production-f4c2.up.railway.app
```

### Key Name Not Found

**Cause:** Key names may vary over time.

**Solution:** Always list keys first to see current names:
```bash
python3 /app/skills/vault_client/vault_client.py list
```

### Empty Key Value

**Cause:** Key may exist but value is empty, or there was a retrieval error.

**Solution:** Check error output:
```python
result = subprocess.run([...], capture_output=True, text=True)
if result.returncode != 0:
    print(f"Error: {result.stderr}")
```

---

## Available Keys (Examples)

Key names may change. Always run `vault list` to see current keys.

Common keys found:
- `GITHUB_API` — GitHub personal access token
- `NEWEST_N8N_API` — n8n automation API key
- `N8N_API_ON_RAILWAY` — Legacy n8n key

---

## Environment Variables

These are set automatically for each agent:

```bash
VAULT_URL=https://vault-production-f4c2.up.railway.app
VAULT_TOKEN=<agent-specific-token>
```

**Do not modify these.** Each agent has their own token.

---

## Best Practices

1. **Always use the vault** — Never hardcode API keys in code
2. **Use subprocess for reliability** — Works in all environments
3. **Check key names first** — Run `vault list` before fetching
4. **Handle errors gracefully** — Check return codes and stderr
5. **Cache in memory only** — Don't write keys to disk
6. **Test with CLI first** — If Python fails, test: `python3 /app/skills/vault_client/vault_client.py get KEY_NAME`

---

## Testing Your Setup

Run these commands to verify everything works:

```bash
# Should show available keys
python3 /app/skills/vault_client/vault_client.py list

# Should return key value (not error)
python3 /app/skills/vault_client/vault_client.py get GITHUB_API

# Should return skill documentation
python3 /app/skills/vault_client/vault_client.py skill GITHUB_API
```

If all three work, the vault is functional and ready to use.
