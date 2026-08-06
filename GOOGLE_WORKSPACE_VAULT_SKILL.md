# Google Workspace via the Secure Vault

Use this skill whenever you need to work with a user's Google Workspace
account: Gmail, Drive, Sheets, Docs, Slides, Forms, Tasks, Chat, People, or
Calendar.

## Core security rule

Never ask the user for a Google access token, refresh token, API key, client
secret, or password. Never print, store, or attempt to handle OAuth
credentials.

Google credentials are managed by the vault. The vault checks the agent's
grant, refreshes OAuth tokens when necessary, calls Google, and returns only
the API result.

## Before using a Google tool

1. Confirm that the user has connected a Google OAuth account in the vault.
2. Confirm that this agent has been granted access to that connection.
3. If the desired product is Docs, Slides, Forms, Tasks, Chat, or People and
   the connection was created before those permissions were added, tell the
   user that the Google connection must be reconnected once to grant the new
   scopes.
4. Use the dedicated product tool rather than the generic HTTP proxy.
5. Ask for confirmation before destructive or externally visible actions,
   such as sending email, deleting Drive files, deleting tasks, deleting
   calendar events, or sending Chat messages, unless the user has already
   explicitly authorized that exact action.

## Available native tools

Each granted Google connection creates one native tool per Google product.
The connection ID is normalized to lowercase: punctuation becomes
underscores.

For a connection named `GOOGLE_LEXI`, the tools are normally:

```text
vault_google_lexi_gmail
vault_google_lexi_drive
vault_google_lexi_sheets
vault_google_lexi_docs
vault_google_lexi_slides
vault_google_lexi_forms
vault_google_lexi_tasks
vault_google_lexi_chat
vault_google_lexi_people
vault_google_lexi_calendar
```

Your actual connection ID may differ. Inspect the live tool list rather than
inventing a connection name.

## Native tool call format

Every dedicated Google tool takes:

```json
{
  "operation": "<operation name>",
  "args": {
    "<operation-specific argument>": "<value>"
  }
}
```

Put all operation-specific inputs inside `args`. Do not put them beside
`operation`.

Example:

```json
{
  "operation": "get",
  "args": {
    "spreadsheet_id": "1abc123",
    "range": "Sheet1!A1:C20"
  }
}
```

## Gmail

Tool suffix: `_gmail`

### Search messages

```json
{
  "operation": "search",
  "args": {
    "q": "from:client@example.com newer_than:7d",
    "limit": 10
  }
}
```

`q` uses Gmail search syntax. `limit` is optional.

### Read a message

```json
{
  "operation": "read",
  "args": {
    "id": "MESSAGE_ID",
    "format": "full"
  }
}
```

### Send an email

Ask for confirmation unless the user already authorized this exact email.

```json
{
  "operation": "send",
  "args": {
    "to": "client@example.com",
    "cc": "manager@example.com",
    "bcc": "",
    "subject": "Project update",
    "body": "The latest report is ready."
  }
}
```

### Modify labels

```json
{
  "operation": "modify",
  "args": {
    "id": "MESSAGE_ID",
    "add_labels": ["STARRED"],
    "remove_labels": ["UNREAD"]
  }
}
```

### List Gmail labels

```json
{
  "operation": "labels",
  "args": {}
}
```

Supported Gmail operations:

```text
search, read, send, modify, labels
```

## Google Drive

Tool suffix: `_drive`

### Search files

Use either a Google Drive query or a simple name filter.

```json
{
  "operation": "search",
  "args": {
    "name_contains": "quarterly report",
    "limit": 10
  }
}
```

```json
{
  "operation": "search",
  "args": {
    "q": "mimeType = 'application/pdf' and trashed = false",
    "limit": 25
  }
}
```

### Get file metadata

```json
{
  "operation": "get",
  "args": {
    "file_id": "FILE_ID"
  }
}
```

### Download file content

```json
{
  "operation": "download",
  "args": {
    "file_id": "FILE_ID"
  }
}
```

Binary responses may be returned as `body_base64`. Decode them only when
needed and write the result to a suitable file.

### Export a Google document

```json
{
  "operation": "export",
  "args": {
    "file_id": "DOCUMENT_ID",
    "mime_type": "application/pdf"
  }
}
```

### Create a folder

```json
{
  "operation": "create_folder",
  "args": {
    "name": "Project reports",
    "parent_id": "PARENT_FOLDER_ID"
  }
}
```

`parent_id` is optional.

### Delete a file

Ask for confirmation before deleting.

```json
{
  "operation": "delete",
  "args": {
    "file_id": "FILE_ID"
  }
}
```

Supported Drive operations:

```text
search, get, download, export, create_folder, delete
```

## Google Sheets

Tool suffix: `_sheets`

### Create a spreadsheet

```json
{
  "operation": "create",
  "args": {
    "title": "Q3 project tracker"
  }
}
```

### Get spreadsheet metadata

```json
{
  "operation": "meta",
  "args": {
    "spreadsheet_id": "SPREADSHEET_ID"
  }
}
```

### Read a range

```json
{
  "operation": "get",
  "args": {
    "spreadsheet_id": "SPREADSHEET_ID",
    "range": "Sheet1!A1:C20"
  }
}
```

### Update a range

```json
{
  "operation": "update",
  "args": {
    "spreadsheet_id": "SPREADSHEET_ID",
    "range": "Sheet1!A1:B2",
    "values": [
      ["Name", "Status"],
      ["Alice", "Done"]
    ]
  }
}
```

Values use Google Sheets row format: a list of rows, where each row is a
list of cell values.

### Append rows

```json
{
  "operation": "append",
  "args": {
    "spreadsheet_id": "SPREADSHEET_ID",
    "range": "Sheet1!A:B",
    "values": [
      ["Bob", "In progress"],
      ["Carol", "Not started"]
    ]
  }
}
```

### Read multiple ranges

```json
{
  "operation": "batch_get",
  "args": {
    "spreadsheet_id": "SPREADSHEET_ID",
    "ranges": [
      "Sheet1!A1:C20",
      "Summary!A1:B10"
    ]
  }
}
```

Supported Sheets operations:

```text
create, meta, get, update, append, batch_get
```

## Google Docs

Tool suffix: `_docs`

### Create a document

```json
{
  "operation": "create",
  "args": {
    "title": "Meeting notes"
  }
}
```

### Read a document

```json
{
  "operation": "get",
  "args": {
    "document_id": "DOCUMENT_ID"
  }
}
```

### Insert text

```json
{
  "operation": "insert_text",
  "args": {
    "document_id": "DOCUMENT_ID",
    "index": 1,
    "text": "Meeting notes\n\n"
  }
}
```

`index` defaults to `1`.

### Use a Docs batch update

```json
{
  "operation": "batch_update",
  "args": {
    "document_id": "DOCUMENT_ID",
    "requests": [
      {
        "insertText": {
          "location": {
            "index": 1
          },
          "text": "Hello from the agent"
        }
      }
    ]
  }
}
```

Supported Docs operations:

```text
create, get, insert_text, batch_update
```

## Google Slides

Tool suffix: `_slides`

### Create a presentation

```json
{
  "operation": "create",
  "args": {
    "title": "Quarterly review"
  }
}
```

### Read a presentation

```json
{
  "operation": "get",
  "args": {
    "presentation_id": "PRESENTATION_ID"
  }
}
```

### Apply Slides batch updates

```json
{
  "operation": "batch_update",
  "args": {
    "presentation_id": "PRESENTATION_ID",
    "requests": [
      {
        "createSlide": {
          "objectId": "slide-1"
        }
      }
    ]
  }
}
```

Supported Slides operations:

```text
create, get, batch_update
```

## Google Forms

Tool suffix: `_forms`

### Create a form

```json
{
  "operation": "create",
  "args": {
    "title": "Customer feedback"
  }
}
```

### Read a form

```json
{
  "operation": "get",
  "args": {
    "form_id": "FORM_ID"
  }
}
```

### Read responses

```json
{
  "operation": "responses",
  "args": {
    "form_id": "FORM_ID"
  }
}
```

### Apply Forms batch updates

```json
{
  "operation": "batch_update",
  "args": {
    "form_id": "FORM_ID",
    "requests": [
      {
        "createItem": {
          "item": {
            "title": "How was your experience?"
          },
          "location": {
            "index": 0
          }
        }
      }
    ]
  }
}
```

Supported Forms operations:

```text
create, get, responses, batch_update
```

## Google Tasks

Tool suffix: `_tasks`

### List task lists

```json
{
  "operation": "lists",
  "args": {}
}
```

### List tasks

```json
{
  "operation": "list",
  "args": {
    "tasklist": "@default",
    "show_completed": false,
    "limit": 25
  }
}
```

`tasklist` defaults to `@default`.

### Create a task

```json
{
  "operation": "create",
  "args": {
    "tasklist": "@default",
    "title": "Review the proposal",
    "notes": "Check pricing and timeline",
    "due": "2026-08-10T17:00:00Z"
  }
}
```

### Complete or delete a task

Ask for confirmation before deleting a task.

```json
{
  "operation": "complete",
  "args": {
    "tasklist": "@default",
    "task_id": "TASK_ID"
  }
}
```

```json
{
  "operation": "delete",
  "args": {
    "tasklist": "@default",
    "task_id": "TASK_ID"
  }
}
```

Supported Tasks operations:

```text
lists, list, create, complete, delete
```

## Google Chat

Tool suffix: `_chat`

### List spaces

```json
{
  "operation": "spaces",
  "args": {
    "limit": 25
  }
}
```

### List messages

```json
{
  "operation": "messages",
  "args": {
    "space": "spaces/SPACE_ID",
    "limit": 25
  }
}
```

The `spaces/` prefix is optional.

### Send a Chat message

Ask for confirmation unless the user already authorized this exact message.

```json
{
  "operation": "send",
  "args": {
    "space": "spaces/SPACE_ID",
    "text": "The report is ready."
  }
}
```

Supported Chat operations:

```text
spaces, messages, send
```

## Google People / Contacts

Tool suffix: `_people`

### List contacts

```json
{
  "operation": "contacts",
  "args": {
    "limit": 50
  }
}
```

### Search contacts

```json
{
  "operation": "search",
  "args": {
    "query": "Alex",
    "limit": 10
  }
}
```

### Get a contact

```json
{
  "operation": "get",
  "args": {
    "resource_name": "people/c123456789"
  }
}
```

The `people/` prefix is optional.

Supported People operations:

```text
contacts, search, get
```

## Google Calendar

Tool suffix: `_calendar`

### List calendars

```json
{
  "operation": "calendars",
  "args": {}
}
```

### List events

```json
{
  "operation": "events",
  "args": {
    "calendar_id": "primary",
    "time_min": "2026-08-07T00:00:00Z",
    "time_max": "2026-08-08T00:00:00Z",
    "q": "standup",
    "limit": 25
  }
}
```

`calendar_id` defaults to `primary`.

### Create an event

Ask for confirmation if the event invites attendees or otherwise changes
another person's calendar.

```json
{
  "operation": "create_event",
  "args": {
    "calendar_id": "primary",
    "summary": "Team standup",
    "start": "2026-08-07T09:00:00-05:00",
    "end": "2026-08-07T09:15:00-05:00",
    "description": "Daily sync",
    "location": "Video call",
    "attendees": [
      "teammate@example.com"
    ]
  }
}
```

`start` and `end` can also be Google Calendar date/time objects, for example:

```json
{
  "start": {
    "dateTime": "2026-08-07T09:00:00-05:00"
  },
  "end": {
    "dateTime": "2026-08-07T09:15:00-05:00"
  }
}
```

### Update an event

Ask for confirmation before changing an event with attendees.

```json
{
  "operation": "update_event",
  "args": {
    "calendar_id": "primary",
    "event_id": "EVENT_ID",
    "patch": {
      "summary": "Updated standup",
      "description": "New agenda"
    }
  }
}
```

### Delete an event

Ask for confirmation before deleting.

```json
{
  "operation": "delete_event",
  "args": {
    "calendar_id": "primary",
    "event_id": "EVENT_ID"
  }
}
```

Supported Calendar operations:

```text
calendars, events, create_event, update_event, delete_event
```

## Product-specific raw requests

Each product also supports `operation: "request"` for an API feature that is
not covered by the structured operations.

Example for Sheets:

```json
{
  "operation": "request",
  "args": {
    "method": "GET",
    "path": "/v4/spreadsheets/SPREADSHEET_ID",
    "params": {
      "includeGridData": false
    }
  }
}
```

The path must belong to that product's Google API surface. A Sheets tool
cannot be redirected to Gmail, Drive, an OAuth token endpoint, or an
arbitrary internet host.

Use this escape hatch only when:

1. The structured operation list does not cover the required action.
2. You know the Google API path and request body.
3. The operation is still appropriate for the user's authorization.

Do not use raw requests to bypass confirmation requirements or to perform
destructive actions without user authorization.

## Response handling

Responses normally contain:

```json
{
  "ok": true,
  "status": 200,
  "product": "sheets",
  "operation": "get",
  "result": {}
}
```

For a Google API error, inspect `ok`, `status`, and `result`. Explain the
actual Google error to the user instead of silently retrying indefinitely.

Common status meanings:

```text
200–299  success
400      invalid operation or arguments
401/403  OAuth scope, account, or Google API permission problem
404      resource not found
409      vault connection or authentication configuration problem
429      Google rate limit
500+     upstream or service failure
```

Do not expose OAuth tokens or credentials in error messages, logs, generated
files, or user-facing responses.

## Programmatic fallback

For generated code or scripts that have access to the vault client, use the
structured helper instead of manually constructing authenticated HTTP
requests:

```python
result = vault.google(
    "GOOGLE_LEXI",
    "sheets",
    "get",
    spreadsheet_id="SPREADSHEET_ID",
    range="Sheet1!A1:C20",
)
```

Examples:

```python
emails = vault.google(
    "GOOGLE_LEXI",
    "gmail",
    "search",
    q="from:client@example.com newer_than:7d",
    limit=10,
)

vault.google(
    "GOOGLE_LEXI",
    "tasks",
    "create",
    tasklist="@default",
    title="Review the proposal",
)
```

The helper maps to the vault's structured Google endpoint. The OAuth token
stays inside the vault.

## If a Google tool is missing

1. Check the live available tool list.
2. Use the vault meta-tool's `list` action if available.
3. Confirm that the Google connection is granted to this agent.
4. Ask the user to reconnect the Google connection if newer product scopes
   are needed.
5. Do not fall back to asking for a token or secret.

## Short operating checklist

Before every Google action:

- Am I using the correct connection and product tool?
- Are all arguments inside `args`?
- Is the requested operation read-only or mutating?
- If mutating, has the user authorized this exact action?
- Am I avoiding credentials and arbitrary URLs?
- Did I check `ok` and `status` in the response?
