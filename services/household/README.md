# Household spending — independent attachment

A private FastAPI/Jinja add-on in the existing repository. This service has **its own URL, UI, auth, SQLite database, upload directory and deployment config**. It does not replace/migrate/import the fleet, agent CRM, vault app, or their storage. No Redis client is used. The primary agent route is **agents → Vault → Household MCP**, with an owner-issued scoped token stored only in Vault and grants only to selected agents. OCR can use Vault in the opposite direction; raw provider credentials never leave that vault.

The API contract is in `API.md`. The frontend lives exclusively in `templates/` and `static/`.

## Local preview (fictional only)

From repository root:

```sh
HOUSEHOLD_PORT=8099 python scripts/household_preview.py
```

It binds **0.0.0.0** for Replit's shared preview proxy and creates a unique marked `household-demo-*` temporary directory. Remote preview users can auto-sign into this **shared fictional DEMO** household; do not enter real financial or personal data here. It never reads production database paths or inference credentials. All seeded stores/purchasers/notes and every financial JSON response are labeled demo. Seed data is discarded on exit. OCR and agent token issuance/MCP are disabled. Manual edits exercise the real APIs only in that temporary database.

Preview supports HTTPS-terminating proxies through preview-only same-host/forwarded-host Origin handling; CSRF remains required for writes. Production Origin/host/auth checks are unchanged and do not trust forwarded headers. Never publish this shared demo as a production service. `HOUSEHOLD_PREVIEW` is deliberately not an environment auth-bypass switch.

## Dedicated production configuration

Deploy a **new, separate service** with repository-root build context using `services/household/Dockerfile` and `services/household/railway.toml`. Do not apply these to any existing service. Publishing requires owner approval; no production URL or deployment has been created here.

Required:

- `HOUSEHOLD_DATA_DIR=/data/household` — mount a dedicated persistent volume at `/data`. Never point this at existing fleet/vault/CRM storage. SQLite and private uploads must both persist on the same volume.
- `HOUSEHOLD_PERSISTENT_STORAGE=1` — operator acknowledgement that a real persistent volume is attached. Code cannot detect platform volume durability.
- `HOUSEHOLD_PUBLIC_URL=https://<independent-household-host>` — origin only, no path. TLS required.
- First-run owner account created through operator CLI **or** `HOUSEHOLD_OWNER_USERNAME` and `HOUSEHOLD_OWNER_PASSWORD` secrets. Password must be 12–256 characters. This secret is only used to bootstrap an empty dedicated database; it never resets an existing password. Remove bootstrap secret after setup.
- Optional `HOUSEHOLD_TIMEZONE` (default `America/New_York`), `HOUSEHOLD_CURRENCY` (default `USD`). Dates are inclusive household-local calendar dates, not implicit UTC timestamps.
- `HOUSEHOLD_PORT` or `PORT` (default 8099). Serve command: `python -m services.household.app`.

Production is the default. Startup **fails closed** without persistent-storage acknowledgement, HTTPS origin or an owner account. `HOUSEHOLD_ENV=development` relaxes HTTPS/volume acknowledgement for explicitly isolated local development only; it does **not** create accounts or bypass authentication. No unrestricted signup or web password setup endpoint exists.

The Docker image includes Poppler `pdfinfo` for genuine PDF validation. Standalone Python dependencies are in `requirements.txt` (install into the service's own environment); tests also require pytest. Persistent volume directory must be writable by container UID 10001. Provision its ownership/permissions before starting; private directory mode 0700, files/database 0600. Keep deployment replicas at **one** with SQLite/local files; do not share this filesystem across multiple containers. The service disables provider credential/access logging and API documentation routes.

### Operator-only account setup/recovery

Use the service terminal with the required dedicated config set. Password is read interactively with `getpass`, never a command-line argument or a chat message.

```sh
python -m services.household.app setup --username owner
python -m services.household.app add-user --username partner --role partner
python -m services.household.app reset-password --username owner
```

At most two local accounts; purchaser entities are separate editable identities. Owner/partner both manage purchases; only owner issues/revokes agent tokens. Password reset invalidates that user's sessions. Scrypt hashes, hashed cookie sessions (12-hour lifetime), CSRF on browser writes, strict same-site cookies, secure production cookies, exact Origin checks, and persistent sign-in rate limiting are independent of fleet auth.

## OCR: configure explicitly, prefer the vault

This is **Household → Vault → OpenRouter** for receipt scanning, independent of **agents → Vault → Household MCP** for read-only spending access. The two routes use different tokens and grants; configuring one does not enable the other.

OpenRouter integration catalog lookup found no configured Replit OpenRouter connector during implementation. Existing workspace vault architecture is proxy-only. This add-on **does not automatically discover or grant access to connections**.

Preferred server-only adapter:

- `HOUSEHOLD_VAULT_URL=https://<vault-host>` (base origin).
- `HOUSEHOLD_VAULT_CONNECTION=<owner-approved-openrouter-connection-id>`.
- `HOUSEHOLD_VAULT_TOKEN=<dedicated-scoped-vault-agent-token>` as a service secret.

The owner must create/approve an OpenRouter API-key connection and grant that dedicated vault identity. The adapter calls `POST /api/vault/proxy/{connection}` with:

```json
{"method":"POST","path":"https://openrouter.ai/api/v1/chat/completions","json":{"model":"<selected-live-id>","messages":["<bounded-private-input>"]}}
```

It sends only the vault identity token to the vault; vault injects the OpenRouter key. It consumes the existing `{status,truncated,json}` wrapper, rejects truncated/upstream failures, and never exposes upstream diagnostic bodies. No existing vault configuration is altered.

An alternative for this new service is the household-specific secret `HOUSEHOLD_OPENROUTER_API_KEY` (ordinary inference key, not provisioning key). Vault takes precedence when configured. **`OPENROUTER_PROVISIONING_KEY`, generic fleet OpenRouter keys/tokens and all agent secrets are never read or used for inference**. No key creation/provisioning or billable live OCR test was performed. Readiness reports “configured, not yet verified” or an explicit unavailable state; no-key upload returns 503. Manual purchase entry works without OCR.

### Live model choices / cost and limits

Verified against public official HTTP documentation and live anonymous model metadata:

- https://openrouter.ai/docs/guides/overview/models
- https://openrouter.ai/docs/guides/overview/multimodal/image-understanding
- https://openrouter.ai/docs/guides/overview/multimodal/pdfs
- https://openrouter.ai/api/v1/models

Catalog refreshes from the official endpoint with a bounded **five-minute cache**; upstream failure returns explicit 503 instead of made-up/stale prices. Input/output modalities label image support and **native** PDF support (`file` metadata). Text-output models may process PDF through the explicitly selected `cloudflare-ai` file-parser; they are not falsely labeled native PDF models. No fixed inference model.

Prices are displayed as USD/million input/output tokens, plus per-image/request rates when supplied. Missing price is null, not zero; per-million-token prices are **not guaranteed per-photo quotes**. Actual provider usage cost is retained on the editable draft. The implementation explicitly selects `native` for file-input models and `cloudflare-ai` otherwise. Current docs label Cloudflare parser free; model tokens still cost money. `pdf-text` is deprecated. No implicit default engine or automatic paid Mistral fallback is used.

Safety bounds: 10 MiB per source, 20 PDF pages, 24 megapixels, images stripped of EXIF and resized to 2400px, 200 line items, 6,000 output tokens, 90-second inference timeout, bounded upstream responses, two concurrent scans, **25 scans/day/household** (including failed attempted scans). `HOUSEHOLD_SCAN_DAILY_LIMIT` may be 1–100. Models with unavailable token rates, tiered price overrides, prompt >$20/million, completion >$100/million, or image/request fees >$1 are rejected rather than guessing cost. Bounds reduce exposure but are not a guaranteed dollar budget; operator should also set provider-side credit/budget limits.

Supported files: JPEG, PNG, WebP, PDF. No HEIC, GIF, SVG, Office files. PDF type/signature/pages/encryption/readability validated with Poppler and a timeout; malformed/encrypted/oversized files fail explicitly. Receipt files are private, served only through authenticated API source routes. Uploads are sent only to the explicitly configured inference provider/vault. Review that provider's privacy/retention terms before uploading financial documents.

## Financial integrity and retention

Integer cents for documented two-decimal currencies, never floats for ledger amounts. Currency groups never silently combined. OCR is untrusted data, not instructions: missing fields produce editable warnings/nulls; no extraction counts as spending until explicit reviewed confirmation. Price screenshots are separate drafts. Confirm is idempotent/transactional. A receipt reconciles `sum(net lines) + tax - additional receipt discount = total`; subtotal is informational, not added again. Exact/normalized source duplicates prompt review.

Without category/product filters, headline/person/time totals use receipt totals. With category/product filters, they use matched **item lines only**, exclude unrelated lines and receipt-level tax/discount. Category/item totals always use net line amounts and state that basis. Inclusive local dates, person/store/category/product filters, pagination and explicit all-match JSON/CSV exports are shared with MCP. Queries exceeding 10,000 purchase records require a narrower period, never silently truncate. CSV escapes formula-leading untrusted text and warns not to sum repeated receipt totals.

Sources retained while a draft/purchase references them; deleting a draft or confirmed purchase removes its source when unreferenced. Confirmation retains a read-only draft link for retry idempotency; purchase deletion removes that link. No background retention deletion; operator is responsible for backups and desired deletion policy.

## Agent attachment through Vault (read-only MCP)

**Primary route: agents → Vault → Household MCP.** Household is not deployed yet; no live Household URL, Vault connection, tool sync or agent grant has been created or verified. The named **Household Spending** Vault preset is setup support, not a live connection. The fictional preview disables MCP and token issuance and cannot be used as an upstream service.

After the owner approves a dedicated Household deployment:

1. Sign in to the **Household owner UI → Settings**. Issue a named expiring Household MCP token with only the needed read-only scopes (`purchases:read` and/or `summary:read`). The secret is shown once; Household stores only its SHA-256 hash.
2. Open the existing **Vault → Services** and choose the **Household Spending** preset (`household_spending`). Enter the deployment's full HTTPS MCP endpoint URL, ending in `/mcp`. There is no default host; use the actual approved deployment URL.
3. Paste the show-once **Household MCP token** into Vault's **Bearer token password field**. This is not the Household login password, a Vault agent identity token, or an OCR/OpenRouter key. Never copy the secret into agents, `mcpServers` configuration, prompts, model context, chat, screenshots or source files.
4. **Save**, then **Sync tools** on the Vault connection. Verify the successful sync and scoped `household_*` manifest before granting access. `purchases:read` exposes `household_purchases`; `summary:read` exposes `household_summary`, `household_snapshot` and `household_items`. Every tool is read-only.
5. In Vault's **Grants**, allow the connection **only for the agents the owner selects**. No fleet-wide default grant. Those agents use their existing Vault identity and native `vault_<connection>_<tool>` tools (or `POST /api/vault/mcp/{connection}` with a tool name and arguments). Vault injects the Household token server-side; it never enters the agent's context. Agents pick up tools on their next Vault sync or with `vault(action='refresh')`.

Agent scopes cannot upload, mutate purchases, fetch private files, issue tokens or infer with provider credentials. Removing a Vault grant blocks that agent's next call. Revoking the Household token in the owner UI blocks all uses of that upstream token on the next request; after replacing it in Vault, Sync tools again to verify the current scopes.

The upstream uses Streamable HTTP JSON-RPC: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`, protocol versions 2024-11-05 / 2025-03-26 / 2025-06-18. Stateless responses, no SSE GET stream (405 permitted); do not use legacy SSE transport. Tools share the dashboard's isolated DB/query semantics; exact cents and timezone are disclosed. Snapshots support calendar months or custom periods; items support normalized labels (e.g. shampoo) and categories (e.g. snacks). Paginated purchase results include total count.

## Backup / restore

Only household data; never run fleet datastore cleanup commands. At a **quiescent/offline** point:

```sh
python -m services.household.app backup --output /secure-backups/household-YYYYMMDD.sqlite3
```

This uses SQLite's backup API for a consistent DB copy. Also copy `HOUSEHOLD_DATA_DIR/private-uploads` from the same quiescent point into the encrypted backup. Database contains private financial records, password/session hashes and token metadata; restrict/encrypt backups. Do not include inference/vault secrets in exported backups. Schedule and test backups externally; volume durability is not a backup.

Restore with service stopped: replace only the dedicated `household.sqlite3` and matching private-uploads directory; set UID 10001 and modes 0700/0600. Do not restore into existing service directories. A raw backup can preserve sessions/agent tokens; after a restore/security event, operator may invalidate sessions and revoke household tokens in this dedicated database before reopening. Keep secrets in the deployment secret store, separate from data.

## Isolated verification

```sh
python -m pytest services/household/tests -q --confcutdir=services/household/tests
python -m pytest services/vault/tests/test_household_spending_mcp.py -q --confcutdir=services/vault/tests
```

Tests clear credential/household/Redis environment variables, prohibit socket connections, create dedicated temporary SQLite databases, and use explicitly mocked inference/metadata HTTP responses only. They exercise authentication, CSRF, scopes/revocation, integer arithmetic, currency separation, filters/calendar boundaries, idempotency/draft exclusion, duplicate/privacy/file bounds, exports and persistence. Real Poppler validates test-generated bounded PDFs. Public official docs/catalog were read, but **live billable inference and production deployment remain unverified/not performed**.

The focused Vault contract suite uses only fake Redis, temporary Household SQLite, in-process TestClients and MockTransport. It verifies the named preset, HTTPS/no-default URL, actual MCP handshake/scoped tools, selected-agent grants, denied scopes and both Vault-grant and Household-token revocation. It never uses workspace Redis or a live Household deployment.