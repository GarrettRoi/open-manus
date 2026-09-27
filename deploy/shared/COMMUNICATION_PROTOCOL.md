# Fleet agent request board

The detailed authority is `/app/skills/harmony_communication/SKILL.md`.
`agent_dispatch` is the compatibility tool name; use the running tool's schema
during migration. Already-issued legacy chains can still be inspected, marked
`working`, completed, or cancelled through controlled compatibility; new
task-only orders and legacy question/answer negotiation are retired.

For new work, search for a specialist on demand, inspect their current grants
with `inspect_access(agent=...)`, then submit one ticket to that peer with
`to`, `objective`, `inputs` (object), `constraints` (array),
`expected_output`, and `artifacts` (array). Harmony is not a mandatory
router. The recipient performs the work silently and calls
`complete(chain_id, text, success=true|false)` or
`blocked(chain_id, text, required_inputs=[...])`. Do not use a ticket as an
automated Q&A conversation.
Read status only when needed.

Redis holds canonical tickets. Discord threads and per-agent task-board threads
are read-only mirrors, never a source of agent turns. Hive Mind is knowledge,
not a handoff. Do not use direct agent mentions, `[REQUEST]`/`[END]` tags,
webhooks, `inter_agent_comm`, `n8n_task_dispatcher`, or board posts as alternate
notification paths. If the tool is unavailable, tell the owner. For read-only
operator checks and rollout staging, see `docs/agent-request-board.md`.