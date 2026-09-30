# Household API contract

Independent add-on service; no imports, storage sharing, or changes to fleet/CRM/vault services. UI owns `templates/index.html` and `static/`. All financial amounts are **integer cents** (supported currencies with 2 decimal minor units only). IDs are strings. Dates are `YYYY-MM-DD` in the configured household timezone. Production never has demo records; preview records and responses have `demo: true`.

## Browser authentication

Same-origin cookie `household_session` (HttpOnly, SameSite=Strict; Secure in production). Call `GET /api/session` first. For every mutation except login send `X-CSRF-Token: <csrf>` returned by session/login. JSON errors use `{"detail":"actionable message"}`. No public signup/password setup endpoint.

- `GET /api/session` → `{authenticated, csrf, user:{id,username,role}|null, preview, demo, timezone, currency, readiness:{ocr_available,provider,message}, limits:{upload_bytes,pdf_pages}, mcp_endpoint:"/mcp"}`. In preview auto-session has owner role, **isolated demo database only**, inference unavailable.
- `POST /api/login` `{username,password}` → session shape above. Rate limited; generic invalid-credential error.
- `POST /api/logout` `{}` → `{ok:true}`.
- `GET /api/people` → `{people:[{id,name,active}]}`
- `POST /api/people` `{name}` → person. Both users may add purchaser entities. Names are 1–100 characters.
- `PATCH /api/people/{id}` `{name?,active?}` → person.
- `GET /api/categories` → `{categories:[string]}` derived from records, plus suggested editable labels. Categories are not a fixed enum.

## Live model picker / upload

- `GET /api/models` (authenticated, optional `q`) → `{models:[{id,name,input_modalities,output_modalities,supports_images,supports_native_pdf,supports_documents,document_mode,pricing:{prompt,completion,image,request},input_usd_per_million,output_usd_per_million}],source:"https://openrouter.ai/api/v1/models",fetched_at,stale:false,parser:{engine:"cloudflare-ai",fee:string,max_pages:20},cost_notice:string}`.
  Prices are decimal strings or null, in USD; `pricing` retains raw official per-unit values. Native PDF requires explicit `file` metadata; other text-output models can process PDF text through OpenRouter's explicit `cloudflare-ai` file parser. Catalog failure is explicit 503, never a fake/hardcoded model list.
- `POST /api/uploads` multipart fields `file`, **`model`** (live catalog ID, required), `person` (purchaser ID), `source_type` = `receipt` or `price_screenshot` → **201 draft object**. PDF support uses native model PDF input or the explicit OpenRouter file parser, with local Poppler validation for format/page limits. Scanned PDF extraction quality depends on parser/model; missing fields stay flagged, never fabricated. Images: JPEG, PNG, WebP only; PDF ≤20 pages; upload ≤10 MiB. No inference configured → 503, never fake OCR.
  Exact duplicate → **409** `{detail:"Duplicate source already exists",duplicate:{kind:"draft"|"purchase",id}}`. Existing files are never public static assets. PDF engine is explicitly `native` for file-input models, otherwise `cloudflare-ai` (documented free parser, model tokens still charged). There is no automatic paid Mistral parser fallback.

## Editable draft / purchase shape

```
{
 id, source_type:"receipt"|"price_screenshot", store:string,
 date:"YYYY-MM-DD"|null, currency:"USD", person_id:string|null,
 items:[{
   id:string, label:string, normalized_label:string, category:string,
   quantity:string, unit_price_cents:integer|null, line_total_cents:integer|null
 }],
 subtotal_cents:integer|null, tax_cents:integer|null,
 discount_cents:integer|null, total_cents:integer|null,
 notes:string, warnings:[string], reconciliation:{
   item_total_cents:integer|null, calculated_total_cents:integer|null,
   difference_cents:integer|null, balanced:boolean
 },
 model:string|null, usage:object|null, source_available:boolean,
 created_at:string, updated_at:string, demo:boolean,
 status:"draft"|"confirmed", confirmed_purchase_id:string|null
}
```

Item line totals are **net of line-level discounts**. Receipt-level `discount_cents` is additional only, nonnegative; total = sum(lines)+tax-discount. `subtotal_cents` is informational and never added again. Categories are editable text. Quantity is positive decimal string (not money). Missing OCR fields stay null with warnings. OCR output is untrusted. Drafts never enter totals. Confirmation requires store/date/currency/person and every line label/normalized label/category/line total, positive quantity, nonnegative tax/discount, matching total; mismatch must be explicitly corrected before saving.

- `GET /api/drafts` → `{drafts:[draft]}` (latest 100; `limit` 1–100 and `offset` ≥0 supported; response also `total,limit,offset`).
- `GET /api/drafts/{id}` → draft.
- `PATCH /api/drafts/{id}` → draft. Accept only editable top-level fields above (store/date/currency/person_id/items/subtotal_cents/tax_cents/discount_cents/total_cents/notes). Sending `items` replaces all items. Server recomputes warnings/reconciliation, normalizes product labels if blank; partial edits allowed.
- `DELETE /api/drafts/{id}` → `{ok:true}`. Deletes private source when unreferenced.
- `POST /api/drafts/{id}/confirm` `{}` → `{purchase:purchase,already_confirmed:boolean}`; idempotent, moves one draft into confirmed history. Retained confirmed draft is read-only.
- `GET /api/drafts/{id}/source`, `GET /api/purchases/{id}/source` → authenticated private file.
- `GET /api/purchases/{id}` → purchase (same fields with `status:"confirmed"`).
- `PATCH /api/purchases/{id}` → purchase; same editable fields, must reconcile.
- `DELETE /api/purchases/{id}` → `{ok:true}`.
- `POST /api/purchases` → validated manual purchase (same editable fields plus optional `source_type:"receipt"|"price_screenshot"`, default `receipt`); useful when OCR unavailable. This endpoint is the user's explicit manual **Confirm purchase** action; it requires a complete reconciled purchase even for a price screenshot. Uploads still only create drafts, never automatic spending. `source_type` is preserved on subsequent edits but cannot be changed via PATCH.

## History / reporting

`GET /api/purchases`, `/api/summary`, `/api/export` accept inclusive local-date filters:
`start`, `end`, `person` (ID), `category` (case-insensitive exact), `q` (case-insensitive store/item/normalized-label search), `store` (substring). All dates validated; start ≤ end. History supports `limit` (1–100, default 30), `offset` (default 0).

- purchases → `{purchases:[purchase],total,limit,offset,demo}`.
- summary → `{currencies:[{currency,total_cents,purchase_count,item_total_cents,tax_cents,discount_cents,by_day:[{date,total_cents}],by_month:[{month,total_cents}],by_person:[{person_id,name,total_cents}],by_category:[{category,total_cents}],by_item:[{normalized_label,total_cents,quantity:string}]}],filters:object,basis:string,demo}`.
  Without item filters, headline/person/time totals use receipt total. With `category`/`q`, totals use matched **item lines only**, exclude receipt tax/discount and unrelated lines; a store match under `q` includes all lines. Category/item totals always use net item lines (exclude receipt-level tax/discount); this is stated in `basis`. No currency mixing. Empty data → empty currencies.
- `GET /api/export?format=json|csv` + filters exports **all matches**, no silent pagination. JSON `{purchases,summary,demo}`; CSV one row per matched item plus receipt amounts/metadata (explicit basis). Attachments download; up to 10,000 purchases per request, above returns 413 requiring narrower dates.

## Agent settings / MCP

Owner only token management (partner can use all household purchase APIs):
- `GET /api/agent-tokens` → `{tokens:[{id,name,scopes:[string],created_at,expires_at,last_used_at,revoked}]}` never secret/hash.
- `POST /api/agent-tokens` `{name,scopes:["purchases:read","summary:read"],expires_days:integer(1..365)}` → `{token:"hh_…",id,name,scopes,expires_at}` **secret returned once**. Name is 1–100 characters. Scope subset only. Store only hashed token.
- `DELETE /api/agent-tokens/{id}` → `{ok:true}` revocation immediately effective.
- `POST /mcp` Authorization Bearer token; JSON-RPC 2.0, stateless Streamable HTTP JSON responses. No write tools, no provider secrets, no automatic fleet grants. `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Tools `household_purchases` (purchases:read), `household_summary` (summary:read), `household_snapshot` (summary:read; month `YYYY-MM` or start/end, optional person/category/q), `household_items` (summary:read; item/category/date filters). Arguments reuse the above filters and pagination. Responses are MCP `content:[{type:"text",text:JSON-string}]` and `structuredContent`; tool failures use `isError:true`. Unsupported protocol/method/argument errors are JSON-RPC errors. Endpoint GET returns 405 (no SSE stream); initialization advertises only tools.

## Deployment / preview

`GET /health` is unauthenticated liveness only, no counts/financial data. `GET /` serves Jinja template; `/static` contains frontend assets only.
Backend launch: `python -m services.household.app` (HOUSEHOLD_PORT or PORT, default 8099). Operator setup CLI documented in README; production requires dedicated persistent storage, origin and owner credentials. Preview launch: `python scripts/household_preview.py`, binds `0.0.0.0` for Replit's shared preview proxy with an isolated marked temp database; remote visitors share fictional demo data only. No production DB, credentials, inference or MCP access; never enter real financial data in preview. All preview data unmistakably marked DEMO. Preview-only proxy Origin handling does not relax production auth/host/Origin checks.