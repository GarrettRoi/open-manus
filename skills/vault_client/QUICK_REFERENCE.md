# Vault Quick Reference (v3 — proxy only)

Keys never leave the vault. Call APIs *through* it.

```bash
# STEP ONE when a task needs an external API: check + auto-request if missing
python3 /app/skills/vault_client/vault_client.py ensure SERVICE "why I need it"
#   exit 0 = usable now · exit 2 = request filed, relay the message to the user

# What can I use?
python3 /app/skills/vault_client/vault_client.py list

# Call an API (vault injects the credential)
python3 /app/skills/vault_client/vault_client.py call CONN METHOD PATH [--json '{...}'] [--param k=v]

# Examples
python3 /app/skills/vault_client/vault_client.py call ELEVENLABS GET /v1/voices
python3 /app/skills/vault_client/vault_client.py call OPENAI POST /v1/chat/completions --json '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
python3 /app/skills/vault_client/vault_client.py call GOOGLE GET /gmail/v1/users/me/messages --param maxResults=5
python3 /app/skills/vault_client/vault_client.py call QUO GET /phone-numbers

# Usage notes for a connection
python3 /app/skills/vault_client/vault_client.py skill CONN
```

```python
sys.path.insert(0, "/app/skills/vault_client")
from vault_client import vault
resp = vault.request("OPENAI", "POST", "/v1/chat/completions", json={...})
resp["status"]; resp.get("json") or resp.get("text") or resp.get("body_base64")
```

Gone in v3 (raises an error): `vault.get(...)`, `vault.export_env()`, CLI `get` / `export`.
Errors: 403 = no grant/blocked host · 409 = connection not set up · 502 = upstream down.
