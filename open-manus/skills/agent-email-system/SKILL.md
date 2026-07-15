---
name: agent-email-system
description: Multi-agent email system with Google Service Accounts, comprehensive safeguards (recipient allowlisting, rate limiting, keyword filtering, manual approval queue), and REST API service for secure agent-to-agent email communication.
version: 1.0.0
author: Valentina
category: email
license: MIT
metadata:
  hermes:
    tags: [Email, Gmail, Google Service Account, REST API, Multi-Agent, Security, Safeguards]
    homepage: https://github.com/GarrettRoi/open-manus
    requires_config:
      - GOOGLE_SERVICE_ACCOUNT_JSON
      - AGENT_EMAIL_API_KEY
---

# Agent Email System

A secure, multi-agent email system designed for automated agent-to-agent communication with comprehensive safeguards.

## Overview

This skill provides:
- **Google Service Account** integration for Gmail API access
- **Comprehensive Safeguards** - allowlisting, rate limiting, keyword filtering, manual approval queue
- **REST API Service** for agent-to-agent email communication
- **Audit Logging** - full traceability of all email operations

## Quick Start

```bash
# 1. Set up environment
export GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
export AGENT_EMAIL_API_KEY="your-api-key"

# 2. Start the REST API service
python3 /app/skills/agent-email-system/scripts/email_service.py

# 3. Send an email via API
curl -X POST http://localhost:8080/api/v1/email/send \
  -H "Authorization: Bearer $AGENT_EMAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "recipient@example.com",
    "subject": "Test from Agent",
    "body": "Hello from the agent system!"
  }'
```

## Architecture

```
┌─────────────┐     HTTP/REST      ┌──────────────────┐     Gmail API      ┌─────────────┐
│   Client    │ ─────────────────→ │  Email Service   │ ─────────────────→ │   Gmail     │
│  (Agents)   │                    │  (FastAPI)       │                    │   (Google)  │
└─────────────┘                    └──────────────────┘                    └─────────────┘
                                          │
                                          ↓
                                   ┌──────────────────┐
                                   │  Safeguards      │
                                   │  - Allowlist     │
                                   │  - Rate Limit    │
                                   │  - Keywords      │
                                   │  - Approval      │
                                   └──────────────────┘
```

## Safeguards

### 1. Recipient Allowlisting
Only pre-approved email addresses/domains can receive emails.

### 2. Rate Limiting
Configurable per-sender limits to prevent spam.

### 3. Keyword Filtering
Block emails containing prohibited keywords.

### 4. Manual Approval Queue
Sensitive emails require manual approval before sending.

## Scripts

- `scripts/email_service.py` - Main REST API service (FastAPI)
- `scripts/gmail_client.py` - Gmail API client wrapper
- `scripts/safeguards.py` - Safeguard enforcement module
- `scripts/approval_queue.py` - Manual approval management

## Templates

- `templates/config.yaml` - Service configuration template
- `templates/allowlist.txt` - Recipient allowlist template

## References

- `references/gmail-api-setup.md` - Google Service Account setup guide
- `references/api-reference.md` - REST API endpoint documentation
- `references/security-model.md` - Security architecture details
