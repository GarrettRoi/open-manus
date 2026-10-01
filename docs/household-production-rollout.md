# Household production rollout — blocked before activation

## Status

The independent Railway project, service, dedicated volume and Railway-generated
domain exist. **Household is not live and is not connected to Vault.**
The supplied bootstrap password does not satisfy the application's documented
12–256 character requirement. It was not transmitted to Railway or saved to disk.
Replace `HOUSEHOLD_OWNER_PASSWORD` through the secure secret interface before
continuing. Do not weaken password validation.

## Nonsecret deployment metadata

- Project: `f80f5fc3-290b-4576-93fe-9efb98753921`
- Production environment: `ee2ba093-b103-4f5c-823d-3593fc0720f3`
- Household service: `86240e5d-5922-44a0-b26f-c1cb2f5d8297`
- Dedicated volume: `1a7817b2-dd87-4eca-9a3e-fa04c68b1053`, mount `/data`
- Allocated URL (not serving a successful deployment):
  `https://household-spending-production.up.railway.app`
- Isolated source upload deployment:
  `a4eddf71-9f4c-433c-8a44-ca281cf5b4bd`, observed status `FAILED`
- Uploaded gzip archive SHA-256:
  `9d9c36a14772fbefb8233c61dd100cdcd79f4b8c373dcc4e4b23959b3569bd3e`
- Upload contained only Household application assets under `services/household`.
- Service root `/`; Dockerfile `services/household/Dockerfile`; one replica;
  healthcheck `/health`; generated domain target port 8099.

Railway's current API rejects setting `railwayConfigFile` as deprecated and no
longer accepts `DOCKERFILE` in the `Builder` enum. The accepted service update
sets `dockerfilePath` directly without either field. The source still includes
`services/household/railway.toml`. The sole upload occurred before configuration
completed; another isolated upload is required after successful secret validation
and production variable provisioning. No successful build/runtime is claimed.

## Preservation checks

All 18 non-Vault existing services retained their exact latest deployment IDs,
and all 14 original project volumes remained unchanged. Vault also remained on
deployment `cbc40772-68cd-4ab1-97ea-dc60d3f7f8c5`, commit
`1ce6f2b4afc70c24e486437d99b0adccf6cfe795`. Its root, source, domains and variables
were not changed. Its health endpoint returned HTTP 200:
`https://vault-production-44a6.up.railway.app/health`.

Authenticated Vault overview verified that the Household preset and connection
are both still absent and that there are zero Household grants. No Household
MCP token, fleet agent token or grant was created. No Git branch was pushed.

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
tests passed; entrypoint Python syntax check passed. Live owner login, storage
persistence, empty ledger, MCP handshake and encrypted Vault connection
verification remain pending.

Receipt scanning remains unconfigured. No OpenRouter key was created, no
provisioning key was used for inference, and no billable inference was attempted.

## Resume safely

Reuse the project/service/volume above; do not create duplicates. Validate the
replacement secret before remote writes. Set the documented production variables,
upload the allowlisted Household archive to this exact service and environment,
and wait for its returned deployment ID to reach `SUCCESS`. Then verify owner
login and remove only the Railway bootstrap variable.

Deploy Vault from the recorded baseline plus only the Household catalog entry;
preserve its `services/vault` root and existing configuration. Only after Household
is healthy should an owner-issued 365-day read-only MCP token be transferred
in memory into a `HOUSEHOLD_SPENDING` Vault connection with no grants. Verify
the four tools and empty-data results before reporting it connected.