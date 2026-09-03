# Fleet cron registry deployment verification

## Released source

- Local implementation commits: `1e45b76` and `d222529`
- Intended GitHub deployment branch: `deploy`
- Shared Redis namespace: `fleet:cron:v1`

## Production rollout

On 2026-09-03, the verified source tree was uploaded directly to Railway for
the Vault service and all 15 agent services in project
`ea6649cb-ac92-44fd-bea9-3fbf6ad5e473`, environment
`e57f146e-e0b8-4d5c-a443-c30e0baf016f`.

All 16 exact Railway deployment IDs reached `SUCCESS`.

## Production checks

- The registry reported 13 fresh agents and 13 jobs.
- Every published job had a generated or deterministic description.
- No published snapshot contained a raw `prompt` field.
- The Vault `/cron-jobs` page redirected anonymous requests to `/login`.
- The Vault `/api/admin/cron-jobs` endpoint rejected anonymous requests with
  HTTP 401.
- A Bianca job was paused through desired state, reconciled by the live agent,
  restored to its original enabled state, and left with no pending desired
  control.
- The pre-existing fleet freeze remained enabled throughout the smoke test and
  was not modified.

## Operational note

The release must remain on the same source tree in GitHub before relying on
GitHub-triggered autodeploys. Verify both the branch tree and Railway
deployment source revision whenever changing deployment transport.