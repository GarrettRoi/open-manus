# Agent request board — evaluation and rollout

## Evidence ledger

**Isolated test evidence (test assertions, not production observations):**

- `tests/test_agent_dispatch.py` reproduces the old ambiguous parent
  inference in fakeredis with overlapping assignments: the heuristic selects
  the most recently updated *unrelated* assignment.
- `tests/test_dispatch_tickets.py` checks durable non-expiring ticket state and
  dedup, a single winner under concurrent claims, pre-accept lease expiry
  followed by a bounded retry, stale-token rejection, and post-accept
  ambiguous recovery to `blocked`/`reconciliation_required` rather than
  replay. It also checks retry exhaustion remains visible as failed,
  cancellation fences completion, and outcomes do not enqueue conversational
  follow-up turns.
- `tests/gateway/test_ticket_consumer.py` checks that acceptance precedes
  scheduling, a simulated scheduling exception *after acceptance* recovers to
  blocked without replay, transport startup can recover, an omitted explicit
  result blocks rather than succeeds, and a fenced ticket execution context
  reaches the native tool. These tests use fake Redis and mocked/in-process
  handlers, not live workers or model calls.

No workspace or production Redis keys were read or written for this
evaluation, and no live agent ticket was submitted. Running the tests and
assessing any pass/fail output belongs to the release operator; these source
assertions are not evidence of production delivery. Availability of workspace
Redis is **not** permission to use it for tests.

**Confirmed by source inspection of the pre-migration implementation:**

- `tools/agent_dispatch.py` writes chain state separately from child indexes
  and inbox/outbox enqueue operations. A crash between those writes can leave
  a ticket without a notification or a lineage update.
- `plugins/platforms/discord/dispatch.py` destructively pops inbox/outbox
  entries (`LPOP`); retries are bounded to five and a worker crash between pop
  and requeue can lose the event. The `dispatch:ack:<id>` SETNX claim occurs
  *before* injection, so a failure after the claim may suppress redelivery.
- The old `create_chain` infers a parent from the most recently updated active
  assignment if `parent_chain_id` is omitted. Under overlapping assignments,
  this is ambiguous rather than proof of which task the new request belongs to.
- The old `question`/`answer` actions form a back-and-forth agent Q&A path.
  Task-board threads (`plugins/platforms/discord/taskboard.py`) are only
  mirrors of several independently collected sources; they are not a
  transactionally consistent request queue.

These are failure *windows identified by inspection*, not claims that a
particular production ticket was lost. Grant-backed discovery, structured
ticket intake/completion, reliable recovery and delivery guarantees remain
**unverified in production** until runtime rollout and operator acceptance.

## Read-only diagnostic checklist

Run only with explicitly authorized, read-only access to the intended
environment. Avoid commands/scripts that mutate keys, pop queues, ack events,
or send Discord messages. Do not print credentials, request bodies containing
client data, or entire ticket payloads into logs.

1. Confirm the deployed tool schema/version exposes `search`,
   `inspect_access`, `submit`, `status`, `list`, `complete` and `blocked`.
   Confirm the controlled compatibility path for *already-issued* legacy
   records permits `working`/`complete` with a nonempty `text` (and `success`
   for completion), plus inspection/cancellation. New legacy task-only orders
   and `question`/`answer` negotiation are retired.
2. Confirm the target agent has configured Redis and an intake worker.
   Discord channel is an optional mirror, not a delivery prerequisite. Check
   vault grants/roster entries for freshness; inspect *capability metadata
   only*, never vault tokens.
3. Compare aggregate counts of pending/claimed/completed tickets with queued
   notification counts and a sample of redacted ticket IDs. Check age and
   retry/error metadata for stalled work; do not use `LPOP`, `RPOPLPUSH`,
   `SETNX`, or `DEL` to diagnose.
4. For a consented, non-sensitive ticket ID, inspect canonical state,
   assignee, delivery/ack timestamps, result/outcome, and any referenced
   Discord mirror. The mirror is not proof of ingestion. Check that the sender
   sees the same terminal state and artifact references without querying chat.
5. Confirm blocked results preserve `required_inputs`, failed results retain
   a reason, and neither terminal state is reported as success. Verify no
   duplicate turn or mirror post after an intentional worker restart in an
   *isolated* test environment, not on the live fleet.
6. Escalate any mismatch with only redacted IDs, timestamps and observed
   states. Do not manually repair live Redis without an approved recovery
   procedure and backup.

## Staged migration

1. Select an explicitly opted-in cohort and deploy the updated gateway
   consumer **and** native tool/schema together for those agents. There is no
   implemented feature flag or read-only action gate. Retain `working` and
   `complete` for already-issued legacy chains, as well as inspection/cancel;
   new task-only orders and Q&A are retired. Never bulk-convert/replay old work.
2. Run the isolated store and gateway tests above, plus authorization and
   grant-freshness checks, before cohort acceptance. Do not select workspace
   Redis merely because it is reachable. These fixtures cover selected
   crash/retry boundaries, not every production failure mode.
3. Within the updated cohort, use read-only `search` and `inspect_access` to
   compare capabilities with actual non-secret grants. Reject stale or unknown
   assignees rather than assuming role descriptions imply access.
4. With owner approval, allow structured `submit`, `complete` and `blocked`
   for the cohort and distribute matching skills/SOUL instructions with the
   deployment. Reconcile already-issued legacy effects before replacing any
   work with a new ticket.
5. Expand only after acceptance checks pass, watching stale-ticket and retry
   metrics. If intake is unreliable, halt new-ticket submission without
   deleting existing tickets or switching to Discord/webhooks.

## Opt-in harmless live smoke (not executed)

Require explicit owner permission and an opted-in sender and recipient.
After read-only `search` and `inspect_access(agent="raven")` confirm that
Raven is a suitable available recipient, the opted-in sender may submit this
**single** harmless request (illustrative only; do not run during evaluation):

```text
agent_dispatch(
  action="submit",
  to="raven",
  objective="Reply with exactly READY; do not research, contact anyone or use external tools.",
  inputs={"exercise_id": "request-board-smoke-v1"},
  constraints=["No client data", "No external API calls or side effects"],
  expected_output="Exactly READY as the result text",
  artifacts=[],
  dedup_key="request-board-smoke-v1"
)
```

Do not reuse the dedup key for a different intent. Record only a redacted
returned ticket ID and timestamps; expect one queued record, one acceptance
(inspect `started_at`/`delivery.accepted_at`), and one terminal `succeeded`
ticket with `result="READY"` visible by `status` to both participants.
No external artifacts should be delivered,
and a Discord mirror, if configured, must not drive agent work. Do not
manually inject turns or submit a second ticket to paper over missing
delivery. With renewed permission, separately test a blocked outcome using
an intentionally missing non-secret input and confirm `required_inputs`
arrives intact. Stop and escalate on any mismatch; none of this was executed.