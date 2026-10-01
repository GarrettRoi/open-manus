# Household production rollout — live and connected

## Status

**Household is live, with persistent storage and a verified read-only Vault
connection.** Sign in as `owner` using the password supplied through the secure
secret form. The replacement password passed the documented 12–256 character
check before provider changes. No password or MCP token was written to project
files or printed.

The Railway bootstrap password was removed after account creation. A subsequent
successful deployment without that variable retained the same owner account and
accepted its password, verifying persisted account/hash storage. The workspace
secret was not deleted.

## Nonsecret deployment metadata

- Project: `f80f5fc3-290b-4576-93fe-9efb98753921`
- Production environment: `ee2ba093-b103-4f5c-823d-3593fc0720f3`
- Household service: `86240e5d-5922-44a0-b26f-c1cb2f5d8297`
- Dedicated volume: `1a7817b2-dd87-4eca-9a3e-fa04c68b1053`, mount `/data`
- Live Household URL:
  `https://household-spending-production.up.railway.app`
- Final Household deployment:
  `3bd75d85-f43e-45c7-bfc7-965bb865bf3c`, observed status `SUCCESS`
- Initial healthy bootstrap deployment:
  `060e8ddc-c1e6-4c92-948d-c2ea532ecdf2`, observed `SUCCESS` before replacement
- Uploaded gzip archive SHA-256:
  `9d9c36a14772fbefb8233c61dd100cdcd79f4b8c373dcc4e4b23959b3569bd3e`
- Upload contained only Household application assets under `services/household`.
- Service root `/`; Dockerfile `services/household/Dockerfile`; one replica;
  healthcheck `/health`; `PORT` and `HOUSEHOLD_PORT` both 8099;
  generated domain target port 8099.
- Production settings: `HOUSEHOLD_DATA_DIR=/data/household`,
  `HOUSEHOLD_PERSISTENT_STORAGE=1`, exact HTTPS public origin above.
- Final Vault deployment:
  `17dd49c0-0792-462a-a697-eaeacb133f83`, observed status `SUCCESS`
- Vault gzip archive SHA-256:
  `17cd6e1d5b687fa9a6feaeff440509ee8b8ca728145a15d5aa77172457508076`
- Vault source: baseline `1ce6f2b4afc70c24e486437d99b0adccf6cfe795`
  plus only the `household_spending` catalog entry. Every staged file was compared
  with the baseline; an AST comparison verified the sole catalog addition.
- Live Vault URL: `https://vault-production-44a6.up.railway.app`

Railway's current API rejects setting `railwayConfigFile` as deprecated and no
longer accepts `DOCKERFILE` in the `Builder` enum. The accepted service update
sets `dockerfilePath` directly without either field. The source still includes
`services/household/railway.toml`.

Historical unsuccessful attempts were `a4eddf71-9f4c-433c-8a44-ca281cf5b4bd`
(before completed configuration) and `a57a2ec2-17a6-45fa-b039-a7671acc6c59`
(before explicitly aligning Railway's healthcheck `PORT` with application port
8099). Both were superseded by the verified successful deployments above.

## Preservation checks

All 18 non-Vault existing services retained their exact latest deployment IDs,
and all 14 original project volumes remained unchanged. Vault alone was updated
from `cbc40772-68cd-4ab1-97ea-dc60d3f7f8c5` to the deployment above. Its
`services/vault` root, source configuration, domains, build settings and variable
values were verified unchanged. Existing Vault connection IDs and all existing
grants were preserved. Its health endpoint returned HTTP 200:
`https://vault-production-44a6.up.railway.app/health`.

No Git branch was pushed. Both services were deployed through exact-service,
exact-environment official source uploads from isolated allowlisted archives.
Unrelated Keenable/Firecrawl changes were not deployed. No fleet agent identity
token was created and no agent grant was added.

## Verified Vault connection

- Connection ID/name: `HOUSEHOLD_SPENDING`
- Preset: `household_spending`
- Upstream: `https://household-spending-production.up.railway.app/mcp`
- Auth: owner-issued bearer token, transferred directly in process memory
- Scopes: `purchases:read`, `summary:read`
- Lifetime: 365 days; expires `2027-10-01T12:53:19.459650+00:00`
- Status: `ready`; background connection test successful; token not suspect
- Agent grants: **zero**. No agents can use this connection until the owner
  selects and grants them access.
- Synced tools: `household_purchases`, `household_summary`,
  `household_snapshot`, `household_items`; all four advertise read-only,
  non-destructive behavior.

Authenticated Vault overview and explicit sync verified the stored credential
works. A read-only inspection of this connection's Redis `secret_enc` field
confirmed Fernet encryption, exact in-memory decryption to the issued token and
absence of a plaintext `api_key` field. No secret was emitted or persisted locally.
The Vault overview does not expose the token.

One initial show-once token was immediately revoked after the operator helper
expected HTTP 200 rather than the endpoint's correct HTTP 201. It was never
connected to Vault. The replacement is the sole active token issued for this
connection.

The non-Vault before/after deployment IDs were:

| Service | Unchanged deployment |
| --- | --- |
| agent-bianca | `5fcaec34-8b1c-4da0-affd-ad354b0cf54b` |
| agent-lexi | `66ba1a90-7ab8-4980-a2b4-d23f772e8980` |
| agent-cora | `86f56138-b5d1-4ff4-999e-70872ba49c82` |
| agent-raven | `e0e51450-182b-4c89-8a31-45079e5b9296` |
| agent-tatiana | `b18e383c-51bb-4dcc-8dba-cb7c8a402adb` |
| agent-addison | `33333b7c-4a7a-4c9d-8225-986db5b390b7` |
| agent-sasha | `bca13efd-23ba-417f-b53c-014e96f6cd3c` |
| agent-samantha | `7fba2145-674c-4153-aeca-9c7d76e0298b` |
| agent-jade | `73737120-1e33-46a3-bd0f-ea1dedd9a7f2` |
| redis | `35bd6c55-04bf-4a01-8e89-73e848f2ab38` |
| agent-sabrina | `88db8bc2-9409-4762-a6f4-6dee2574e41b` |
| redis-insight | `a2e56f8d-c24b-435d-b793-342dc07da4b4` |
| qdrant | `f910d204-deca-4904-9278-1474e2c0854f` |
| agent-scarlett | `55679f25-f101-473d-8caf-306065fc18b8` |
| agent-victoria | `25140d17-c088-43f9-a08a-e45dbd89d3df` |
| agent-harmony | `efe51f1b-2ddc-41f8-8f59-c9089549d143` |
| agent-vivian | `c89b56bb-f3e9-4326-a6ae-44b3a56fd94c` |
| agent-valentina | `8f217fdc-381f-4621-ae31-27e568f6d44d` |

## Local changes and verification

The Household Docker entrypoint initializes only `/data/household` with UID/GID
10001 and mode 0700, rejects symlinks and unexpected data paths, clears
supplementary groups, drops root permanently and uses umask 0077 before starting
the application. It never recursively changes ownership of the mount.

Local verification: 41 Household tests and 7 focused Vault/Household contract
tests passed; entrypoint Python syntax check passed.

Live checks passed:

- Exact final Household and Vault deployment IDs both reached `SUCCESS`.
- Household deployment metadata shows the `/data` mount.
- Owner login and `/api/session` authenticate correctly, including after a fresh
  deployment with the Railway bootstrap password absent.
- Session cookie is Secure, HttpOnly and SameSite=Strict.
- Unauthenticated purchases, summary, export and token-management requests
  return HTTP 401 with only an error payload, not financial data.
- Preview/demo flags are false; real ledger has zero purchases and empty
  currency totals. No fake purchases or financial test records were inserted.
- MCP initialize/initialized handshake, exact scoped tool listing and all four
  tool calls succeeded. Purchases returned zero records; summary/snapshot/items
  returned empty currency totals.
- Vault connection is ready, Fernet-encrypted at rest, tool sync and background
  test succeed, zero grants, previous grants unchanged.
- Unauthenticated live browser screenshot displays the owner login screen.
- The successful verification owner session was logged out.

Receipt scanning remains unconfigured. No OpenRouter key was created, no
provisioning key was used for inference, and no billable inference was attempted.

## Remaining owner choices

Manual purchase entry is available now. Receipt OCR remains intentionally
unconfigured and requires separate approval/configuration; this rollout did not
create an OpenRouter key or configure an inference connection.

Agent access is intentionally disabled by zero grants. Choose specific agents
in Vault before granting access; do not grant the whole fleet by default.
Rotate the upstream token before its expiration. Arrange encrypted Household
database/private-upload backups separately; a persistent volume is not a backup.

Future deployments must continue to target only the intended service. Do not
push a shared deployment branch to deploy Household or this isolated Vault change.