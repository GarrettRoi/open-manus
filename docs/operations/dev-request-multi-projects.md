# Multi-project dev-request owner walkthrough

The vault can route an approved development request to more than one existing
Replit project. This is an owner configuration feature: it does **not** create
Replit accounts, request new OAuth permissions, or start a run while projects
are being edited.

## 1. Discover the destination names

Ask an agent to call the native dev-request tool with:

```text
request_dev_modification(action="projects")
```

The response contains names only. An agent should use one of those names in
the `project` field when it submits a request. New submissions must provide a
project; a blank value has no default.

## 2. Add the second project in the vault

1. Open the vault dashboard and sign in as the owner.
2. In **Dev Request Destinations**, leave the legacy default alone unless the
   default project should change.
3. Click **Add project**, enter a short destination name (for example
   `research-lab`) and the existing Replit `replId`.
4. Click **Save destinations**.

Names are trimmed, lower-cased, and normalized to hyphens by the vault. The
server rejects duplicate names, duplicate Replit IDs, invalid values, and
oversized registries before it writes anything. Saving only persists the
registry; it does not dispatch an approved or unapproved request.

An explicit `open-manus` row takes precedence over the legacy default. If that
row is deleted, the legacy default is used again when it is configured.

The project IDs must belong to Replit projects the existing vault OAuth
connection can access. No second Replit account or OAuth connection is needed.

## 3. Submit and approve a request

From the agent session, submit the request with the discovered name:

```text
request_dev_modification(
  action="submit",
  title="Example change",
  description="Full problem and proposed implementation details",
  work_scope="project_app",
  project="research-lab"
)
```

Every new submission declares its scope; routing is not inferred from title or
description. Use `fleet_platform` only with `project="open-manus"` for shared
vault, credentials/OAuth, agent runtime, and dispatch infrastructure. Use
`project_app` with the explicitly named actual application for app feature
work. A missing destination, an unknown destination, or a scope/project
mismatch is rejected before a request record is created. The system never
falls back to the sole configured destination.

The resolved repl ID is snapshotted at submission and revalidated at approval.
Approval is refused if the mapping disappeared or changed. A successful
approval pins that exact target for dispatch and retries; later registry edits
cannot silently move the request. Legacy records remain readable but missing
scope, submission snapshot, or trustworthy approval evidence blocks dispatch.
This includes historical scoped approvals without a matching content digest
and versioned owner-confirmation evidence. They require explicit owner
correction and fresh approval; merely having an old reviewer name is not enough.
See [the investigation and dry-run rollout](dev-routing-investigation.md).

The owner reviews the full request in Discord with `/devrequests` and clicks
**Approve**. Approval is still required for every project. Once approved, the
existing dispatcher queues the request for the configured destination; the
dashboard's project editor itself never starts a run.

The review list filters out started approvals before limiting results, so newer
started work does not hide blocked requests. For any older record, use
`/devrequests request_id:<id>` to inspect its evidence and, when eligible, correct
its route. Started work remains inspection-only.

## 4. Check or retry an approved request

Check the dispatch result in the request record or the dashboard status
endpoint:

```text
GET /api/admin/replit-mcp/status
```

The status endpoint shows aggregate counts; use the native tool's
`action="status", request_id="..."` for a specific request's destination and
dispatch error. A successfully resolved route is pinned before the provider
call: eligible retries keep that original ID if a mapping is edited. Deleting
the destination disables dispatch; assigning its pinned ID to a different
project blocks dispatch too. Use **Correct routing** in `/devrequests` for
not-started requests, then review the exact new identity and approve again.
The original route and prior approval evidence remain visible. Configuration
edits alone cannot repair missing legacy approval evidence.

If an approved request has a `failed` dispatch status, retry it through the
existing admin endpoint (using the vault admin authentication):

```text
POST /api/admin/replit-mcp/dispatch/<request-id>
```

`?force=true` bypasses only the enqueue cooldown, never an active lease or a
recorded provider attempt. It cannot cancel an issued call. Ambiguous provider
outcomes and historical failed calls without attempt evidence require manual
inspection, not replay. A retry does not bypass owner approval. If Replit is
not connected, complete the existing **Connect Replit** OAuth flow on the
dashboard; do not create another account or credential.

Failures before the MCP `tools/call` attempt (including missing OAuth or failed
initialization) have a transport-confirmed `not_issued` disposition. Their
attempt evidence is retained in history and they can be retried after reconnect
or corrected by the owner. Once `tools/call` is attempted, transport timeouts or
uncertain results retain the durable attempt fence; reconnect does not release it.

## Safe local dashboard preview

Focused mocked routing, registry API, agent-tool, and Discord UI checks run
without live Replit calls. Install `requirements-dev-routing.txt` to enable
fakeredis Lua; never select workspace Redis to work around missing Lua support.
Run each focused test file in a separate process because the Discord tests
install module stubs. The configured CRM and household preview workflows are
unrelated to this Discord/vault routing change and must not be restarted.

For a screenshot or visual review, do not point the production vault at a
shared `REDIS_URL` and do not start the dispatcher. From the repository root,
run this disposable fixture instead:

```bash
python - <<'PY'
import asyncio, json, sys
import fakeredis
import uvicorn

sys.path.insert(0, "services/vault")
import app

fake = fakeredis.FakeRedis(decode_responses=True)
fake.set("replitmcp:target_repl", "repl-preview-default")
fake.set("replitmcp:projects", json.dumps({
    "research-lab": "repl-preview-research",
}))
app.r = fake
app.store.r = fake
app.item_store.r = fake
app.cron_registry.r = fake
app.replit_mcp.r = fake
app.app.router.on_startup.clear()  # no token sync, sweep, backup, or dispatch
app.csrf_token = lambda _request: "local-preview-only"

async def idle_dispatch(_mcp):
    await asyncio.Event().wait()

async def idle_backup(_redis):
    await asyncio.Event().wait()

app.replit_mcp_mod.dispatch_loop = idle_dispatch
app.vault_backup.backup_loop = idle_backup
app.verify_admin_session = lambda _request: True
app.verify_admin_api = lambda _request: True
app.require_admin_api = lambda _request: None
app.require_browser_csrf = lambda _request: None

uvicorn.run(app.app, host="127.0.0.1", port=8099, log_level="warning")
PY
```

Open `http://127.0.0.1:8099/` and use the already-running local preview
only. The fixture uses in-process `fakeredis`, seeds non-sensitive example
IDs, disables the backup/dispatch loops, and binds to loopback. Stop it with
`Ctrl-C`; never use this snippet with a production Redis URL.
