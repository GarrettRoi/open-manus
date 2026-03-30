# Agent Email System - Security Model

## Overview

The Agent Email System implements a defense-in-depth approach to email security, with multiple layers of safeguards to prevent abuse and ensure secure agent-to-agent communication.

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Client Request                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 1: API Authentication                                                 │
│  - Bearer token validation                                                   │
│  - API key must be provided in Authorization header                          │
│  - Fails fast if invalid                                                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 2: Recipient Allowlisting                                             │
│  - Only pre-approved recipients can receive emails                           │
│  - Supports exact emails, domains, and wildcards                             │
│  - Configurable via file or API                                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 3: Rate Limiting                                                      │
│  - Per-sender hourly and daily quotas                                        │
│  - Prevents spam and abuse                                                   │
│  - Persistent SQLite storage                                                 │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 4: Content Filtering                                                  │
│  - Blocked keyword detection                                                 │
│  - Prevent accidental credential exposure                                    │
│  - Configurable keyword lists                                                │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 5: Manual Approval Queue                                              │
│  - Optional manual review before sending                                     │
│  - For sensitive or high-risk communications                                 │
│  - Audit trail of approvals/rejections                                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Layer 6: Gmail API                                                          │
│  - Google Service Account authentication                                     │
│  - OAuth 2.0 with domain delegation                                          │
│  - Google's own security and rate limiting                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Safeguard Details

### 1. API Authentication

**Purpose:** Ensure only authorized clients can access the API.

**Implementation:**
- Bearer token authentication via `Authorization: Bearer <token>` header
- Token must match `AGENT_EMAIL_API_KEY` environment variable
- No token = immediate 401 rejection

**Configuration:**
```bash
export AGENT_EMAIL_API_KEY="$(openssl rand -hex 32)"
```

### 2. Recipient Allowlisting

**Purpose:** Prevent emails to unauthorized recipients.

**Implementation:**
- File-based allowlist at `data/allowlist.txt`
- Supports:
  - Exact emails: `user@example.com`
  - Domain wildcards: `*@example.com`
  - Subdomain wildcards: `*.example.com`
- Case-insensitive matching

**Example Allowlist:**
```
# Specific users
alice@company.com
bob@company.com

# Entire domain
*@trusted-partner.com

# Subdomains
*.internal.company.com
```

**Management:**
- Manual: Edit `data/allowlist.txt`
- API: `POST /api/v1/allowlist/emails` and `POST /api/v1/allowlist/domains`

### 3. Rate Limiting

**Purpose:** Prevent abuse through excessive email sending.

**Implementation:**
- SQLite-based persistent counters
- Hourly and daily quotas per sender
- Sender identified by `X-Sender` header or defaults to "api-client"

**Default Limits:**
- 10 emails per hour
- 50 emails per day

**Response Headers (future):**
```
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 7
X-RateLimit-Reset: 3600
```

### 4. Content Filtering

**Purpose:** Block emails containing sensitive information.

**Implementation:**
- Keyword-based detection in subject and body
- Case-insensitive matching
- Configurable via `data/keywords.json`

**Default Blocked Keywords:**
- password
- credit card
- ssn
- bank account
- wire transfer

**Configuration:**
```json
{
  "blocked": [
    "password",
    "credit card",
    "secret key",
    "api key"
  ]
}
```

### 5. Manual Approval Queue

**Purpose:** Provide human oversight for sensitive emails.

**Implementation:**
- SQLite-based queue with persistence
- Emails queued when `require_approval: true` is set
- Admin endpoints to approve/reject
- Audit trail of all decisions

**Workflow:**
1. Client sends email with `require_approval: true`
2. Email is queued, client receives approval ID
3. Admin reviews via `GET /api/v1/approvals/pending`
4. Admin approves via `POST /api/v1/approvals/{id}/approve`
5. Email is sent, audit logged

### 6. Gmail API Security

**Purpose:** Leverage Google's security infrastructure.

**Implementation:**
- Service Account authentication (no user passwords)
- OAuth 2.0 with domain-wide delegation (Workspace)
- Google's anti-spam and rate limiting
- Encrypted transmission (HTTPS/TLS)

**Domain-Wide Delegation:**
Required for sending emails from a service account in Google Workspace.

## Data Storage

### SQLite Databases

All data is stored locally in SQLite databases:

| Database | Purpose | Location |
|----------|---------|----------|
| `rate_limits.db` | Email counters per sender | `data/rate_limits.db` |
| `approval_queue.db` | Pending approval requests | `data/approval_queue.db` |

### File Storage

| File | Purpose | Location |
|------|---------|----------|
| `allowlist.txt` | Approved recipients | `data/allowlist.txt` |
| `keywords.json` | Blocked keywords | `data/keywords.json` |

### Environment Variables

| Variable | Purpose | Sensitivity |
|----------|---------|-------------|
| `AGENT_EMAIL_API_KEY` | API authentication | **HIGH** - Generate strong random value |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Gmail API credentials | **HIGH** - Keep secure, rotate regularly |
| `EMAIL_SERVICE_PORT` | Service port | Low |
| `EMAIL_SERVICE_HOST` | Service bind address | Low |

## Audit Logging

All significant events are logged:

- Email send attempts (success/failure)
- Safeguard blocks with reasons
- Approval queue actions
- Allowlist modifications

Log format:
```
2026-03-30 10:30:00,123 - agent_email - INFO - Email sent: message_id=18f3..., to=recipient@example.com
2026-03-30 10:31:00,456 - agent_email - WARNING - Email blocked by safeguards: Recipient not on allowlist
```

## Security Best Practices

### Deployment

1. **Use strong API keys**
   ```bash
   openssl rand -hex 32
   ```

2. **Restrict network access**
   - Bind to localhost only if no external access needed
   - Use firewall rules to limit access

3. **Secure credentials**
   ```bash
   # Store in environment, not in code
   export AGENT_EMAIL_API_KEY="..."
   export GOOGLE_SERVICE_ACCOUNT_JSON="..."
   ```

4. **Regular key rotation**
   - Rotate `AGENT_EMAIL_API_KEY` monthly
   - Rotate Google Service Account keys quarterly

5. **Monitor logs**
   - Watch for repeated failed authentication attempts
   - Monitor rate limit violations
   - Review approval queue for patterns

### Allowlist Management

1. **Principle of least privilege**
   - Only add necessary recipients
   - Prefer specific emails over domain wildcards

2. **Regular review**
   - Audit allowlist quarterly
   - Remove unused entries

3. **Document additions**
   - Keep a change log
   - Note business justification

### Approval Queue

1. **Review promptly**
   - Set up notifications for new approvals
   - Review within 24 hours

2. **Document decisions**
   - Note reason for approval/rejection
   - Escalate patterns of suspicious requests

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Unauthorized API access | Bearer token authentication |
| Email to wrong recipient | Recipient allowlisting |
| Spam/abuse | Rate limiting |
| Data leakage | Content keyword filtering |
| Sensitive content exposure | Manual approval queue |
| Credential theft | Service Account auth (no passwords) |
| Audit tampering | Immutable SQLite records |

## Compliance Notes

This system can help with:
- **GDPR**: Audit trail for data processing
- **SOX**: Financial controls via approval queue
- **HIPAA**: Access controls and audit logging (config needed)

For regulated environments, additional controls may be needed:
- Encryption at rest for databases
- Access logging for admin endpoints
- Automated retention policies
