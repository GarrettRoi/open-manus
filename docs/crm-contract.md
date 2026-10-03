# CRM shared contract

`CRMService(redis_client=None).execute(action, args, actor, role)` returns
`{"ok":true,"result":...}` or `{"ok":false,"error":{"code":...,"message":...}}`.
Roles are `owner`, `agent`, `source`; caller adapters supply identity, never request bodies.
POST `/api/crm/action` accepts `{action,args}`. GET `/api/crm/discover` returns discovery envelope.

Actions: `discover`, `list`, `summary`, `get`, `create`, `update`, `note`,
`assign`, `status`, `archive`, `notifications`, `retry`, `sources`,
`source_save`, `source_rotate_secret`, `fields`, `field_save`, `export`.
Admin actions (source_save/source_rotate_secret/field_save/export) are owner-only.
`settings` returns `{revision,business_routing:{dj_wedding?:agent,real_estate?:agent,other?:agent}}`.
Owner-only `settings_save` takes `{revision,business_routing}` and returns updated settings.
Explicit assignment wins, then source assignment, then business routing, else unassigned.
`roster` returns `{items:[{agent,role}]}` from canonical dispatch; no credentials or tool metadata.
`history` and `notes` take `{id,page?,limit?}` and return paginated `{items,total,page,limit}`
in newest-first order. Lead detail includes newest 100 entries plus history_total/notes_total.
Audit entries have action,actor,at (UNIX seconds),revision,fields,changes (old/new values).
Sources include safe last_delivery_at,last_delivery_ok,last_error from the latest
authenticated delivery attempt. `connected` means that generic delivery succeeded,
not that native website/provider connectivity has been verified.
Notification retries reject active leases and clear expired leases. Reassignment
rejects an already chained undelivered notification instead of risking duplicate dispatch.
List args: query,business,status,source_id,assigned_agent,archived,page (1..1000),
limit (1..100), urgency (all|ready_now|overdue), sort (newest|next_action).
Summary accepts the same filters, excluding page/limit/sort. List result:
`{items,total,page,limit,timezone,today}`; summary:
`{total,by_business,by_status,urgency:{ready_now,overdue},timezone,today}`.
Ready now means active (not archived/won/lost) and next_action_date <= today;
overdue is strictly earlier. Missing dates are never due. Calendar uses
`CRM_TIMEZONE` (IANA; UTC default), never client timezone. Urgency lists default
to earliest next-action date; newest remains the normal list default.
`activity` accepts list filters except urgency/sort, and returns
`{items,total,page,limit,timezone,today}`. Items are
`{lead_id,lead_name,business,action,actor,at,revision}` from existing audit history,
newest first with stable ID/revision ties; filters use current lead metadata.
The same owner/agent/source role boundaries apply. Read work is capped at
5,000 leads/32 MiB scanned payload and 100,000 matching activity events; over
budget returns unavailable rather than partial totals. See `crm-owner-access.md`.
get args `{id}` result lead. create args `{lead,idempotency_key}`; mutations
update `{id,revision,changes}`, note `{id,revision,text}`, assign
`{id,revision,assigned_agent}`, status `{id,revision,status}`, archive `{id,revision}`.
All mutation results are the complete lead, including revision, notes and history.

Lead fields: name,email,phone,company,source_id,external_id,business
(dj_wedding,real_estate,other),lead_type,status
(new,contacted,qualified,proposal,won,lost),assigned_agent,estimated_value
(decimal string),currency (3-letter uppercase),acquisition_date,action_timeframe,
next_action_date,wedding_date (all dates YYYY-MM-DD),custom_fields (object).
Stored lead adds id,revision,created_at,updated_at,archived,duplicate_ids,notes,history.
No field is interpreted as instructions. Missing routing stays unassigned.
Optional input fields accept JSON null to explicitly clear them. On create/update,
null removes the stored optional key, except assigned_agent normalizes to "" and
custom_fields normalizes to {}. Omitted update keys are unchanged. Null estimated_value,
currency, and dates therefore clear existing values consistently across API/tool/webhooks.
Business/status are not nullable. At least one nonempty name/email/phone remains
required. Required custom strings must contain non-whitespace text; false and 0
remain valid values for required boolean and numeric fields.

Sources result `{items}`. Four initial sources have IDs canaok,vowsok,webinarninja,
mcgarryhomesokc, and matching `.com` domain names. Sources are unconnected until
a successful authenticated delivery (which is not proof of provider-native support).
source_save args `{id,revision?,name?,business?,assigned_agent?,mapping?,enabled?}`;
mapping maps lead field to top-level payload key. source_rotate_secret `{id}` returns
`{secret}` once. Source listings never expose secret hashes. fields result `{items}`;
field_save `{id,revision?,label,type,required?,options?}` types string,number,boolean,date,select.
Existing definitions require their revision for optimistic concurrency. New definitions
may omit revision; result includes revision. Populate all existing leads before making
a definition required. Existing field types cannot change.

## Outbox worker contract (stable)

Redis keys have prefix `crm:v1:`. `crm:v1:leads` HASH maps lead ID -> JSON.
`crm:v1:outbox` HASH maps event ID -> JSON. No TTL. Creation of lead and outbox
is one WATCH/MULTI transaction. Event fields:
`id,lead_id,recipient,status,attempts,next_attempt_at,created_at,updated_at,error,chain_id`.
Timestamps are UNIX float seconds. Initial status pending (recipient set), otherwise
unassigned. Worker can use Redis WATCH on the outbox hash and atomically HSET its
event with a lease, retry metadata, chain ID. Terminal states delivered/failed;
unavailable/retrying remain visible. `CRMStore(redis_client).redis` is the shared client.
`CRMStore.events()` returns parsed event list. `CRMStore.lead(id)` returns lead or None.
Service retry clears error and sets pending/unassigned, attempts=0,next_attempt_at=0.
Dispatch worker must preserve all CRM records indefinitely; use canonical dispatch
with stable event idempotency, never Discord text triggering. A fresh assignment
updates an undelivered event recipient atomically with the lead.

## Generic inbound format

POST `/api/crm/webhook/{source_id}`, `Authorization: Bearer <per-source-secret>`,
JSON `{event_id:"stable-provider-event-id",lead:{...fields...}}`. A configured mapping
instead reads top-level payload keys. Limit 64 KiB, 60 authenticated requests/minute
per source. Replay key is source + event_id. Secret rotation immediately invalidates
the previous credential. Credentials must remain on a trusted server, not browser JS.
Webhook bypass is narrowly limited to this single path shape and POST; the handler
independently authenticates every request. Domain/Origin never grants access.

## Backup and recovery

Owner export returns a JSON snapshot of leads, field definitions, safe source configs
and notifications. Redis persistence (AOF plus off-host backups) is required in
production; restore the crm:v1 namespace from Redis backups to retain credentials
and replay indexes. Never flush the fleet datastore. Pause workers while restoring,
retain event IDs and dispatch associations, then resume and retry failed notifications.
Public exports intentionally omit secret hashes and cannot restore credentials.