# Gmail API Setup Guide

This guide walks you through setting up Google Service Account authentication for the Agent Email System.

## Prerequisites

- A Google Cloud project
- Gmail API enabled
- Google Workspace domain (recommended) or regular Gmail account

## Step 1: Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "New Project" and give it a name
3. Select the project

## Step 2: Enable the Gmail API

1. Navigate to "APIs & Services" > "Library"
2. Search for "Gmail API"
3. Click "Enable"

## Step 3: Create a Service Account

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "Service Account"
3. Fill in the service account name (e.g., "agent-email-system")
4. Click "Create and Continue"
5. Grant the role: "Owner" or create a custom role with Gmail API permissions
6. Click "Done"

## Step 4: Create and Download the Key

1. Click on your new service account
2. Go to the "Keys" tab
3. Click "Add Key" > "Create New Key"
4. Select "JSON" format
5. Click "Create" - the key file will download automatically

## Step 5: Enable Domain-Wide Delegation (Google Workspace)

**Required for sending emails from the service account.**

1. Still in the service account details, click "Advanced Settings"
2. Click "Enable Domain-Wide Delegation"
3. Note the "Client ID" shown
4. Go to [Google Admin Console](https://admin.google.com/) > Security > API Controls
5. Click "Manage Domain-Wide Delegation"
6. Click "Add New"
7. Enter the Client ID from step 3
8. Add these OAuth scopes:
   ```
   https://www.googleapis.com/auth/gmail.send
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/gmail.modify
   ```
9. Click "Authorize"

## Step 6: Configure the Agent Email System

1. Copy the contents of the downloaded JSON key file
2. Set it as an environment variable:
   ```bash
   export GOOGLE_SERVICE_ACCOUNT_JSON='paste-json-here'
   ```
   
   Or for multi-line:
   ```bash
   export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account-key.json)"
   ```

3. Set the API key:
   ```bash
   export AGENT_EMAIL_API_KEY="$(openssl rand -hex 32)"
   ```

4. Add at least one email to the allowlist:
   ```bash
   echo "allowed@example.com" >> /app/skills/agent-email-system/data/allowlist.txt
   ```

## Step 7: Start the Service

```bash
cd /app/skills/agent-email-system
pip install -r requirements.txt
python scripts/email_service.py
```

## Testing

Test the service is working:

```bash
# Health check
curl http://localhost:8080/health

# Check allowlist
curl -H "Authorization: Bearer $AGENT_EMAIL_API_KEY" \
  "http://localhost:8080/api/v1/allowlist/check?email=allowed@example.com"
```

## Troubleshooting

### "Access Denied" when sending emails

- Verify domain-wide delegation is enabled (Google Workspace)
- Check the service account has the correct scopes authorized
- Ensure the "from" email is a valid user in your domain

### "Invalid Credentials"

- Double-check the JSON is properly formatted
- Verify the service account key hasn't expired
- Ensure the Gmail API is enabled

### Rate Limits

Google imposes these Gmail API limits:
- 250 quota units per user per second
- 1,000,000,000 quota units per day for the project

The service includes additional rate limiting - see the config.
