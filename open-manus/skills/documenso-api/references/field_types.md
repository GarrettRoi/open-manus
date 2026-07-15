# Documenso Field Types and Configuration

## Field Types

### SIGNATURE
Signature field where recipient signs the document.

**Required Properties:**
- `type`: "SIGNATURE"
- `recipientId`: Recipient who will sign
- `page`: Page number (1-indexed)
- `positionX`: X coordinate (0-100)
- `positionY`: Y coordinate (0-100)
- `width`: Field width
- `height`: Field height

### EMAIL
Email address field.

**Properties:** Same as SIGNATURE

### NAME
Name field for recipient's name.

**Properties:** Same as SIGNATURE

### DATE
Date field (auto-filled with signing date).

**Properties:** Same as SIGNATURE

### TEXT
Free-text input field.

**Properties:** Same as SIGNATURE plus:
- `label` (optional): Field label
- `placeholder` (optional): Placeholder text
- `value` (optional): Pre-filled value

### NUMBER
Numeric input field.

**Properties:** Same as TEXT

### CHECKBOX
Checkbox field for yes/no options.

**Properties:** Same as TEXT plus:
- `value` (optional): Array of selected options

### DROPDOWN
Dropdown selection field.

**Properties:** Same as TEXT plus:
- `options` (required): Array of available options

### RADIO
Radio button group field.

**Properties:** Same as DROPDOWN

## Field Positioning

Fields use coordinate-based positioning on PDF pages.

### Coordinate System
- **Origin:** Top-left corner of page
- **X-axis:** 0 (left) to 100 (right)
- **Y-axis:** 0 (top) to 100 (bottom)
- **Page:** 1-indexed page number

### Positioning Example
```json
{
  "page": 1,
  "positionX": 10,
  "positionY": 80,
  "width": 40,
  "height": 10
}
```

This places a field:
- On page 1
- 10% from left edge
- 80% from top edge
- 40% page width wide
- 10% page height tall

### Best Practices

**Signature Fields:**
- Width: 40-50 units
- Height: 15-20 units
- Position: Bottom third of page

**Text Fields:**
- Width: 30-60 units (depending on content)
- Height: 8-12 units
- Leave space for labels

**Date Fields:**
- Width: 20-30 units
- Height: 8-10 units
- Usually near signature

## Field Assignment

Fields are assigned to specific recipients using `recipientId`.

### Recipient ID
The `recipientId` is obtained from:
1. Creating recipients via API (returns ID)
2. Retrieving envelope details (includes recipient IDs)

### Multiple Fields per Recipient
A single recipient can have multiple fields across different pages.

```json
{
  "recipientId": 1,
  "fields": [
    {
      "type": "SIGNATURE",
      "page": 1,
      "positionX": 10,
      "positionY": 80,
      "width": 40,
      "height": 15
    },
    {
      "type": "DATE",
      "page": 1,
      "positionX": 60,
      "positionY": 80,
      "width": 25,
      "height": 10
    }
  ]
}
```

## Pre-filling Fields

Fields can be pre-filled when creating documents from templates.

```json
{
  "prefillFields": [
    {
      "id": 21,
      "type": "text",
      "label": "Company Name",
      "value": "Acme Corp"
    },
    {
      "id": 22,
      "type": "number",
      "value": "12345"
    },
    {
      "id": 23,
      "type": "checkbox",
      "value": ["option-1", "option-2"]
    }
  ]
}
```

## Field Identifiers

When adding fields during envelope creation, use `identifier` to link fields to specific PDF files.

**Identifier Options:**
1. File index (0-based): `"identifier": 0`
2. Filename: `"identifier": "contract.pdf"`

### Example with Multiple Files
```json
{
  "recipients": [
    {
      "email": "signer@example.com",
      "name": "John Doe",
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
        },
        {
          "identifier": 1,
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
```

This creates signature fields on page 1 of both the first (index 0) and second (index 1) PDF files.
