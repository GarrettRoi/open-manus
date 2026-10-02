# Developer clarification channel

Approved dev requests dispatch only after their Replit destination is durably
pinned and a clarification capability is provisioned. Configure an HTTPS
`VAULT_PUBLIC_URL` (or the existing Railway public-domain fallback). Missing or
insecure URLs fail dispatch explicitly; no run starts without this channel.

The dispatched developer privately receives a 24-hour bearer capability and
ask/poll instructions in the MCP prompt. The vault stores only its hash, never
the prompt or plaintext token. Do not commit, log, forward, or display this token.
It is not an admin credential or agent/fleet credential.

## Ask and poll

Both endpoints require `Authorization: Bearer <capability>`. Admin cookies,
admin headers, and agent credentials are not substitutes.

* `POST /api/dev-requests/{request_id}/destinations/{dispatch_repl_id}/clarifications`
  with JSON `{"question": "...", "idempotency_key": "stable-unique-key"}`.
* `GET /api/dev-requests/{request_id}/destinations/{dispatch_repl_id}/clarifications/{question_id}`
  where `question_id` is the returned record's `id`.

Responses are public clarification records (including `status`, `answer_state`,
`answer`, and delivery
state), marked `Cache-Control: no-store`. Retry the same question with its same
idempotency key; changing the question requires a new key. Admission is globally
limited to four questions in a rolling 300 seconds, including across projects.
Poll with a delay rather than a busy loop.

* **401**: missing, expired, revoked, or wrong-scope capability. Stop and request
  authorized reprovisioning; do not seek admin secrets.
* **400**: invalid input, missing question, or idempotency conflict.
* **422**: malformed JSON/schema validation failure.
* **429**: wait the `Retry-After` seconds before retrying.
* **503**: channel storage unavailable; retry with backoff.

## Authorized recovery

`POST /api/admin/dev-requests/{request_id}/clarifications/provision` accepts
`{"destination": "<pinned dispatch_repl_id>", "developer_id": "<developer identity>"}`.
It requires existing vault admin authentication. Cookie-authenticated mutations
also require a same-origin `Origin` and session-bound `X-CSRF-Token`.
The request must remain approved with that pinned destination.

The response returns `token`, `token_type: "Bearer"`, and `expires_in: 86400`
once, with `Cache-Control: no-store`. Only provide it privately to the dispatched
project developer. Reprovisioning creates an additional scoped capability;
existing capabilities retain their original expiry. No live deployment or fleet
operation is part of this API implementation.