# Agent Guide — Using the Vault (v3, proxy-only)

## What changed

The vault no longer hands out API keys. `vault.get("OPENAI_API_KEY")` and
`vault_client.py get/export` are **gone** — they return an error telling you
to use the proxy. This is deliberate: credentials (including OAuth logins for
Google/GitHub/Outlook) now live only inside the vault.

## Step one of (almost) every task: check the vault

When your plan needs an external API/service, your FIRST move is a vault check —
maybe another agent already helped the user connect it:

```python
result = vault.ensure("elevenlabs", reason="need TTS for a voice message")
if result["usable"]:
    resp = vault.request(result["connection"], "GET", "/v1/voices")
else:
    # A request was filed automatically. Tell the user (result["message"]
    # explains what to do — add a key or complete an OAuth login in the
    # dashboard), then keep working on everything that doesn't need it.
```

- `vault.resolve("google")` — cheap check only: found? granted? ready?
- `vault.request_access("google", reason="...")` — file the request yourself.
- `vault.ensure(...)` — both in one call. Idempotent; safe to call every task.
- Never ask the user to paste an API key into chat. The vault dashboard is the
  only place credentials go.

## The one pattern you need

```python
import sys
sys.path.insert(0, "/app/skills/vault_client")
from vault_client import vault

resp = vault.request(CONNECTION, METHOD, PATH, json=..., params=..., timeout=...)
```

- `CONNECTION` — a name from `vault.list_connections()` (e.g. `OPENAI`, `GOOGLE`, `QUO`, `N8N`, `ELEVENLABS`, `RAILWAY`).
- `PATH` — relative to the service's base URL. `vault.get_skill(CONNECTION)` shows the base URL plus an example call.
- The vault attaches auth (and auto-refreshes OAuth tokens). Any auth header you send is stripped.
- Responses: `resp["status"]` + one of `resp["json"]`, `resp["text"]`, `resp["body_base64"]` (binary, e.g. audio).

## Writing generated code for users

When you generate code that needs an external API, do NOT ask for keys or
read env vars. Generate code that routes through the vault:

```python
resp = vault.request("QUO", "POST", "/messages", json={
    "from": "+15551234567", "to": ["+15557654321"], "content": "Hello!",
})
if resp["status"] >= 400:
    raise RuntimeError(f"Quo API error {resp['status']}: {resp.get('json') or resp.get('text')}")
```

## When something fails

| Error | Meaning | What to do |
|-------|---------|------------|
| 403 no grant | You don't have access to that connection | Ask Garrett/Harmony to grant it in the vault dashboard |
| 403 host not allowed | You tried a URL outside the service's allowlist | Use the service's own API paths |
| 409 | Connection not finished (no key, or OAuth login pending) | Ask Garrett to finish setup in the dashboard |
| 410 | You used the removed raw-fetch API | Switch to `vault.request(...)` |
| 502 | Upstream API down/unreachable | Retry later; report if persistent |

## Storing new keys (valentina / harmony only)

```python
vault.store("SENDGRID", "SG.xxxx", service="custom",
            base_url="https://api.sendgrid.com/v3",
            description="Email sending",
            skill_description="POST /mail/send with the standard SendGrid payload")
```

OAuth services (Google, GitHub, Outlook) can only be added by the admin in
the vault dashboard, because they require a browser login.

## Email mailboxes (IMAP/SMTP connections)

Some connections are classic mailboxes, not HTTP APIs. They show up as
native tools too (e.g. `vault_gmail_personal`) with an `action` parameter:

- `action="search_messages"` — smart search; combine ANY of: `from`, `to`,
  `cc`, `subject`, `text` (keywords), `since`/`before` (YYYY-MM-DD),
  `last_days` (e.g. 20), `unseen` (true/false), `flagged`,
  `min_size_kb`/`max_size_kb`, `has_attachment`, `attachment_name`
  (substring or `*.pdf` wildcard), `folder`, `limit`. Results are newest
  first with attachment names included, plus `total_matches` and `more`.
  On Gmail mailboxes this uses Gmail's native search automatically.
  Examples: `{action: "search_messages", from: "john", last_days: 20}`,
  `{action: "search_messages", attachment_name: "contract", since: "2026-07-01"}`
- `action="list_messages"` (folder, limit, unseen_only) — newest first
- `action="read_message"` (uid from list_messages; result lists attachment filenames)
- `action="download_attachment"` (uid + filename or index, optional save_dir)
  — the vault fetches the attachment and the tool saves it locally
  (default `~/Downloads`) and returns the path so you can read/process it
- `action="send"` (to, subject, body, cc, bcc)
- `action="list_folders"`

Via the skill instead: `vault.email(CONNECTION, action, **kwargs)` maps to
`POST /api/vault/email/{CONNECTION}` with `{"action": "folders|list|search|read|send|attachment", ...}`.
(In Python, pass the sender filter as `from_=...` — it's sent as `"from"`.)
(`attachment` returns `content_b64` — decode and write it to a file yourself.)
The vault logs into the mail servers itself — you never see the password.
Do NOT use `vault.request(...)`/the HTTP proxy on an email connection; it
will refuse and point you here.

## Your tool list is live

Granted connections auto-appear as `vault_<name>` tools (re-synced every few
minutes AND instantly when you call an unknown `vault_*` tool — the runtime
pulls the vault list and registers it before failing). If something seems
missing: `vault(action='refresh')` then `vault(action='list')`.
