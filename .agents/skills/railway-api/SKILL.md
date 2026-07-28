---
name: railway-api
description: Correct way to call the Railway GraphQL API for the Open Manus fleet (verify deploys, read service variables, list services). Use whenever Railway API access is needed — deploy verification, token sync, service env vars. Covers the token-access gotcha that causes false "Not Authorized" errors.
---

# Railway API Access (Open Manus fleet)

## The one rule that matters

**Always read `RAILWAY_TOKEN` from the shell environment (`$RAILWAY_TOKEN` via ShellExec + curl), NOT via the `requestSecrets` sandbox callback.** The sandbox path has returned an EMPTY string for this secret (observed July 2026), producing misleading "Not Authorized" errors even with a perfectly valid token. The token is a full account token and works fine.

Never print or echo the token value. Check presence with `[ -n "$RAILWAY_TOKEN" ]`.

## Working pattern

```bash
curl -s -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"query { me { name } }"}'
```

- Endpoint: `https://backboard.railway.com/graphql/v2`
- Auth: `Authorization: Bearer $RAILWAY_TOKEN` (account token — NOT a project token; do not use the `Project-Access-Token` header)
- Sanity check: the `me { name }` query. If it fails, the token itself is bad; if it works but a scoped query fails, the IDs are wrong.

## Fleet IDs

- Project: `ea6649cb-ac92-44fd-bea9-3fbf6ad5e473`
- Environment: `e57f146e-e0b8-4d5c-a443-c30e0baf016f`
- agent-lexi service: `08006723-2b99-4fa5-aec0-f4afe96a242c`
- Other agent service IDs: query `project(id: ...) { services { edges { node { id name } } } }`

## Verify a deploy (required after every push to `deploy`)

```bash
curl -s -X POST https://backboard.railway.com/graphql/v2 \
  -H "Authorization: Bearer $RAILWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"query { deployments(first: 3, input: { projectId: \"ea6649cb-ac92-44fd-bea9-3fbf6ad5e473\", serviceId: \"<SERVICE_ID>\", environmentId: \"e57f146e-e0b8-4d5c-a443-c30e0baf016f\" }) { edges { node { status createdAt meta } } } }"}'
```

A fix is only deployed when the newest deployment has `status: SUCCESS` **and** `meta.commitHash` matches the pushed commit. Pipe through python for readable output:

```bash
... | .pythonlibs/bin/python3 -c "import json,sys; d=json.load(sys.stdin); [print(e['node']['status'], e['node']['createdAt'], (e['node'].get('meta') or {}).get('commitHash','')[:7]) for e in d['data']['deployments']['edges']]"
```

## Read service variables (e.g. dashboard basic-auth creds for live verification)

```graphql
query { variables(projectId: "...", environmentId: "...", serviceId: "...") }
```

Returns a JSON map. Never display secret values; use them directly in follow-up requests.

## Other gotchas

- Shell `$VAR` inside single-quoted JSON payloads does not expand — build payloads with double quotes or heredocs carefully.
- `gitPush` callback can silently no-op the commit step; if `git log` doesn't show your commit, `git add && git commit` in the shell, then call `gitPush` again (shell `git push` lacks auth).
