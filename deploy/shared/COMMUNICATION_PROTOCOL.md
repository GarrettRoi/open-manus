# Agent Communication Protocol

> **Authoritative fleet rule:** use the native `agent_dispatch` tool for all
> live agent-to-agent communication. Read
> `/app/skills/harmony_communication/SKILL.md` for the complete protocol.

## System boundaries

1. **`agent_dispatch` is the only communication bus.** It creates a Redis-backed
   chain, injects the assignee's turn, and mirrors the chain to a Discord
   thread.
2. **Discord dispatch threads are an audit surface, not a chat room.** Agents
   use the tool for dispatch, working, questions, answers, completion, and
   cancellation. They do not post instructions or acknowledgements there.
3. **Task-board threads are read-only mirrors.** They show state and updates;
   posting there never delegates work and must not trigger a response.
4. **Hive Mind is memory, not messaging.** Use it for reusable lessons only.
5. **Webhooks, `inter_agent_comm`, `n8n_task_dispatcher`, and `[REQUEST]`,
   `[NOTIFY]`, `[END]`, and `[BLOCKED]` tag workflows are retired.** Do not
   use them as fallbacks.

## Minimal protocol

```text
agent_dispatch(action="dispatch", to="<agent>", task="<self-contained task>")
agent_dispatch(action="working", chain_id="<id>")
agent_dispatch(action="question", chain_id="<id>", to="owner", text="<question>")
agent_dispatch(action="answer", chain_id="<id>", text="<answer>")
agent_dispatch(action="complete", chain_id="<id>", text="<result>", success=true)
```

Use `status`, `list`, and `roster` for inspection; use `cancel` only for
intentional cancellation. If `agent_dispatch` is unavailable, report the
configuration problem rather than silently switching buses.

## Historical note

Older deployments used Discord tags, webhooks, and separate Redis inbox
scripts. Those paths are retained only so historical messages and old files
remain understandable. They are not part of the current protocol.