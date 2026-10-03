# Owner Lead desk access

## Verified live access — 2026-10-02

The existing dashboard is served by Railway **agent-lexi**, not agent-tatiana:
**https://agent-lexi-production.up.railway.app/crm**

Sign in with the existing Hermes dashboard owner credentials (not the household
spending password), then choose **Lead desk**. The verified domain targets port
9119 and serves at the root: there is no extra `/app` prefix. A reverse-proxied
installation instead uses `<dashboard-origin>/<prefix>/crm`, with its proxy
stripping the prefix and forwarding `X-Forwarded-Prefix`.

Read-only checks against the deployed service confirmed:
- Unauthenticated `/crm` redirects to the password provider; CRM API returns 401.
- Owner login and authenticated `/crm` return 200.
- The served JavaScript bundle contains the `/crm` route and Lead desk.
- Owner `discover` and `summary` succeed; aggregate count was 26 (23 real estate,
  3 DJ/wedding, 0 other). No contact data was printed and no lead was modified.
- The verification session was logged out.

Only agent-lexi had dashboard startup enabled in the inspected fleet
configuration. Tatiana's container is therefore not evidence that the owner's
dashboard is absent. No production variables, grants, deployment settings or
services were changed.

## New views and release boundary

The existing live dashboard predates this completion: **ready-now, overdue,
filtered summaries and cross-lead recent activity still require a targeted
release**. Local preview uses isolated fictional storage and does not show
the owner's real leads.

The container copies `hermes_cli/web_dist`; startup uses `--skip-build`. Run
`cd web && npm run build` and verify the produced bundle along with the Python
CRM changes before releasing. Publish only the intended dashboard service under
an explicitly authorized rollout; do not push the shared fleet deployment branch.
Keep the existing credentials, shared Redis and dashboard port/domain unchanged.
Publishing this dashboard may restart its hosting agent; account for active work.

After publishing, check the exact service's successful deployment, owner login,
`/crm`, the new discovery action `activity`, filtered summary and read-only views.
Do not create synthetic leads in production. Never use health alone as proof
that the UI or owner API is working.

## Date and activity rules

- `CRM_TIMEZONE` is an IANA timezone; absent configuration means explicitly
  displayed **UTC**, not the browser's timezone. Invalid configuration fails
  explicitly. Changing it is a separate operational setting choice.
- Ready now means nonarchived, non-won/non-lost leads with a next-action date
  today or earlier. Overdue is the subset strictly before today. Undated leads
  remain in All leads; a free-text action timeframe is not parsed into a date.
- Pipeline and urgency counts are scoped to the selected business. Business
  overview counts remain separately labeled; they do not invent a Cana business
  type. Cana remains a source within existing business classifications.
- Recent activity reads existing lead audit history (including note events),
  not the notification queue. Links open the lead's notes/history for details.
  Filters apply to current lead fields, not historical business membership.
  Archived records are excluded unless explicitly requested.
- Pagination is a live view, not a transactional export snapshot. Concurrent
  edits can move entries between pages. Stable tie-breakers prevent duplicate
  ordering for equal timestamps in an unchanged dataset.
- Read views cap work at 5,000 leads, 32 MiB scanned lead payload and 100,000
  matching audit events. Exceeding these budgets returns an actionable error,
  never truncated totals disguised as complete. Larger deployments need indexed
  reporting rather than increasing these limits blindly.

## Superseded dev request

Read-only inspection found request 86 already **approved and started** in Vowsok;
87 was approved and started in Open Manus. The supported pending-decision path
cannot deny already-started work and doing so would not stop an issued provider
run. No request was replayed, rerouted, relabeled as cancelled or erased. This
document records 87 as the corrected request; the owner must separately inspect
the already-started Vowsok run if containment or undo is needed there.