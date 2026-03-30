# Agent Email System

A secure, multi-agent email system with comprehensive safeguards for automated agent-to-agent email communication.

## Features

- **Google Service Account** integration for Gmail API access
- **Comprehensive Safeguards:**
  - Recipient allowlisting
  - Rate limiting (per-sender quotas)
  - Keyword filtering
  - Manual approval queue
- **REST API** for easy integration
- **Audit logging** for all operations

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set up Google Service Account** (see `references/gmail-api-setup.md`)

3. **Configure environment:**
   ```bash
   export GOOGLE_SERVICE_ACCOUNT_JSON='your-service-account-json'
   export AGENT_EMAIL_API_KEY="$(openssl rand -hex 32)"
   ```

4. **Add recipients to allowlist:**
   ```bash
   mkdir -p data
   echo "allowed@example.com" > data/allowlist.txt
   ```

5. **Start the service:**
   ```bash
   python scripts/email_service.py
   ```

6. **Send a test email:**
   ```bash
   curl -X POST http://localhost:8080/api/v1/email/send \
     -H "Authorization: Bearer $AGENT_EMAIL_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "to": "allowed@example.com",
       "subject": "Test",
       "body": "Hello from Agent Email System!"
     }'
   ```

## Documentation

- `references/gmail-api-setup.md` - Google Service Account setup
- `references/api-reference.md` - API endpoint documentation
- `references/security-model.md` - Security architecture and best practices

## Project Structure

```
agent-email-system/
├── scripts/
│   ├── email_service.py    # Main REST API service
│   ├── gmail_client.py     # Gmail API client
│   └── safeguards.py       # Security safeguards
├── templates/
│   ├── config.yaml         # Configuration template
│   └── allowlist.txt       # Recipient allowlist template
├── references/
│   ├── gmail-api-setup.md  # Setup guide
│   ├── api-reference.md    # API docs
│   └── security-model.md   # Security docs
├── requirements.txt        # Python dependencies
└── SKILL.md               # Hermes skill manifest
```

## License

MIT
