---
name: vault_client
description: Zero-exposure API access through the centralized vault. Call external APIs (OpenAI, Google, ElevenLabs, Quo, n8n, ...) via the vault proxy — credentials are injected server-side and never enter your environment.
version: 3.0.0
author: Valentina
license: MIT
metadata:
  hermes:
    tags: [vault, security, api-keys, credentials, proxy, oauth]
    related_skills: [n8n-automation-builder, github-auth]
---

# Vault Client — Zero-Exposure API Access

The vault holds every credential (API keys AND OAuth logins for Google, GitHub, Outlook, ...). You **never** receive a key. Instead you send the API request *through* the vault, which attaches the credential and returns the response.

> ⚠️ **v3 breaking change:** `get` and `export` are gone. `vault.get("KEY")` raises an error. Route calls through `vault.request(...)` instead.

---

## Quick Start

### List services you can call
```bash
python3 /app/skills/vault_client/vault_client.py list
```

### Call an API through the vault
```bash
# GET
python3 /app/skills/vault_client/vault_client.py call ELEVENLABS GET /v1/voices

# POST with a JSON body
python3 /app/skills/vault_client/vault_client.py call OPENAI POST /v1/chat/completions \
  --json '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}'

# Query params
python3 /app/skills/vault_client/vault_client.py call GOOGLE GET /gmail/v1/users/me/messages --param maxResults=10
```

### Usage notes for a connection
```bash
python3 /app/skills/vault_client/vault_client.py skill GOOGLE
```

---

## Python API

```python
import sys
sys.path.insert(0, "/app/skills/vault_client")
from vault_client import vault, VaultError

# What can I call?
for c in vault.list_connections():
    print(c["id"], c["label"], c["example_call"])

# Chat completion via OpenAI
resp = vault.request("OPENAI", "POST", "/v1/chat/completions", json={
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Say hi"}],
})
print(resp["status"])           # upstream HTTP status
print(resp["json"])             # parsed JSON body (or resp["text"] / resp["body_base64"])

# Gmail (OAuth — the vault refreshes tokens automatically)
resp = vault.request("GOOGLE", "GET", "/gmail/v1/users/me/messages",
                     params={"maxResults": 5})

# Binary responses (e.g. ElevenLabs audio) come back base64-encoded
resp = vault.request("ELEVENLABS", "POST", "/v1/text-to-speech/VOICE_ID",
                     json={"text": "hello"})
import base64
audio = base64.b64decode(resp["body_base64"])
```

### Response shape
```python
{
  "status": 200,                  # upstream status code
  "content_type": "application/json",
  "truncated": False,             # True if body exceeded the 10MB cap
  # exactly one of:
  "json": {...},                  # parsed JSON
  "text": "...",                  # text/xml bodies
  "body_base64": "...",           # binary bodies
}
```

### Rules
- `path` is relative to the connection's base URL (`/v1/voices`, not the full URL).
- You cannot set auth headers — the vault injects them and strips any you send.
- Only hosts on the service's allowlist are reachable; everything else is 403.
- Upstream timeout: default 30s, max 120s (`timeout=` argument).
- Every call is audit-logged with your agent name.

### Errors
- `403` — no grant for that connection (ask Garrett/Harmony) or blocked host.
- `409` — connection isn't set up yet (missing key, OAuth login not completed).
- `410` — you used the removed raw-fetch API; switch to `vault.request`.
- `502` — upstream API unreachable.

### Storing a new key (valentina / harmony only)
```python
vault.store("STRIPE", "sk_live_...", service="custom",
            base_url="https://api.stripe.com/v1",
            description="Stripe payments")
```
