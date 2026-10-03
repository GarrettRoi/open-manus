# Routing investigation — 2026-10-03

## Read-only evidence

Inspected the registry and the latest 20 request records (72–91) using GET/MGET
only. No requests, mappings, queues, deployments, or grants were changed.

The registry has three distinct identities: Open Manus
`bb5ca13e-fc76-4692-8b8d-2c881b61697d`, Vowsok
`8eb8af06-4fb1-410b-b3f1-136194eea964`, and Cana Collective
`0df396a6-e653-484f-adf6-d20be8065df8`. The legacy default is empty.
There was no effective-default collision at inspection time.

| Request | Requested ownership | Stored approval/dispatch | Explanation |
|---|---|---|---|
| 88: tracking_thread errors | project_app / vowsok; Vowsok submission snapshot | Vowsok pin; started at 1790939729 | tracking_thread is shared fleet code. The incorrect ownership was already present at submission. Dispatch did not substitute a different stored destination. |
| 74: tracking_thread crash | fleet_platform / open-manus | pending, no dispatch | Same feature was previously classified as fleet work. Availability/home-app inference is not a valid ownership rule. |
| 76: shared cross-business CRM | project_app / vowsok | Vowsok pin; started at 1790276354 | Consistent stored routing, but cross-business wording alone does not establish the intended code owner. |
| 86: publish CRM dashboard | project_app / vowsok | Vowsok pin; started at 1790862990 | Follows the earlier CRM selection, not independent proof of correct ownership. |
| 87: fleet CRM dashboard | fleet_platform / open-manus | Open Manus pin; started at 1790939736 | Explicit fleet ownership; no stored target substitution. |
| 73: add_lead_to_fair deployment | no scope/snapshot; vowsok | Vowsok pin; started at 1790137145 | Legacy record cannot prove approval-time scope or target snapshot. Never replay it automatically. |

The started timestamps and pins are local dispatcher evidence, not an
independent provider receipt. Historical records lack separate provider-attempt
identity, original content digest, and registry revision evidence. We therefore
cannot prove the provider's actual target, reconstruct intermediate mapping
changes, or diagnose all reported incidents from these records alone.

Railway's latest vault deployment reports SUCCESS at
2026-10-01T12:49:07.914Z, but its metadata supplies no commitHash. This does not
establish that current workspace source is deployed. Scoped recent submissions
show that at least some agents use scoped submission logic; they do not prove
fleet-wide version uniformity. No provider run was launched to test this.

## Dry-run rollout and remediation

1. Run isolated tests with REDIS_URL pointing to an unreachable loopback port.
   Use fakeredis with the Lua extra; never fall back to workspace Redis.
2. Review the changes and identify exact deployed source versions before an
   owner-authorized rollout. No production rollout is authorized by this work.
3. Inspect records through /devrequests without approving. Cards must display
   work scope, canonical project, exact Replit identity, and routing reason.
4. Legacy/inconsistent requests need explicit correction and a fresh approval.
   Configuration repair alone must never authorize an old request.
   Historical scoped records without immutable content confirmation and
   owner-only approval evidence also need fresh approval, even when pins agree.
5. Deleted destination names disable dispatch. Mapping edits never replace
   approved IDs. Reusing a pinned ID under another project blocks dispatch.
6. Correction preserves original route and prior approval evidence, clears the
   old queued entry, and returns eligible requests to pending. Started,
   in-flight, or provider-attempted records cannot be corrected this way.
7. Force only bypasses an enqueue cooldown. It cannot cancel a provider call.
   Unknown provider outcomes require manual provider-side investigation, not
   automatic replay. No live remediation was performed or approved here.

Transport-confirmed failures before MCP tools/call are distinct from ambiguous
issued calls: they retain a not-issued history entry and allow safe reconnect
and retry. Older records can be inspected by owner-only request-ID lookup, even
when outside the default review list.

Any later request-history dashboard must use the shared dev_routing contract,
not create a second destination resolver or independent approval mechanism.