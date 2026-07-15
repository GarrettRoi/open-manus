# Documenso API Workflows

## Workflow 1: Create and Send Document

Create a new document with PDF files, add recipients and signature fields, then send for signing.

### Steps

**1. Prepare PDF Files**
Ensure PDF files are accessible on the filesystem.

**2. Create Envelope with Recipients and Fields**
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

files = [
    ("files", open("contract.pdf", "rb"))
]

data = {
    "payload": str(payload).replace("'", '"')
}

headers = {"Authorization": api_key}
response = requests.post(url, headers=headers, data=data, files=files)
envelope = response.json()
envelope_id = envelope["id"]
```

**3. Distribute Envelope**
```python
distribute_url = "https://app.documenso.com/api/v2/envelope/distribute"
response = requests.post(
    distribute_url,
    headers=headers,
    json={"envelopeId": envelope_id}
)
```

## Workflow 2: Use Template to Generate Document

Generate a document from an existing template with custom recipient information.

### Steps

**1. Get Template ID**
Retrieve template ID from Documenso UI or via API.

**2. Retrieve Template Details (Optional)**
```python
url = f"https://app.documenso.com/api/v2/envelope/{template_id}"
response = requests.get(url, headers={"Authorization": api_key})
template = response.json()

# Extract recipient IDs
recipient_ids = [r["id"] for r in template["recipients"]]
```

**3. Generate Document from Template**
```python
use_url = "https://app.documenso.com/api/v2/envelope/use"
payload = {
    "envelopeId": template_id,
    "recipients": [
        {
            "id": recipient_ids[0],
            "email": "newclient@example.com",
            "name": "John New Client"
        }
    ]
}

response = requests.post(use_url, headers={"Authorization": api_key}, json=payload)
new_document = response.json()
```

**4. Distribute Document (Optional)**
Include `"distributeDocument": True` in payload to send immediately, or call distribute endpoint separately.

## Workflow 3: Add Fields to Existing Envelope

Add signature and form fields to an existing draft envelope.

### Steps

**1. Get Envelope Details**
```python
url = f"https://app.documenso.com/api/v2/envelope/{envelope_id}"
response = requests.get(url, headers={"Authorization": api_key})
envelope = response.json()

# Extract recipient ID and envelope item ID
recipient_id = envelope["recipients"][0]["id"]
envelope_item_id = envelope["envelopeItems"][0]["id"]
```

**2. Add Fields**
```python
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
        },
        {
            "recipientId": recipient_id,
            "envelopeItemId": envelope_item_id,
            "type": "DATE",
            "page": 1,
            "positionX": 60,
            "positionY": 80,
            "width": 25,
            "height": 10
        }
    ]
}

response = requests.post(fields_url, headers={"Authorization": api_key}, json=payload)
```

## Workflow 4: Track Document Status

Monitor document signing progress and retrieve completed documents.

### Steps

**1. Find Documents by Status**
```python
url = "https://app.documenso.com/api/v2/envelope"
params = {
    "type": "DOCUMENT",
    "status": "PENDING",
    "page": 1,
    "perPage": 20
}

response = requests.get(url, headers={"Authorization": api_key}, params=params)
documents = response.json()["data"]
```

**2. Check Specific Document**
```python
url = f"https://app.documenso.com/api/v2/envelope/{envelope_id}"
response = requests.get(url, headers={"Authorization": api_key})
envelope = response.json()

status = envelope["status"]  # DRAFT, PENDING, COMPLETED, REJECTED
completed_at = envelope.get("completedAt")

# Check recipient signing status
for recipient in envelope["recipients"]:
    print(f"{recipient['email']}: {recipient['signingStatus']}")
```

**3. Download Completed Document**
```python
if status == "COMPLETED":
    # Get envelope item ID
    item_id = envelope["envelopeItems"][0]["id"]
    
    # Download PDF
    download_url = f"https://app.documenso.com/api/v2/envelope/item/{item_id}/download"
    response = requests.get(download_url, headers={"Authorization": api_key})
    
    with open("completed_document.pdf", "wb") as f:
        f.write(response.content)
```

## Workflow 5: Use Template with Custom Files

Generate document from template but replace PDF files with custom versions.

### Steps

**1. Get Template Details**
```python
url = f"https://app.documenso.com/api/v2/envelope/{template_id}"
response = requests.get(url, headers={"Authorization": api_key})
template = response.json()

# Get envelope item ID to replace
envelope_item_id = template["envelopeItems"][0]["id"]
recipient_id = template["recipients"][0]["id"]
```

**2. Generate Document with Custom File**
```python
use_url = "https://app.documenso.com/api/v2/envelope/use"

payload = {
    "envelopeId": template_id,
    "recipients": [
        {
            "id": recipient_id,
            "email": "client@example.com",
            "name": "Jane Client"
        }
    ],
    "customDocumentData": [
        {
            "identifier": "custom_contract.pdf",
            "envelopeItemId": envelope_item_id
        }
    ]
}

files = [
    ("files", open("custom_contract.pdf", "rb"))
]

data = {"payload": str(payload).replace("'", '"')}
response = requests.post(use_url, headers={"Authorization": api_key}, data=data, files=files)
```

## Workflow 6: Pre-fill Template Fields

Generate document from template with pre-filled field values.

### Steps

**1. Create Document with Pre-filled Fields**
```python
use_url = "https://app.documenso.com/api/v2/envelope/use"

payload = {
    "envelopeId": template_id,
    "recipients": [
        {
            "id": recipient_id,
            "email": "client@example.com",
            "name": "Jane Client"
        }
    ],
    "prefillFields": [
        {
            "id": 21,
            "type": "text",
            "label": "Company Name",
            "value": "Acme Corporation"
        },
        {
            "id": 22,
            "type": "number",
            "label": "Contract Amount",
            "value": "50000"
        }
    ]
}

response = requests.post(use_url, headers={"Authorization": api_key}, json=payload)
```

## Using the Python Client

All workflows above can be simplified using the provided `documenso_client.py` script:

```python
from documenso_client import DocumensoClient

client = DocumensoClient()

# Workflow 1: Create and send document
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

# Workflow 2: Use template
document = client.use_template(
    envelope_id=template_id,
    recipients=[
        {
            "id": recipient_id,
            "email": "newclient@example.com",
            "name": "John New Client"
        }
    ],
    distribute=True
)
```
