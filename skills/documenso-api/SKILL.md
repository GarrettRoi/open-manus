---
name: documenso-api
description: Interact with Documenso API for document signing workflows. Use for creating documents/templates, managing recipients and signature fields, sending documents for signing, tracking signing status, and downloading completed documents. Supports all Documenso API v2 operations including envelopes, recipients, fields, items, and attachments.
---

# Documenso API Skill

Interact with the Documenso API v2 to automate document signing workflows.

## Overview

Documenso is an open-source document signing platform. This skill enables programmatic interaction with the Documenso API for creating, managing, and distributing documents for electronic signature.

## Authentication

Documenso uses API key authentication. The API key is stored in the `DOCUMENSO_API` environment variable.

**API Key Format:** `api_xxxxxxxxxxxxxxxx`

**Authentication Header:**
```
Authorization: api_xxxxxxxxxxxxxxxx
```

**Base URL:** `https://app.documenso.com/api/v2`

## Core Concepts

### Envelopes
Both documents and templates are called "envelopes" in the API.

**Types:**
- `DOCUMENT` - Single-use document for signing
- `TEMPLATE` - Reusable document template

**Status:**
- `DRAFT` - Not yet sent
- `PENDING` - Sent, awaiting signatures
- `COMPLETED` - All signatures collected
- `REJECTED` - Rejected by recipient

### Recipients
Individuals who interact with the document.

**Roles:**
- `SIGNER` - Must sign the document
- `APPROVER` - Must approve the document
- `CC` - Receives copy only
- `VIEWER` - Can view only

### Fields
Interactive elements on the document (signatures, text inputs, dates, etc.).

**Common Types:** SIGNATURE, EMAIL, NAME, DATE, TEXT, NUMBER, CHECKBOX, DROPDOWN, RADIO

Fields use coordinate-based positioning (0-100 scale for X/Y on each page).

## Common Operations

### Create and Send Document

Create a document with PDF files, recipients, and signature fields, then send for signing.

```python
import requests
import os

api_key = os.getenv("DOCUMENSO_API")
url = "https://app.documenso.com/api/v2/envelope/create"

payload = {
    "type": "DOCUMENT",
    "title": "Service Agreement",
    "recipients": [
        {
            "email": "client@example.com",
            "name": "Jane Client",
            "role": "SIGNER",
            "fields": [
                {
                    "identifier": 0,
                    "type": "SIGNATURE",
                    "page": 1,
                    "positionX": 10,
                    "positionY": 80,
                    "width": 40,
                    "height": 15
                }
            ]
        }
    ]
}

files = [("files", open("contract.pdf", "rb"))]
data = {"payload": str(payload).replace("'", '"')}
headers = {"Authorization": api_key}

response = requests.post(url, headers=headers, data=data, files=files)
envelope = response.json()

# Send to recipients
distribute_url = "https://app.documenso.com/api/v2/envelope/distribute"
requests.post(distribute_url, headers=headers, json={"envelopeId": envelope["id"]})
```

### Use Template to Generate Document

Generate a document from an existing template with custom recipient information.

```python
# Get template details first
template_id = "envelope_xxxxxxxxxxxxx"
url = f"https://app.documenso.com/api/v2/envelope/{template_id}"
response = requests.get(url, headers={"Authorization": api_key})
template = response.json()

# Generate document from template
use_url = "https://app.documenso.com/api/v2/envelope/use"
payload = {
    "envelopeId": template_id,
    "recipients": [
        {
            "id": template["recipients"][0]["id"],
            "email": "newclient@example.com",
            "name": "John New Client"
        }
    ],
    "distributeDocument": True  # Send immediately
}

response = requests.post(use_url, headers={"Authorization": api_key}, json=payload)
```

### Track Document Status

Monitor signing progress and retrieve completed documents.

```python
# Find pending documents
url = "https://app.documenso.com/api/v2/envelope"
params = {"type": "DOCUMENT", "status": "PENDING", "page": 1, "perPage": 20}
response = requests.get(url, headers={"Authorization": api_key}, params=params)
documents = response.json()["data"]

# Check specific document
envelope_id = "envelope_xxxxxxxxxxxxx"
url = f"https://app.documenso.com/api/v2/envelope/{envelope_id}"
response = requests.get(url, headers={"Authorization": api_key})
envelope = response.json()

if envelope["status"] == "COMPLETED":
    # Download completed document
    item_id = envelope["envelopeItems"][0]["id"]
    download_url = f"https://app.documenso.com/api/v2/envelope/item/{item_id}/download"
    response = requests.get(download_url, headers={"Authorization": api_key})
    
    with open("completed_document.pdf", "wb") as f:
        f.write(response.content)
```

### Add Fields to Existing Envelope

Add signature and form fields to a draft envelope.

```python
# Get envelope details
url = f"https://app.documenso.com/api/v2/envelope/{envelope_id}"
response = requests.get(url, headers={"Authorization": api_key})
envelope = response.json()

recipient_id = envelope["recipients"][0]["id"]
envelope_item_id = envelope["envelopeItems"][0]["id"]

# Add fields
fields_url = "https://app.documenso.com/api/v2/envelope/field/create-many"
payload = {
    "envelopeId": envelope_id,
    "data": [
        {
            "recipientId": recipient_id,
            "envelopeItemId": envelope_item_id,
            "type": "SIGNATURE",
            "page": 1,
            "positionX": 10,
            "positionY": 80,
            "width": 40,
            "height": 15
        }
    ]
}

response = requests.post(fields_url, headers={"Authorization": api_key}, json=payload)
```

## Using the Python Client

A helper client is provided in `scripts/documenso_client.py` for simplified API interaction.

```python
import sys
sys.path.append("/home/ubuntu/skills/documenso-api/scripts")
from documenso_client import DocumensoClient

client = DocumensoClient()

# Create and send document
envelope = client.create_envelope(
    title="Service Agreement",
    envelope_type="DOCUMENT",
    recipients=[
        {
            "email": "client@example.com",
            "name": "Jane Client",
            "role": "SIGNER",
            "fields": [
                {
                    "identifier": 0,
                    "type": "SIGNATURE",
                    "page": 1,
                    "positionX": 10,
                    "positionY": 80,
                    "width": 40,
                    "height": 15
                }
            ]
        }
    ],
    files=["contract.pdf"]
)

client.distribute_envelope(envelope["id"])

# Use template
document = client.use_template(
    envelope_id="envelope_xxxxxxxxxxxxx",
    recipients=[{"id": 1, "email": "new@example.com", "name": "New Client"}],
    distribute=True
)

# Find completed envelopes
envelopes = client.find_envelopes(status="COMPLETED")
```

## Reference Documentation

For detailed information, read these reference files:

- **`references/api_endpoints.md`** - Complete endpoint reference with all operations
- **`references/field_types.md`** - Field types, positioning, and configuration details
- **`references/workflows.md`** - Step-by-step guides for common workflows

## Key Endpoints

**Envelopes:**
- `GET /envelope` - Find envelopes
- `GET /envelope/{id}` - Get envelope details
- `POST /envelope/create` - Create envelope
- `POST /envelope/use` - Use template
- `POST /envelope/distribute` - Send to recipients

**Recipients:**
- `POST /envelope/recipient/create-many` - Add recipients
- `POST /envelope/recipient/update-many` - Update recipients

**Fields:**
- `POST /envelope/field/create-many` - Add fields
- `POST /envelope/field/update-many` - Update fields

**Items:**
- `GET /envelope/item/{id}/download` - Download PDF

## Error Handling

Common HTTP status codes:
- `200` - Success
- `400` - Invalid input data
- `401` - Authorization not provided or invalid
- `403` - Insufficient access
- `404` - Resource not found
- `500` - Internal server error

Always check response status and handle errors appropriately.

## Best Practices

1. **Always retrieve envelope details** before modifying to get current recipient/field IDs
2. **Use multipart/form-data** when uploading PDF files with envelope creation
3. **Store envelope IDs** returned from create operations for future reference
4. **Check envelope status** before attempting to distribute (must be DRAFT)
5. **Use templates** for recurring document types to avoid recreating structure
6. **Pre-fill fields** when using templates to reduce manual input
7. **Download completed documents** promptly for record-keeping

## Limitations

- API available to individual users, teams, and higher plans (Fair Use applies)
- Rate limits not explicitly documented but reasonable usage expected
- API V1 is deprecated; use V2 for all new integrations
- Envelopes created via API V2 use the new Envelope system (not Legacy Documents)
