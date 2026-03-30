# Agent Email System API Reference

Base URL: `http://localhost:8080` (or your configured host/port)

## Authentication

All API endpoints (except `/health`) require Bearer token authentication:

```bash
-H "Authorization: Bearer YOUR_API_KEY"
```

## Endpoints

### Health Check
```
GET /health
```

Returns service health status. No authentication required.

**Response:**
```json
{
  "status": "healthy",
  "gmail_connected": true,
  "timestamp": "2026-03-30T10:30:00",
  "version": "1.0.0"
}
```

---

### Send Email
```
POST /api/v1/email/send
```

Send an email with safeguard checks applied.

**Headers:**
- `Authorization: Bearer YOUR_API_KEY`
- `X-Sender: my-agent` (optional, for rate limiting)

**Request Body:**
```json
{
  "to": "recipient@example.com",
  "subject": "Test Email",
  "body": "Hello from the agent system!",
  "cc": ["cc@example.com"],
  "bcc": ["bcc@example.com"],
  "html": false,
  "require_approval": false
}
```

**Response (Success):**
```json
{
  "success": true,
  "message_id": "18f3...",
  "thread_id": "18f3...",
  "timestamp": "2026-03-30T10:30:00",
  "safeguard_result": {
    "allowed": true,
    "reason": "All safeguards passed"
  }
}
```

**Response (Blocked):**
```json
{
  "success": false,
  "timestamp": "2026-03-30T10:30:00",
  "safeguard_result": {
    "allowed": false,
    "reason": "Recipient not on allowlist"
  },
  "error": "Recipient 'bad@example.com' is not on the allowlist"
}
```

---

### Get Pending Approvals
```
GET /api/v1/approvals/pending
```

List all emails waiting for manual approval.

**Response:**
```json
{
  "approvals": [
    {
      "id": "APV-20260330-103000",
      "to_email": "recipient@example.com",
      "subject": "Important",
      "sender": "api-client",
      "timestamp": "2026-03-30T10:30:00",
      "status": "pending"
    }
  ],
  "count": 1
}
```

---

### Approve Email
```
POST /api/v1/approvals/{approval_id}/approve
```

Approve and send a pending email.

**Headers:**
- `X-Approved-By: admin-name` (optional)

**Response:**
```json
{
  "success": true,
  "message_id": "18f3...",
  "approval_id": "APV-20260330-103000"
}
```

---

### Reject Email
```
POST /api/v1/approvals/{approval_id}/reject
```

Reject a pending approval request.

**Headers:**
- `X-Rejected-By: admin-name` (optional)

**Response:**
```json
{
  "success": true,
  "approval_id": "APV-20260330-103000"
}
```

---

### Get Rate Limit Stats
```
GET /api/v1/stats/{sender}
```

Get email sending statistics for a sender.

**Response:**
```json
{
  "sender": "my-agent",
  "hourly_count": 3,
  "daily_count": 12,
  "hourly_limit": 10,
  "daily_limit": 50,
  "remaining_hourly": 7,
  "remaining_daily": 38
}
```

---

### Add Email to Allowlist
```
POST /api/v1/allowlist/emails?email=new@example.com
```

Add a specific email address to the allowlist.

**Response:**
```json
{
  "success": true,
  "email": "new@example.com"
}
```

---

### Add Domain to Allowlist
```
POST /api/v1/allowlist/domains?domain=example.com
```

Add an entire domain to the allowlist.

**Response:**
```json
{
  "success": true,
  "domain": "example.com"
}
```

---

### Check Allowlist
```
GET /api/v1/allowlist/check?email=test@example.com
```

Check if an email address is on the allowlist.

**Response:**
```json
{
  "email": "test@example.com",
  "allowed": true
}
```

---

### List Inbox
```
GET /api/v1/inbox/list?query=from:example.com&max_results=10
```

List recent emails from the inbox.

**Query Parameters:**
- `query`: Gmail search query (optional)
- `max_results`: Maximum results to return (default: 10)

**Response:**
```json
{
  "messages": [
    {
      "id": "18f3...",
      "thread_id": "18f3...",
      "subject": "Re: Test",
      "from": "sender@example.com",
      "date": "Mon, 30 Mar 2026 10:00:00 GMT",
      "snippet": "Thanks for the message..."
    }
  ],
  "count": 1
}
```

## Error Codes

| HTTP Code | Meaning |
|-----------|---------|
| 401 | Missing authorization header |
| 403 | Invalid API key |
| 404 | Resource not found |
| 500 | Internal server error |
| 503 | Gmail service unavailable |

## Rate Limits

The API enforces these rate limits per sender:
- **10 emails per hour**
- **50 emails per day**

These are configurable in `templates/config.yaml`.
