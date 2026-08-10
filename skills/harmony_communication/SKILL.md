---
name: harmony-communication
description: The authoritative communication and delegation protocol for Harmony and the Open Manus fleet. Use whenever Harmony needs to delegate work, monitor a handoff, answer a dispatched question, or report a result.
---

# Harmony Communication Protocol

This is the **single source of truth** for fleet communication. Do not combine
it with older webhook, inbox, tag, or Discord-chat protocols.

## The one decision to remember

Use the native **`agent_dispatch` tool** for every agent-to-agent handoff.
Redis is the source of truth. Discord is only the human-auditable mirror.

Do not send another agent a task by:

- posting or @mentioning them in Discord;
- calling `webhook_comm.py`;
- calling `n8n_task_dispatcher.py`;
- calling `skills/inter_agent_comm/send_task.py`; or
- using a task-board command as a notification.

If `agent_dispatch` is unavailable, do **not** silently fall back to one of
those systems. Tell Garrett that dispatch is unavailable and continue only
with work that does not require the missing handoff.

## What each system is for

| System | Authority | Use it for | Do not use it for |
|---|---|---|---|
| `agent_dispatch` | **Canonical** | Delegate, ask, answer, complete, cancel, inspect chains | — |
| Redis dispatch chains | **Canonical state** | Durable queue, status, assignee, result | Manual key edits |
| Discord dispatch threads | Mirror/audit surface | Garrett watches orders, reactions, questions, results | Agent conversation |
| Per-agent task-board threads | Automatic read-only mirror | See goals, kanban items, dispatch state, and recent updates | Posting instructions or triggering turns |
| `skills/task_board` | Optional task record state | Create/update structured legacy task records when a task needs one | Communicating with an agent |
| Hive Mind | Knowledge system | Search and submit reusable lessons | Delegating live work |
| Webhooks / `inter_agent_comm` / `n8n_task_dispatcher` | **Retired** | Nothing in the current fleet protocol | Any new work |

The task-board mirror may display records from several sources. That does not
make those sources communication channels. A board update is not a message to
an agent.

## Harmony's workflow

### 1. Understand and route

Choose the best specialist from the roster. Make the task self-contained:
include the objective, relevant context, deliverables, constraints, and
success criteria. Prefer one clear assignee. Use sub-delegation only when it
reduces total work.

Before dispatching, use:

```text
agent_dispatch(action="roster")
```

### 2. Create the handoff

Use the native tool, not a shell script:

```text
agent_dispatch(
  action="dispatch",
  to="cora",
  task="Create three spring wedding promo graphics and one Facebook cover. Use the current Vows & Vinyl branding. Deliver PNGs in the shared output folder. Success means all four assets are export-ready and sized for their platforms."
)
```

The result contains a `chain_id`. Save it in the current working context.
The system opens a Discord thread, mentions the assignee in the order, queues
the order through Redis, and injects a new internal turn for the assignee.
Do not poll Discord or ask the assignee to acknowledge in text.

### 3. Observe without creating chatter

The assignee automatically receives a 👀 reaction on the order and should
work silently. Status is represented by reactions:

| Reaction | Meaning |
|---|---|
| 👀 | Received/acknowledged |
| 🔧 | Working |
| ❓ | Waiting for an answer |
| ✅ | Completed successfully |
| ❌ | Failed |
| 🛑 | Cancelled |

Inspect only when needed:

```text
agent_dispatch(action="status", chain_id="123")
agent_dispatch(action="list")
```

Harmony's PM watcher handles stalled multi-agent chains. Do not create a
second reminder message in Discord just because a thread is quiet.

### 4. Handle questions

If an assignee asks a question, the chain pauses and the question arrives as
a new internal turn. Answer with the same chain:

```text
agent_dispatch(
  action="answer",
  chain_id="123",
  text="Use the existing square logo lockup. Do not create a new mark."
)
```

If Harmony needs Garrett's decision, the assignee should address the question
to `owner`; surface the decision request to Garrett in the normal conversation
without copying it into multiple fleet channels.

### 5. Handle completion or failure

The assignee reports through the tool. Harmony receives a new internal turn
with the result. Integrate it into the parent task and report to Garrett.
There is no `[END]` message to send and no completion webhook.

For a task Harmony herself owns, use:

```text
agent_dispatch(
  action="complete",
  chain_id="123",
  text="All four assets are exported and available at ...",
  success=true
)
```

Use `success=false` when the assignee could not complete the work. A parent
chain cannot complete while its child chains are still active.

### 6. Cancel safely

Cancel only when the work is no longer wanted or a replacement supersedes it:

```text
agent_dispatch(
  action="cancel",
  chain_id="123",
  text="Cancelled because Garrett approved the alternate approach."
)
```

Only the dispatcher, assignee, or owner can cancel a chain. Do not resurrect
a cancelled or completed chain by sending a second manual notification.

## Sub-delegation

When working on a dispatched order and delegating a genuine sub-task, pass the
parent chain:

```text
agent_dispatch(
  action="dispatch",
  to="raven",
  task="Verify the three competitor claims in the parent brief and return sources.",
  parent_chain_id="123"
)
```

Depth and fan-out rails are enforced. If the tool rejects deeper delegation,
answer the question yourself or ask the current chain's owner for guidance.

## Discord safety rules

- Never post agent instructions in a dispatch thread.
- Never use @mentions to start agent work.
- Dispatch-thread text is limited to questions, answers, and final results
  generated by the tool.
- Task-board threads are read-only. Never reply there or use them as a
  trigger.
- Never reply to a bot's mirrored board update.
- Do not send acknowledgements such as “Got it” or “Thanks for the update” to
  an agent. Use chain state and reactions instead.

## When a user asks for a status summary

Read the canonical dispatch state with `agent_dispatch(action="list")` and
the automatic task-board mirror if visual context is useful. Report:

1. active chains and their assignees;
2. waiting questions and decisions needed;
3. completed or failed results;
4. blockers and stale work; and
5. the next action.

Do not treat old webhook logs, tag messages, or arbitrary Discord text as
current chain state.

## Migration rule for stale instructions

If another skill, SOUL, or message says to use `[REQUEST]`, `[END]`,
`webhook_comm.py`, `n8n_task_dispatcher.py`, `inter_agent_comm`, or direct
task-board notifications, this skill supersedes it. Follow this protocol and
do not create a compatibility message in the old format.