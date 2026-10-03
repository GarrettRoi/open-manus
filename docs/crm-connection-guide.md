# Fleet CRM connection and recovery guide

## What is connected?

Nothing is connected automatically. The initial `canaok`, `vowsok`,
`webinarninja`, and `mcgarryhomesokc` sources are disabled and unconnected.
Their names identify intended integrations, not evidence that these websites
offer native webhooks. No repositories were inspected, provider accounts
accessed, historical contacts imported, or lead outreach sent.

The supported adapter is a generic HTTPS server-to-server JSON POST. For each
website, its operator must verify whether the form backend or provider can
send authenticated POSTs with a stable submission ID. If not, an authorized
server-side bridge is needed. Do not put the credential in browser JavaScript.

| Source | Remaining operator setup |
|---|---|
| canaok.com | Identify the form handler, confirm authorized backend access, and map its submission fields. |
| vowsok.com | Confirm the submitting backend and event identifier; existing access to other Vowsok tools does not establish a CRM connection. |
| webinarninja.com | Verify the account's supported event delivery/export capabilities and authentication options before choosing a native callback or bridge. |
| mcgarryhomesokc.com | Identify the website/form provider and authorized server-side event path; map the property-inquiry fields. |

## Configure and test a source

1. Open **Lead desk → Sources & routing** in the authenticated dashboard. Choose a source or
   add one. Set business classification explicitly; the defaults make no
   assumptions about a website's business.
2. Choose an agent from the fleet roster, or configure business routing.
   Missing routing remains visibly unassigned. Configure optional field
   mappings from CRM field names to top-level incoming JSON keys.
3. Generate/rotate the source credential. Copy the one-time value directly
   into the submitting server's protected secret store. Rotation revokes the
   previous credential immediately. Do not paste it into agent chat or logs.
4. Enable the source. Set the endpoint to the actual dashboard HTTPS origin
   plus `/api/crm/webhook/<source-id>`. Obtain that origin from the deployment
   configuration; do not infer it from a development preview hostname.
5. Send a synthetic submission with a unique stable `event_id`. Re-send that
   exact event to verify the same lead ID is returned without another alert.
6. Check source delivery health, lead details, and notification status.
   A successful generic test proves this request path, not provider-native
   support or the website's live form connection. Only mark your operational
   rollout verified after an authorized real website test.

Safe payload example (all values synthetic):

```json
{
  "event_id": "synthetic-submission-0001",
  "lead": {
    "name": "Synthetic CRM Test",
    "email": "crm-test@example.invalid",
    "lead_type": "inquiry",
    "estimated_value": "1250.00",
    "currency": "USD",
    "acquisition_date": "2026-09-25",
    "next_action_date": "2026-10-01",
    "custom_fields": {}
  }
}
```

Use `Content-Type: application/json` and
`Authorization: Bearer <source credential>`. The credential, not the domain
name, `Origin`, or source ID, authenticates the sender. Maximum body size is
64 KiB and the authenticated source limit is 60 requests per minute.
Retry transient failures with the **same** event ID. A new opportunity should
have a new event ID even if its email or phone matches an existing lead.
Repeat contacts are flagged for review, not silently merged.

Dates are calendar dates (`YYYY-MM-DD`), including wedding dates; do not
convert them through browser time zones. Values are nonnegative decimal
strings with at most two decimal places, paired with an uppercase three-letter
currency code. Discover current fields and action schemas through the CRM
tool's `discover` action. Sources cannot choose trusted routing identities.

## Notifications and operational recovery

The lead and its notification event are committed together before delivery.
The recipient's Discord dispatch runtime processes the durable CRM queue;
its existing dispatch channel and Redis configuration must be available.
Discord is the audit surface, not the trigger. Lead text is untrusted data,
not instructions, and notifications do not authorize contacting a lead.

Use **Lead desk → Delivery health** to inspect unassigned, unavailable, retrying,
failed, and delivered events. Configure an assignment before retrying an
unassigned event. Restore an offline agent before retrying failures. Active
delivery leases or already-created dispatch assignments can block reassignment
with a conflict instead of silently sending work to the wrong agent.

Event-to-chain creation is atomic and idempotent. Runtime turn scheduling is
at-least-once: a crash after scheduling and before acknowledgement can repeat
the **same** notification/chain ID. Recipients must read the current lead
before acting and must not treat repeat turns as independent opportunities.
“Delivered” requires durable agent-chain progress (`working`, `waiting`, or
`done`), not merely a return from the runtime's background-task scheduler.
Agents are instructed to call `agent_dispatch` with `action: "working"` before
processing. This confirms receipt, not completed lead follow-up. Until then,
the event remains recoverable and retries after a minimum two-minute grace
period with bounded backoff, up to eight attempts before a visible failure
requiring owner retry.

## Backups and restore

CRM records are fleet-shared, namespaced under `crm:v1:`, with no expiration.
Per-agent filesystem cleanup and ordinary dispatch TTLs must not remove them.
This is not a substitute for Redis persistence: production needs durable
storage, AOF or equivalent persistence, an appropriate non-evicting policy,
and tested off-host backups. Provisioning and verifying those infrastructure
settings is an operator responsibility.

The owner-only export captures business records and safe source configuration;
it deliberately excludes credential hashes. Store exports securely: contacts,
notes, and history are sensitive business data. Export is not a credential
backup or a full notification-runtime backup.

For full disaster recovery:

1. Pause inbound submissions and CRM dispatch workers.
2. Restore from a verified Redis backup into an isolated instance first.
   Preserve the whole `crm:v1:` namespace, replay indexes, notification event
   IDs, and associated `dispatch:chain:crm_*` records and dispatch indexes.
3. Check lead counts, revisions, source definitions, and event/chain
   associations before restoring the affected keys to the fleet datastore.
   **Never flush the fleet database.**
4. Rotate credentials if their confidentiality is uncertain. A safe JSON
   export alone cannot restore them.
5. Resume workers and submissions, retry recoverable failures from the
   dashboard, and verify a synthetic lead. Retain idempotency identifiers
   so provider replays cannot recreate opportunities.

Code changes do not prove that production backups or live provider delivery
are configured. Keep those operational checks explicit.