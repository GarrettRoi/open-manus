---
name: harmony-communication
description: Fleet request-board communication and delegation protocol.
---

# Fleet request board

The deployed runtime authority is `skills/harmony_communication/SKILL.md`.
Read it before agent handoffs. Use the native `agent_dispatch` compatibility
tool: discover candidates on demand via `search` and verify grants via
`inspect_access(agent=...)`; submit structured tickets addressed to a specific
peer. Complete with `complete(chain_id, text, success=true|false)` or
`blocked(chain_id, text, required_inputs=[...])`. The board delivers durable state;
Discord and per-agent task-board threads are read-only mirrors. Harmony is not
the mandatory router, and ticket delivery is not a chat/Q&A loop.

Follow the schema exposed by the running tool during staged migration:
already-issued legacy chains retain `working` and `complete` compatibility
as well as inspection/cancellation. Legacy task-only orders and Q&A are
retired; never blindly replay in-flight work. Roll out the gateway and tool
together to an opted-in cohort; no feature flag is implemented.
No direct agent mentions, request tags, webhooks, or task-board notifications
as delivery fallbacks. See `docs/agent-request-board.md` for rollout checks.