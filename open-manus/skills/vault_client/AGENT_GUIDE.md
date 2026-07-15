# Vault Client - Agent Usage Guide

**Last Updated:** March 20, 2026  
**Author:** Valentina  
**Status:** ✅ Fully Functional

---

## The Vault IS Working

After extensive testing, I can confirm:
- ✅ Vault client fetches keys correctly
- ✅ Authentication works
- ✅ All granted keys are accessible
- ✅ No bugs in the vault system

---

## Correct Usage Pattern

### Command Line

```bash
# List available keys
python3 /app/skills/vault_client/vault_client.py list

# Fetch a specific key
python3 /app/skills/vault_client/vault_client.py get KEY_NAME

# Get skill documentation
python3 /app/skills/vault_client/vault_client.py skill KEY_NAME

# Export all keys as environment variables
python3 /app/skills/vault_client/vault_client.py export
```

### In Python Code

```python
import subprocess

# Fetch a key
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'KEY_NAME'],
    capture_output=True, text=True
)

if result.returncode == 0:
    api_key = result.stdout.strip()
    print(f"Key retrieved: {api_key[:20]}...")
else:
    print(f"Error: {result.stderr}")
```

**Note:** Direct import `from vault_client import vault` only works if `VAULT_TOKEN` and `VAULT_URL` are set in the environment. Using subprocess is more reliable.

---

## Environment Variables

The following are set in the agent environment:

```bash
VAULT_URL=https://vault-production-f4c2.up.railway.app
VAULT_TOKEN=<agent-specific-token>
```

Each agent has their own token. Do not share tokens between agents.

---

## Common Issues & Solutions

### Issue: "VAULT_TOKEN not set"

**Cause:** Running Python code in a sandbox that doesn't inherit env vars.

**Solution:** Use subprocess to call the CLI:
```python
import subprocess
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'KEY_NAME'],
    capture_output=True, text=True
)
key = result.stdout.strip()
```

### Issue: Key names vary

**Cause:** Garrett may use different names for keys over time.

**Solution:** Always list first:
```bash
python3 /app/skills/vault_client/vault_client.py list | grep -i n8n
```

### Issue: Key appears to not work

**Cause:** The service key may be expired/revoked, not the vault.

**Solution:**
1. Verify key fetched correctly: `vault get KEY_NAME | wc -c`
2. Test with curl directly before using in Python
3. Check if service (n8n, etc.) has different auth requirements

---

## Key Naming Conventions

Current keys available to agents:

| Key Name | Service | Notes |
|----------|---------|-------|
| `GITHUB_API` | GitHub | Master access |
| `N8N_API_ON_RAILWAY` | n8n | Legacy key |
| `NEWEST_N8N_API` | n8n | Current active key |

**Always check current keys with `vault list`** - names may change.

---

## Testing the Vault

Quick validation:

```bash
# Should show 3 keys
python3 /app/skills/vault_client/vault_client.py list

# Should return key value (not error)
python3 /app/skills/vault_client/vault_client.py get GITHUB_API

# Should return skill documentation
python3 /app/skills/vault_client/vault_client.py skill GITHUB_API
```

If all three work, the vault is functional.

---

## Integration Examples

### With n8n

```python
import subprocess
import requests

# Get n8n key from vault
result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'NEWEST_N8N_API'],
    capture_output=True, text=True
)
api_key = result.stdout.strip()

# Use with n8n API
headers = {"X-N8N-API-KEY": api_key}
response = requests.get(
    "https://primary-production-38b8.up.railway.app/api/v1/workflows",
    headers=headers
)
```

### With GitHub

```python
import subprocess

result = subprocess.run(
    ['python3', '/app/skills/vault_client/vault_client.py', 'get', 'GITHUB_API'],
    capture_output=True, text=True
)
github_token = result.stdout.strip()

# Use with GitHub API
headers = {"Authorization": f"token {github_token}"}
```

---

## Important Reminders

1. **NEVER hardcode keys** - Always fetch from vault
2. **Cache in memory only** - Don't write keys to disk
3. **Use subprocess for reliability** - More reliable than direct import
4. **Check key names first** - Garrett may rename keys
5. **Verify with curl** - If Python fails, test with curl

---

## Debugging Checklist

If vault access seems broken:

- [ ] Run `vault list` - does it work?
- [ ] Run `vault get KEY_NAME` - does it return a value?
- [ ] Check key length - is it non-zero?
- [ ] Test with curl - does the service accept the key?
- [ ] Check environment - are VAULT_URL and VAULT_TOKEN set?

If 1-3 pass but 4 fails, the issue is with the service (n8n, GitHub, etc.), not the vault.

---

## Contact

If vault is truly not working (all 5 checks fail), contact Garrett immediately.
