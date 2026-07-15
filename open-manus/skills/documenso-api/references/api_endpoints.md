# Documenso API v2 - Complete Endpoint Reference

## Envelope Operations

### Find Envelopes
**Endpoint:** `GET /envelope`

Search and filter envelopes with pagination.

**Query Parameters:**
- `query` (string): Search query
- `page` (number): Page number (starts at 1)
- `perPage` (number): Items per page (1-100)
- `type` (enum): DOCUMENT or TEMPLATE
- `status` (enum): DRAFT, PENDING, COMPLETED, REJECTED
- `folderId` (string): Filter by folder
- `orderByDirection` (enum): asc or desc

### Get Envelope
**Endpoint:** `GET /envelope/{envelopeId}`

Retrieve complete envelope details including recipients, fields, and items.

### Get Audit Logs
**Endpoint:** `GET /envelope/{envelopeId}/audit-log`

Retrieve audit trail for envelope actions.

### Create Envelope
**Endpoint:** `POST /envelope/create`

Create new document or template with PDF files, recipients, and fields.

**Content-Type:** `multipart/form-data`

**Body:**
- `payload` (JSON): Envelope configuration
- `files` (files): PDF files to include

### Use Template
**Endpoint:** `POST /envelope/use`

Generate document from template with optional recipient/file customization.

### Update Envelope
**Endpoint:** `POST /envelope/update`

Update envelope properties.

### Delete Envelope
**Endpoint:** `POST /envelope/delete`

Delete envelope permanently.

### Duplicate Envelope
**Endpoint:** `POST /envelope/duplicate`

Create copy of existing envelope.

### Distribute Envelope
**Endpoint:** `POST /envelope/distribute`

Send envelope to recipients for signing.

### Redistribute Envelope
**Endpoint:** `POST /envelope/redistribute`

Resend envelope notifications to recipients.

## Recipient Operations

### Get Recipient
**Endpoint:** `GET /envelope/recipient/{recipientId}`

Retrieve specific recipient details.

### Add Recipients
**Endpoint:** `POST /envelope/recipient/create-many`

Add multiple recipients to envelope.

**Body:**
```json
{
  "envelopeId": "envelope_xxx",
  "data": [
    {
      "email": "user@example.com",
      "name": "John Doe",
      "role": "SIGNER"
    }
  ]
}
```

### Update Recipients
**Endpoint:** `POST /envelope/recipient/update-many`

Update multiple recipients.

### Delete Recipient
**Endpoint:** `POST /envelope/recipient/delete`

Remove recipient from envelope.

## Field Operations

### Get Field
**Endpoint:** `GET /envelope/field/{fieldId}`

Retrieve specific field details.

### Add Fields
**Endpoint:** `POST /envelope/field/create-many`

Add multiple fields to envelope.

**Body:**
```json
{
  "envelopeId": "envelope_xxx",
  "data": [
    {
      "recipientId": 1,
      "type": "SIGNATURE",
      "page": 1,
      "positionX": 10,
      "positionY": 20,
      "width": 50,
      "height": 20
    }
  ]
}
```

### Update Fields
**Endpoint:** `POST /envelope/field/update-many`

Update multiple fields.

### Delete Field
**Endpoint:** `POST /envelope/field/delete`

Remove field from envelope.

## Item Operations

### Add Items
**Endpoint:** `POST /envelope/item/create-many`

Add PDF files to envelope.

### Update Items
**Endpoint:** `POST /envelope/item/update-many`

Update PDF file properties.

### Delete Item
**Endpoint:** `POST /envelope/item/delete`

Remove PDF file from envelope.

### Download Item
**Endpoint:** `GET /envelope/item/{envelopeItemId}/download`

Download PDF file from envelope.

## Attachment Operations

### Find Attachments
**Endpoint:** `GET /envelope/attachment`

List all attachments for envelope.

**Query Parameters:**
- `envelopeId` (required): Envelope ID
- `token` (optional): Access token

### Create Attachment
**Endpoint:** `POST /envelope/attachment/create`

Add attachment to envelope.

### Update Attachment
**Endpoint:** `POST /envelope/attachment/update`

Update existing attachment.

### Delete Attachment
**Endpoint:** `POST /envelope/attachment/delete`

Remove attachment from envelope.

## Response Codes

- **200**: Success
- **400**: Invalid input data
- **401**: Authorization not provided
- **403**: Insufficient access
- **404**: Not found
- **500**: Internal server error

## Rate Limits

Fair Use policy applies. Specific rate limits not documented but reasonable usage expected.
