# n8n Nodes Reference

This reference guide provides information on common n8n nodes and how to structure them in JSON for workflow creation.

## Core Nodes

### Webhook
Triggers the workflow when an HTTP request is received.
- **Type**: `n8n-nodes-base.webhook`
- **Key Parameters**:
  - `httpMethod`: GET, POST, etc.
  - `path`: The unique path for the webhook.
  - `responseMode`: `onReceived` or `lastNode`.

### HTTP Request
Sends an HTTP request to an external API.
- **Type**: `n8n-nodes-base.httpRequest`
- **Key Parameters**:
  - `method`: GET, POST, PUT, DELETE.
  - `url`: The endpoint URL.
  - `authentication`: `none`, `genericCredentialType`, etc.
  - `sendBody`: Boolean.
  - `jsonBody`: The body content (often uses expressions).

### Set / Edit Image / Code
- **Code**: `n8n-nodes-base.code` - Run custom JavaScript or Python.
- **Set**: `n8n-nodes-base.set` - Set or update variables in the workflow.

## AI & Advanced Nodes
- **AI Agent**: `n8n-nodes-base.aiAgent`
- **OpenAI**: `n8n-nodes-base.openAi`

## Connections Structure
Connections are defined as:
```json
"connections": {
  "Source Node Name": {
    "main": [
      [
        {
          "node": "Target Node Name",
          "type": "main",
          "index": 0
        }
      ]
    ]
  }
}
```

## Best Practices
1. **Naming**: Give nodes descriptive names.
2. **Error Handling**: Use the "Error Trigger" node for global error handling.
3. **Expressions**: Use `{{ $json.field }}` to reference data from previous nodes.
4. **Credentials**: Always check available credentials before assigning them to nodes.
