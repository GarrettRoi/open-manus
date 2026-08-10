---
name: harmony-communication
description: The authoritative communication and delegation protocol for Harmony and the Open Manus fleet. Use whenever Harmony needs to delegate work, monitor a handoff, answer a dispatched question, or report a result.
---

# Harmony Communication Protocol

The deployed runtime copy is `skills/harmony_communication/SKILL.md`. It is
the single source of truth: use the native `agent_dispatch` tool for every
agent-to-agent handoff. Redis is authoritative; Discord is only an audit
mirror.

The task-board threads are automatic, read-only status mirrors. Hive Mind is
shared knowledge, not live communication. Webhooks,
`n8n_task_dispatcher.py`, `skills/inter_agent_comm/send_task.py`, direct
Discord @mentions, and `[REQUEST]`/`[END]` tag workflows are retired and must
not be used as fallbacks.

Read the deployed copy for the complete workflow, tool examples, status
reactions, question/answer handling, sub-delegation rails, cancellation rules,
and migration guidance.