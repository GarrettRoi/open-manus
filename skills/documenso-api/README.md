# Documenso API Skill

A comprehensive skill for interacting with the Documenso API v2 to automate document signing workflows.

## What This Skill Does

This skill enables Manus to programmatically interact with Documenso for:

- Creating documents and templates with PDF files
- Managing recipients and their roles (SIGNER, APPROVER, CC, VIEWER)
- Adding and configuring signature and form fields
- Sending documents for electronic signature
- Tracking document signing status
- Downloading completed signed documents
- Using templates to generate documents with custom data

## Files Included

### SKILL.md
Main skill documentation with:
- Authentication setup
- Core concepts (envelopes, recipients, fields)
- Common operations with code examples
- Python client usage
- Best practices and limitations

### scripts/documenso_client.py
Python helper class providing:
- Simplified API interaction
- Authentication handling
- Methods for common operations
- Error handling and response parsing

### references/api_endpoints.md
Complete API endpoint reference organized by:
- Envelope operations
- Recipient operations
- Field operations
- Item operations
- Attachment operations

### references/field_types.md
Detailed documentation on:
- All field types (SIGNATURE, TEXT, DATE, etc.)
- Field positioning and coordinate system
- Field assignment to recipients
- Pre-filling field values
- Field identifiers for multi-file documents

### references/workflows.md
Step-by-step guides for:
- Creating and sending documents
- Using templates to generate documents
- Adding fields to existing envelopes
- Tracking document status
- Using templates with custom files
- Pre-filling template fields

## Usage

The skill is automatically loaded when relevant to the task. Manus will:

1. Read SKILL.md for core workflows and patterns
2. Reference detailed documentation as needed
3. Use the Python client for API interactions
4. Follow best practices for error handling

## Requirements

- Documenso API key (stored in `DOCUMENSO_API` environment variable)
- Python 3.11+ with `requests` library
- PDF files for document creation

## Skill Statistics

- SKILL.md: 299 lines (under 500 line recommendation)
- Total skill package: ~1,227 lines across all files
- Python client: 176 lines
- Reference documentation: 752 lines

## API Version

This skill is built for Documenso API v2 (stable). API v1 is deprecated and not supported.

## License

This skill is created for use with Manus AI agent system.
