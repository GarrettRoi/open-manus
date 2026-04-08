---
name: google-drive
description: Google Drive, Docs, Sheets, and Slides access for all agents. Use to read, write, share, and organize files in Garrett's vowsok.com Google Drive. Requires gws CLI authentication.
version: 1.0.0
author: Samantha
category: files
metadata:
  hermes:
    tags: [Google Drive, Google Docs, Google Sheets, Files, Documents, Sharing]
    requires_config:
      - GOOGLE_SERVICE_ACCOUNT_JSON
---

# Google Drive Skill

Provides access to Garrett's Google Drive (vowsok.com Google Workspace account) via the `gws` CLI.

**Primary Account:** garrett@vowsok.com (Google Workspace)

## Authentication Setup

This skill uses a Google Service Account with domain-wide delegation.
The service account JSON is stored in the Vault as `GOOGLE_SERVICE_ACCOUNT_JSON`.

To authenticate the `gws` CLI:
```bash
# Export the service account JSON from vault
python3 /app/skills/vault_client/vault_client.py get GOOGLE_SERVICE_ACCOUNT_JSON > /tmp/sa.json

# Configure gws with the service account
gws auth service-account /tmp/sa.json --impersonate garrett@vowsok.com
rm /tmp/sa.json  # Clean up
```

## Quick Reference

### List files in Drive
```bash
gws drive list
gws drive list --folder "Clients"
gws drive search --query "wedding contract"
```

### Read a document
```bash
gws docs read --id "DOCUMENT_ID"
gws docs read --name "Wedding Contract Template"
```

### Create a document
```bash
gws docs create --title "New Document" --content "Content here"
```

### Share a file
```bash
gws drive share --id "FILE_ID" --email "client@example.com" --role reader
```

### Upload a file
```bash
gws drive upload --file /path/to/file.pdf --folder "Contracts"
```

### Work with Sheets
```bash
gws sheets read --id "SHEET_ID" --range "Sheet1!A1:Z100"
gws sheets append --id "SHEET_ID" --range "Sheet1" --values "Name,Email,Date"
```

## Key Folders to Know

| Folder | Purpose |
|--------|---------|
| Clients | Client files and contracts |
| Templates | Document templates for all businesses |
| Marketing | Marketing assets and campaigns |
| Real Estate | McGarry Homes transaction files |
| DJ Business | Vows & Vinyl contracts and playlists |
| Cana | Cana Collective vendor files |

## Who Uses This Skill

- **Samantha** — Document management, organizing files
- **Tatiana** — Real estate transaction documents
- **Jade** — DJ contracts and event files
- **Scarlett** — Client communication records
- **Cora** — Storing and sharing creative assets
- **All agents** — Can read shared files

## Important Notes

1. Always use the service account for programmatic access
2. Never store credentials in plain text files
3. Use the Vault to retrieve the service account JSON
4. The `gws` CLI must be installed: `pip install gws-cli` or check if pre-installed
