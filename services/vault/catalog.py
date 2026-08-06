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
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "auth": {"kind": "header", "header_name": "xi-api-key", "prefix": ""},
        "base_url": "https://api.elevenlabs.io",
        "allowed_hosts": ["api.elevenlabs.io"],
        "setup_help": "Paste an API key from ElevenLabs → Profile → API keys.",
        "example_call": "GET /v1/voices",
    },
    "discord": {
        "label": "Discord API",
        "auth": {"kind": "header", "header_name": "Authorization", "prefix": "Bot "},
        "base_url": "https://discord.com/api/v10",
        "allowed_hosts": ["discord.com"],
        "setup_help": "Paste a bot token from the Discord Developer Portal → Bot.",
        "example_call": "GET /users/@me",
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
    },
    "railway": {
        "label": "Railway",
        "auth": {"kind": "bearer"},
        "base_url": "https://backboard.railway.app/graphql/v2",
        "allowed_hosts": ["backboard.railway.app"],
        "setup_help": "Paste a token from Railway → Account Settings → Tokens.",
        "example_call": 'POST / with GraphQL body {"query": "..."}',
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
    },
}


def get_template(service: str) -> Optional[Dict[str, Any]]:
    return CATALOG.get((service or "").strip().lower())


def oauth_services() -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in CATALOG.items() if v["auth"]["kind"] == "oauth2"}
