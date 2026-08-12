"""Service catalog for the Open Manus Key Vault.

Each template describes how to talk to a service so the vault can inject
credentials server-side (agents never see them):

  auth:
    kind: how the credential is attached to proxied requests
      - "bearer"        Authorization: Bearer <secret>
      - "header"        <header_name>: <prefix><secret>
      - "query"         ?<param_name>=<secret>
      - "oauth2"        Authorization: Bearer <access_token> (auto-refreshed)
  base_url:      default upstream root (instance-specific ones are entered on setup)
  allowed_hosts: hosts the proxy may reach for this service (SSRF guard);
                 the connection's own base_url host is always allowed.
  oauth:         authorize/token endpoints + default scopes (kind == oauth2)
  setup_help:    shown in the dashboard when adding the service
  fields:        extra per-connection inputs (e.g. n8n instance URL)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

CATALOG: Dict[str, Dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "auth": {"kind": "bearer"},
        "base_url": "https://api.openai.com",
        "allowed_hosts": ["api.openai.com"],
        "setup_help": "Paste an API key from https://platform.openai.com/api-keys.",
        "example_call": "POST /v1/chat/completions",
        "test_probe": {"method": "GET", "path": "/v1/models"},
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "auth": {"kind": "header", "header_name": "xi-api-key", "prefix": ""},
        "base_url": "https://api.elevenlabs.io",
        "allowed_hosts": ["api.elevenlabs.io"],
        "setup_help": "Paste an API key from ElevenLabs → Profile → API keys.",
        "example_call": "GET /v1/voices",
        "test_probe": {"method": "GET", "path": "/v1/voices"},
    },
    "discord": {
        "label": "Discord API",
        "auth": {"kind": "header", "header_name": "Authorization", "prefix": "Bot "},
        "base_url": "https://discord.com/api/v10",
        "allowed_hosts": ["discord.com"],
        "setup_help": "Paste a bot token from the Discord Developer Portal → Bot.",
        "example_call": "GET /users/@me",
        "test_probe": {"method": "GET", "path": "/users/@me"},
    },
    "discord_user": {
        "label": "Discord (your account — server discovery only)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://discord.com/api/v10",
        "allowed_hosts": ["discord.com"],
        "oauth": {
            "authorize_url": "https://discord.com/oauth2/authorize",
            "token_url": "https://discord.com/api/v10/oauth2/token",
            "scopes": ["identify", "guilds"],
        },
        "setup_help": (
            "Connect Discord as you, purely to discover which servers you're "
            "in (identify + guilds scopes — Discord never lets a user token "
            "read channel messages). One-time setup: in the Discord Developer "
            "Portal create (or reuse) an application, add the redirect URL "
            "shown below under OAuth2 → Redirects, then paste the client ID "
            "and secret here and click Connect. Pair this with a 'Discord "
            "read-only (reader bot)' connection so the dashboard can show "
            "which of your servers the fleet reader bot covers."
        ),
        "example_call": "GET /users/@me/guilds",
        "test_probe": {"method": "GET", "path": "/users/@me"},
    },
    "discord_read": {
        "label": "Discord read-only (fleet reader bot)",
        "auth": {"kind": "header", "header_name": "Authorization", "prefix": "Bot "},
        "base_url": "https://discord.com/api/v10",
        "allowed_hosts": ["discord.com", "cdn.discordapp.com", "media.discordapp.net"],
        # Proxy-level guard: only GET/HEAD on an explicit read allowlist.
        "read_only": True,
        "fields": [
            {"name": "application_id",
             "label": "Bot application ID (for invite links)",
             "placeholder": "the application's ID from the Developer Portal",
             "required": False},
        ],
        "setup_help": (
            "Read-only reader bot: paste the bot token from the Discord "
            "Developer Portal → Bot. The vault only allows read calls "
            "(list channels, read message history, fetch attachments) — any "
            "write-style call is rejected. The application ID is optional; "
            "it is used to build read-only invite links (View Channels + "
            "Read Message History) so you can add the bot to your servers. "
            "If left blank the vault looks it up from the bot token."
        ),
        "example_call": "GET /channels/{channel_id}/messages",
        "test_probe": {"method": "GET", "path": "/users/@me"},
    },
    "n8n": {
        "label": "n8n",
        "auth": {"kind": "header", "header_name": "X-N8N-API-KEY", "prefix": ""},
        "base_url": "",  # instance-specific
        "allowed_hosts": [],
        "fields": [
            {"name": "base_url", "label": "n8n instance URL",
             "placeholder": "https://your-n8n.up.railway.app/api/v1", "required": True},
        ],
        "setup_help": (
            "Enter your n8n instance URL (usually ends in /api/v1) and an API key "
            "from n8n → Settings → API."
        ),
        "example_call": "GET /workflows",
        "test_probe": {"method": "GET", "path": "/workflows"},
    },
    "quo": {
        "label": "Quo (OpenPhone)",
        "auth": {"kind": "header", "header_name": "Authorization", "prefix": ""},
        "base_url": "https://api.openphone.com/v1",
        "allowed_hosts": ["api.openphone.com"],
        "setup_help": (
            "Paste an API key from Quo (formerly OpenPhone) → Settings → API. "
            "Note: Quo uses the raw key in the Authorization header (no 'Bearer')."
        ),
        "example_call": "GET /phone-numbers",
        "test_probe": {"method": "GET", "path": "/phone-numbers"},
    },
    "railway": {
        "label": "Railway",
        "auth": {"kind": "bearer"},
        "base_url": "https://backboard.railway.com/graphql/v2",
        "allowed_hosts": ["backboard.railway.com"],
        "setup_help": "Paste a token from Railway → Account Settings → Tokens.",
        "example_call": 'POST / with GraphQL body {"query": "..."}',
        "test_probe": {
            "method": "POST",
            "path": "/",
            "json": {"query": "{ me { name } }"},
            # After HTTP response, inspect the GraphQL body:
            # errors[].message containing "Unauthorized"/"Not Authorized" → auth failure.
            # data.me.name present → auth confirmed.
            "response_check": "graphql_auth",
        },
    },
    "google": {
        "label": "Google Workspace (Gmail / Drive / Sheets / Docs / Slides / Forms / Tasks / Chat / People / Calendar)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://www.googleapis.com",
        "allowed_hosts": [
            "www.googleapis.com", "gmail.googleapis.com", "sheets.googleapis.com",
            "drive.googleapis.com", "calendar-json.googleapis.com",
            "people.googleapis.com", "oauth2.googleapis.com",
            "docs.googleapis.com", "slides.googleapis.com", "forms.googleapis.com",
            "tasks.googleapis.com", "chat.googleapis.com",
        ],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/presentations",
                "https://www.googleapis.com/auth/forms.body",
                "https://www.googleapis.com/auth/forms.responses.readonly",
                "https://www.googleapis.com/auth/tasks",
                "https://www.googleapis.com/auth/chat.messages",
                "https://www.googleapis.com/auth/contacts.readonly",
            ],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "One-time setup: in Google Cloud Console create an OAuth client "
            "(type: Web application), add the redirect URL shown below, enable the "
            "Gmail, Drive, Sheets, Docs, Slides, Forms, Tasks, Chat, People, and "
            "Calendar APIs, then paste the client ID and secret here and click "
            "Connect to log in with your Google account. Existing connections must "
            "reconnect once to grant the newer scopes (Docs/Slides/Forms/Tasks/"
            "Chat/Contacts)."
        ),
        "example_call": "GET /gmail/v1/users/me/messages",
        # Probe: Drive "about" endpoint — covered by auth/drive scope (always granted)
        # and hosted on www.googleapis.com which is this connection's base_url.
        # Do NOT use /oauth2/v1/userinfo — that requires openid/email/profile scopes
        # which this connection never requests, causing false 401 "Auth rejected" results.
        "test_probe": {"method": "GET", "path": "/drive/v3/about?fields=user"},
    },
    # ── Individual Google Workspace service connections ──────────────────────
    # Each connection is scoped to one service with its correct base URL and
    # minimal OAuth scope, so agents hit the right endpoint every time.
    # All share the same Google OAuth app (same client ID/secret) but are
    # stored as separate vault connections with dedicated tool names.
    "google_gmail": {
        "label": "Google Gmail (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://gmail.googleapis.com",
        "allowed_hosts": ["gmail.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "Dedicated Gmail connection. Same Google OAuth app as the combined "
            "Google Workspace connection — use the same client ID and secret. "
            "Click Connect to log in and grant Gmail access."
        ),
        "example_call": "vault_<id>_gmail with operation='search'",
        "test_probe": {"method": "GET", "path": "/gmail/v1/users/me/profile"},
    },
    "google_drive": {
        "label": "Google Drive (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://www.googleapis.com",
        "allowed_hosts": ["www.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/drive"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "Dedicated Google Drive connection. Same Google OAuth app as the "
            "combined Google Workspace connection."
        ),
        "example_call": "vault_<id>_drive with operation='search'",
        "test_probe": {"method": "GET", "path": "/drive/v3/about?fields=user"},
    },
    "google_sheets": {
        "label": "Google Sheets (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://sheets.googleapis.com",
        "allowed_hosts": ["sheets.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/spreadsheets"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "Dedicated Google Sheets connection with the correct sheets.googleapis.com "
            "endpoint. Same Google OAuth app as the combined connection."
        ),
        "example_call": "vault_<id>_sheets with operation='get'",
        # No lightweight list endpoint — use the Drive about probe via www.googleapis.com.
        # Sheets v4 has no ping; a 400/404 still proves auth works.
        "test_probe": {"method": "GET", "path": "/v4/spreadsheets"},
    },
    "google_docs": {
        "label": "Google Docs (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://docs.googleapis.com",
        "allowed_hosts": ["docs.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/documents"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google Docs connection.",
        "example_call": "vault_<id>_docs with operation='get'",
        "test_probe": {"method": "GET", "path": "/v1/documents"},
    },
    "google_slides": {
        "label": "Google Slides (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://slides.googleapis.com",
        "allowed_hosts": ["slides.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/presentations"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google Slides connection.",
        "example_call": "vault_<id>_slides with operation='get'",
        "test_probe": {"method": "GET", "path": "/v1/presentations"},
    },
    "google_forms": {
        "label": "Google Forms (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://forms.googleapis.com",
        "allowed_hosts": ["forms.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/forms.body",
                "https://www.googleapis.com/auth/forms.responses.readonly",
            ],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google Forms connection.",
        "example_call": "vault_<id>_forms with operation='get'",
        "test_probe": {"method": "GET", "path": "/v1/forms"},
    },
    "google_calendar": {
        "label": "Google Calendar (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://www.googleapis.com",
        "allowed_hosts": ["www.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google Calendar connection.",
        "example_call": "vault_<id>_calendar with operation='events'",
        "test_probe": {"method": "GET", "path": "/calendar/v3/users/me/calendarList"},
    },
    "google_tasks": {
        "label": "Google Tasks (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://tasks.googleapis.com",
        "allowed_hosts": ["tasks.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/tasks"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google Tasks connection.",
        "example_call": "vault_<id>_tasks with operation='lists'",
        "test_probe": {"method": "GET", "path": "/tasks/v1/users/@me/lists"},
    },
    "google_people": {
        "label": "Google People / Contacts (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://people.googleapis.com",
        "allowed_hosts": ["people.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/contacts.readonly"],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": "Dedicated Google People / Contacts connection.",
        "example_call": "vault_<id>_people with operation='contacts'",
        "test_probe": {"method": "GET", "path": "/v1/people/me?personFields=names"},
    },
    "google_meet": {
        "label": "Google Meet (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://meet.googleapis.com",
        "allowed_hosts": ["meet.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/meetings.space.created",
                "https://www.googleapis.com/auth/meetings.space.readonly",
            ],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "Dedicated Google Meet connection. Enable the Google Meet REST API "
            "in Google Cloud Console, then connect with the same OAuth app."
        ),
        "example_call": "vault_<id>_meet with operation='spaces'",
        "test_probe": {"method": "GET", "path": "/v2/spaces"},
    },
    "google_app_script": {
        "label": "Google Apps Script (dedicated)",
        "auth": {"kind": "oauth2"},
        "base_url": "https://script.googleapis.com",
        "allowed_hosts": ["script.googleapis.com", "oauth2.googleapis.com"],
        "oauth": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/script.projects",
                "https://www.googleapis.com/auth/script.projects.readonly",
            ],
            "extra_authorize_params": {"access_type": "offline", "prompt": "consent"},
        },
        "setup_help": (
            "Dedicated Google Apps Script connection. Enable the Apps Script API "
            "in Google Cloud Console."
        ),
        "example_call": "vault_<id>_app_script with operation='list'",
        "test_probe": {"method": "GET", "path": "/v1/projects"},
    },
    "github": {
        "label": "GitHub",
        "auth": {"kind": "oauth2"},
        "base_url": "https://api.github.com",
        "allowed_hosts": ["api.github.com", "uploads.github.com"],
        "oauth": {
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["repo", "read:org", "gist", "workflow"],
            "no_refresh": True,  # classic GitHub OAuth tokens don't expire
        },
        "setup_help": (
            "One-time setup: create an OAuth App at github.com/settings/developers, "
            "set the callback to the redirect URL shown below, then paste the client "
            "ID and secret and click Connect. (You can also add GitHub as a plain "
            "API-key service using a personal access token — pick 'Custom'.)"
        ),
        "example_call": "GET /user/repos",
        "test_probe": {"method": "GET", "path": "/user"},
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "auth": {"kind": "oauth2"},
        "base_url": "https://graph.microsoft.com/v1.0",
        "allowed_hosts": ["graph.microsoft.com", "login.microsoftonline.com"],
        "oauth": {
            "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "scopes": [
                "offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send",
                "Calendars.ReadWrite", "Files.ReadWrite",
            ],
        },
        "setup_help": (
            "One-time setup: register an app in Azure Portal → App registrations "
            "(supported accounts: any org + personal), add the redirect URL shown "
            "below as a Web platform, create a client secret, then paste the "
            "application (client) ID and secret here and click Connect."
        ),
        "example_call": "GET /me/messages",
        "test_probe": {"method": "GET", "path": "/me"},
    },
    "apple": {
        "label": "Apple iCloud (Calendar / Reminders / Contacts)",
        "auth": {"kind": "apple"},
        "base_url": "https://caldav.icloud.com",
        "allowed_hosts": ["caldav.icloud.com", "contacts.icloud.com"],
        "fields": [
            {"name": "apple_id", "label": "Apple ID email",
             "placeholder": "you@example.com", "required": True},
        ],
        "setup_help": (
            "Use your Apple ID email and an app-specific password. Generate the "
            "password at https://appleid.apple.com/ under Sign-In and Security → "
            "App-Specific Passwords. The password is stored encrypted and is never "
            "shown to agents."
        ),
        "example_call": "Use the Apple tool for calendar, reminders, and contacts",
        # No HTTP probe — uses CalDAV/CardDAV protocols via apple_ops.py
    },
    "bluebubbles": {
        "label": "BlueBubbles iMessage bridge",
        "auth": {"kind": "header", "header_name": "password", "prefix": ""},
        "base_url": "",
        "allowed_hosts": [],
        "fields": [
            {"name": "base_url", "label": "BlueBubbles server URL",
             "placeholder": "https://your-mac.example.com", "required": True},
        ],
        "setup_help": (
            "Optional iMessage bridge. Install BlueBubbles on an always-on Mac, "
            "enable its API, then enter the server URL and API password. iMessage "
            "cannot work without a Mac bridge; the vault only proxies BlueBubbles "
            "HTTP calls and never stores message history."
        ),
        "example_call": "GET /api/v1/health",
        "test_probe": {"method": "GET", "path": "/api/v1/ping"},
    },
    "custom_oauth": {
        "label": "Custom (any OAuth app)",
        "auth": {"kind": "oauth2"},
        "base_url": "",
        "allowed_hosts": [],
        "custom_oauth": True,
        "fields": [
            {"name": "base_url", "label": "API base URL",
             "placeholder": "https://api.example.com/v1", "required": True},
            {"name": "authorize_url", "label": "Authorization URL",
             "placeholder": "https://example.com/oauth/authorize", "required": True},
            {"name": "token_url", "label": "Token URL",
             "placeholder": "https://example.com/oauth/token", "required": True},
            {"name": "scopes", "label": "Scopes (space or comma separated)",
             "placeholder": "read write offline_access", "required": False},
        ],
        "setup_help": (
            "Connect any app that supports OAuth 2.0 (authorization code flow). "
            "In the app's developer settings, create an OAuth app/client, add the "
            "redirect URL shown below, then paste its authorize URL, token URL, "
            "scopes, API base URL, and the client ID + secret here."
        ),
        "example_call": "GET /whatever/the/api/offers",
        # Generic fallback — GET / against the configured base URL
        "test_probe": {"method": "GET", "path": "/"},
    },
    "macincloud": {
        "label": "MACinCloud (Mac desktop via SSH/VNC)",
        "auth": {"kind": "macincloud"},
        "base_url": "",
        "allowed_hosts": [],
        "fields": [
            {"name": "ssh_host", "label": "Hostname / IP",
             "placeholder": "mXXX.macincloud.com or 1.2.3.4", "required": True},
            {"name": "ssh_user", "label": "SSH username",
             "placeholder": "your MACinCloud username", "required": True},
            {"name": "vnc_port", "label": "VNC port",
             "placeholder": "5900", "required": False},
            {"name": "ssh_port", "label": "SSH port",
             "placeholder": "22", "required": False},
        ],
        "setup_help": (
            "Store your MACinCloud VM credentials. SSH username and password are used for "
            "programmatic control (screenshots, commands, browser). VNC password is used "
            "for the interactive desktop viewer (/vnc page). "
            "Find your hostname in the MACinCloud dashboard (e.g. m100.macincloud.com). "
            "All credentials are stored encrypted and agents never see them."
        ),
        "example_call": "Use the Mac tool for screenshot, open_browser, run_command, applescript",
        # No HTTP probe — uses SSH/VNC protocols via mac_ops.py
    },
    "email": {
        "label": "Email (IMAP/SMTP)",
        "auth": {"kind": "email"},
        "base_url": "",
        "allowed_hosts": [],
        "fields": [
            {"name": "username", "label": "Email address",
             "placeholder": "you@gmail.com", "required": True},
            {"name": "imap_host", "label": "IMAP server (incoming)",
             "placeholder": "imap.gmail.com", "required": True},
            {"name": "imap_port", "label": "IMAP port",
             "placeholder": "993", "required": False},
            {"name": "smtp_host", "label": "SMTP server (outgoing)",
             "placeholder": "smtp.gmail.com", "required": True},
            {"name": "smtp_port", "label": "SMTP port",
             "placeholder": "587", "required": False},
        ],
        "setup_help": (
            "Connect any mailbox the classic way — no API or OAuth app needed. "
            "For iCloud Mail use imap.mail.me.com / smtp.mail.me.com with an "
            "app-specific password from appleid.apple.com. "
            "Enter the address, the IMAP/SMTP servers, and the password. "
            "Gmail: imap.gmail.com / smtp.gmail.com with an App Password "
            "(Google Account → Security → 2-Step Verification → App passwords). "
            "Outlook/Hotmail: outlook.office365.com / smtp-mail.outlook.com. "
            "Yahoo: imap.mail.yahoo.com / smtp.mail.yahoo.com (app password). "
            "iCloud: imap.mail.me.com / smtp.mail.me.com (app-specific password). "
            "Ports default to 993 (IMAP) and 587 (SMTP)."
        ),
        "example_call": (
            'POST /api/vault/email/{conn} with {"action": "list", "limit": 10} '
            "(actions: folders, list, read, send)"
        ),
        # No HTTP probe — uses IMAP/SMTP protocols via email_ops.py
    },
    "vowsok": {
        "label": "Vowsok",
        "auth": {"kind": "mcp_bearer"},
        "is_mcp": True,
        "base_url": "https://app.vowsok.com/api/mcp",
        "allowed_hosts": ["app.vowsok.com"],
        "setup_help": (
            "Connect to Vowsok via MCP. Paste your Vowsok MCP bearer token below. "
            "After saving, the vault will call tools/list and register each Vowsok "
            "tool as a native vault_<name>_<tool> tool that agents can call directly. "
            "Get your token from the Vowsok dashboard → API / Integrations."
        ),
        "example_call": "Use the native vault_<name>_<tool> tools registered from Vowsok",
        # MCP kind: test uses tools/list — no test_probe needed
    },
    "mcp_bearer": {
        "label": "Custom MCP (bearer token)",
        "auth": {"kind": "mcp_bearer"},
        "is_mcp": True,
        "base_url": "",
        "allowed_hosts": [],
        "fields": [
            {"name": "base_url", "label": "MCP server URL",
             "placeholder": "https://your-mcp-server.com/api/mcp", "required": True},
        ],
        "setup_help": (
            "Connect any MCP server that accepts a static bearer token. "
            "Enter the full MCP endpoint URL and your bearer token. "
            "The vault will call tools/list and register each remote tool as a "
            "native vault_<name>_<tool> that agents can call without seeing credentials. "
            "Only HTTPS endpoints are accepted."
        ),
        "example_call": "Use the native vault_<name>_<tool> tools registered from the MCP server",
        # MCP kind: test uses tools/list — no test_probe needed
    },
    "alpaca": {
        "label": "Alpaca Markets",
        "auth": {"kind": "header", "header_name": "APCA-API-KEY-ID", "prefix": ""},
        "base_url": "https://paper-api.alpaca.markets",
        "allowed_hosts": ["paper-api.alpaca.markets", "api.alpaca.markets"],
        "extra_secret": True,
        "setup_help": (
            "Paste your Alpaca API Key ID and Secret Key. "
            "Choose Paper (paper-api.alpaca.markets) for sandbox/testing or "
            "Live (api.alpaca.markets) for real-money trading. "
            "Generate keys at https://app.alpaca.markets → API Keys. "
            "Both keys are stored encrypted; agents never see them."
        ),
        "example_call": "GET /v2/account",
        "test_probe": {"method": "GET", "path": "/v2/account"},
    },
    "stripe": {
        "label": "Stripe",
        "auth": {"kind": "bearer"},
        "base_url": "https://api.stripe.com",
        "allowed_hosts": ["api.stripe.com"],
        "setup_help": (
            "Paste a secret key from https://dashboard.stripe.com/apikeys "
            "(starts with sk_live_ for real payments or sk_test_ for test mode). "
            "The key is stored encrypted; agents call Stripe through the vault "
            "proxy and never see it."
        ),
        "example_call": "GET /v1/balance",
        "test_probe": {"method": "GET", "path": "/v1/balance"},
    },
    "plaid": {
        "label": "Plaid (bank & card monitoring)",
        "auth": {"kind": "header", "header_name": "PLAID-CLIENT-ID", "prefix": ""},
        "base_url": "https://production.plaid.com",
        "allowed_hosts": ["production.plaid.com", "sandbox.plaid.com"],
        "extra_secret": True,
        "extra_secret_header": "PLAID-SECRET",
        "extra_secret_label": "Plaid secret (never shown to agents)",
        "api_key_label": "Plaid client ID (never shown to agents)",
        "setup_help": (
            "Paste your Plaid client_id and secret from "
            "https://dashboard.plaid.com/developers/keys. "
            "Use the Production secret for real bank/card data or the Sandbox "
            "secret (with base URL https://sandbox.plaid.com) for testing. "
            "Both values are stored encrypted; the vault proxy injects them "
            "into the JSON request body as client_id/secret (and as the "
            "PLAID-CLIENT-ID / PLAID-SECRET headers) — omit them from your "
            "request bodies."
        ),
        "example_call": (
            'POST /accounts/balance/get with {"access_token": "<item access token>"}'
        ),
        "test_probe": {
            "method": "POST",
            "path": "/institutions/get",
            "json": {"count": 1, "offset": 0, "country_codes": ["US"]},
        },
    },
    "custom": {
        "label": "Custom (any API)",
        "auth": {"kind": "header", "header_name": "Authorization", "prefix": "Bearer "},
        "base_url": "",
        "allowed_hosts": [],
        "fields": [
            {"name": "base_url", "label": "API base URL",
             "placeholder": "https://api.example.com/v1", "required": True},
            {"name": "header_name", "label": "Auth header name",
             "placeholder": "Authorization", "required": False},
            {"name": "prefix", "label": "Value prefix (e.g. 'Bearer ')",
             "placeholder": "Bearer ", "required": False},
        ],
        "setup_help": (
            "Any API-key-based service: give the base URL, how the key is sent "
            "(header name + optional prefix), and the key itself."
        ),
        "example_call": "GET /whatever/the/api/offers",
        # Generic fallback — GET / against the configured base URL
        "test_probe": {"method": "GET", "path": "/"},
    },
}


def get_template(service: str) -> Optional[Dict[str, Any]]:
    return CATALOG.get((service or "").strip().lower())


def oauth_services() -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in CATALOG.items() if v["auth"]["kind"] == "oauth2"}
