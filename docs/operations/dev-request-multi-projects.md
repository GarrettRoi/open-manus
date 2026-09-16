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
the `project` field when it submits a request. A blank project keeps the
legacy `open-manus` default.

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
  project="research-lab"
)
```

The owner reviews the full request in Discord with `/devrequests` and clicks
**Approve**. Approval is still required for every project. Once approved, the
existing dispatcher queues the request for the configured destination; the
dashboard's project editor itself never starts a run.

## 4. Check or retry an approved request

Check the dispatch result in the request record or the dashboard status
endpoint:

```text
GET /api/admin/replit-mcp/status
```

The status endpoint shows aggregate counts; use the native tool's
`action="status", request_id="..."` for a specific request's destination and
dispatch error. A successfully resolved route is pinned before the provider
call: retries keep that original ID even if its mapping is edited or deleted.
To intentionally use another target, submit a new request for owner approval.
An unresolved unknown name has no pin; correct its mapping and retry normally.

If an approved request has a `failed` dispatch status, retry it through the
existing admin endpoint (using the vault admin authentication):

```text
POST /api/admin/replit-mcp/dispatch/<request-id>
```

Use `?force=true` only after confirming that no intended run is active; force
supersedes an in-flight lease. A retry re-queues only that already-approved
request and does not bypass Discord approval. If the failure says Replit is
not connected, complete the existing **Connect Replit** OAuth flow on the
dashboard; do not create another account or credential.

## Safe local dashboard preview

Focused mocked routing, registry API, agent-tool, and Discord UI checks run
without live Replit calls. The existing `tests/test_devreq_dispatch_dedup.py`
suite is blocked in this workspace by fakeredis reporting `unknown command
'eval'` (Lua support is unavailable). Production Lua/CAS guards remain in
place; repairing that test environment is tracked separately.

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
