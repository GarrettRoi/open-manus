"""Scoped, explicit schemas for agent mutations; identity never comes from arguments."""
import base64
import binascii
import hashlib

from fastapi import HTTPException

from services.household.queue import MAX_FILE


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STRING = {"type": "string"}
IDENT = {"type": "string", "minLength": 1, "maxLength": 120}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,120}$",
       "description": "Stable retry key; use a new key only for a new intentional action."}
VERSION = {"type": "integer", "minimum": 1, "description": "Current record version; reload after conflict."}
MONEY = {"type": ["integer", "null"], "minimum": 0, "maximum": 100000000}
ITEM = obj({"id": STRING, "label": STRING, "normalized_label": STRING, "category": STRING,
            "quantity": {"type": "string", "description": "Positive decimal string, e.g. 1 or 0.5"},
            "unit_price_cents": MONEY, "line_total_cents": MONEY},
           ("label", "category", "quantity", "line_total_cents"))
FIELDS = {"store": STRING, "date": {"type": ["string", "null"]},
          "currency": STRING, "person_id": {"type": ["string", "null"]},
          "items": {"type": "array", "maxItems": 200, "items": ITEM}, "notes": STRING,
          **{k: MONEY for k in ("subtotal_cents", "tax_cents", "discount_cents", "total_cents")}}
SOURCE = {"type": "string", "enum": ["receipt", "price_screenshot"]}
PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000000}}
TOOLS = []
SCOPES = {}


def tool(name, scope, description, properties, required=(), write=False, destructive=False):
    name = "household_" + name
    SCOPES[name] = scope
    TOOLS.append({"name": name, "description": description, "inputSchema": obj(properties, required),
                  "annotations": {"readOnlyHint": not write, "destructiveHint": destructive,
                                  "idempotentHint": not write or "idempotency_key" in properties,
                                  "openWorldHint": scope == "uploads:create"}})


tool("purchase_create", "purchases:write", "Explicitly confirm a complete manual purchase. Never use for an unpurchased price screenshot. Amounts are integer cents; purchaser is independent of server actor.",
     {"purchase": obj({**FIELDS, "source_type": SOURCE}, ("store", "date", "currency", "person_id", "items", "tax_cents", "discount_cents", "total_cents")),
      "idempotency_key": KEY}, ("purchase", "idempotency_key"), True)
for kind, scope in (("purchase", "purchases:write"), ("draft", "drafts:write")):
    tool(kind + "_edit", scope, "Edit allowed fields only. Sending items replaces all lines. Attribution is immutable. Purchase edits must reconcile.",
         {"id": IDENT, "changes": obj(FIELDS), "version": VERSION, "idempotency_key": KEY},
         ("id", "changes", "version", "idempotency_key"), True)
tool("purchase_delete", "purchases:delete", "Delete a confirmed purchase from totals; immutable audit tombstone remains.",
     {"id": IDENT, "version": VERSION, "idempotency_key": KEY}, ("id", "version", "idempotency_key"), True, True)
tool("draft_confirm", "drafts:confirm", "Explicitly authorize spending history entry from a complete reconciled draft. A screenshot alone is not proof of purchase.",
     {"id": IDENT, "version": VERSION, "idempotency_key": KEY}, ("id", "version", "idempotency_key"), True)
tool("drafts", "drafts:read", "List unconfirmed review drafts; never included in spending.", PAGE)
tool("draft", "drafts:read", "Get draft details including validation warnings, attribution and version.", {"id": IDENT}, ("id",))
tool("purchase", "purchases:read", "Get one confirmed purchase, current version and server-derived attribution.", {"id": IDENT}, ("id",))
tool("people", "purchases:read", "List purchaser entity IDs for entries; these are not actor identities.", {})
tool("models", "uploads:create", "Live OpenRouter catalog and published token pricing; review price before explicitly uploading.", {})
tool("upload", "uploads:create", "Upload one image/PDF for one explicitly selected model scan; returns durable job, never a confirmed purchase. No URL fetch. Maximum file 5 MiB; failures require explicit cost-aware retry.",
     {"filename": {"type": "string", "maxLength": 160}, "mime": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp", "application/pdf", "application/octet-stream"]},
      "data_base64": {"type": "string", "maxLength": 6990508},
      "model": {"type": "string", "maxLength": 200}, "person": IDENT, "source_type": SOURCE, "idempotency_key": KEY},
     ("filename", "mime", "data_base64", "model", "person", "idempotency_key"), True)
tool("job", "drafts:read", "Get durable upload status and draft_id; failed sources remain accessible to signed-in humans.", {"id": IDENT}, ("id",))
tool("job_retry", "uploads:create", "Explicit retry only: prior attempt may already have been billed; acknowledge_cost must be true.",
     {"id": IDENT, "acknowledge_cost": {"type": "boolean", "const": True}, "idempotency_key": KEY},
     ("id", "acknowledge_cost", "idempotency_key"), True)
tool("job_cancel", "uploads:create", "Cancel queued/failed/interrupted work. Running inference cannot be cancelled safely.",
     {"id": IDENT, "idempotency_key": KEY}, ("id", "idempotency_key"), True)
tool("audit", "purchases:read", "Read immutable purchase audit, including deleted tombstones. Draft audit requires drafts:read.",
     {"id": IDENT, "kind": {"type": "string", "enum": ["purchase", "draft"]}, **PAGE})
tool("draft_audit", "drafts:read", "Read immutable draft audit including deleted draft tombstones.", {"id": IDENT, **PAGE})


def validate_arguments(schema, args):
    """Validate structural JSON schema constraints without coercing caller values."""
    import re
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    matches = {"string": isinstance(args, str), "integer": type(args) is int,
               "boolean": type(args) is bool, "null": args is None,
               "object": isinstance(args, dict), "array": isinstance(args, list)}
    if not any(matches.get(t) for t in types):
        raise HTTPException(422, "Tool argument has invalid type.")
    if "enum" in schema and args not in schema["enum"] or "const" in schema and args != schema["const"]:
        raise HTTPException(422, "Tool argument is not an allowed value.")
    if isinstance(args, dict):
        properties = schema.get("properties", {})
        if set(args) - set(properties) or set(schema.get("required", [])) - set(args):
            raise HTTPException(422, "Unknown or missing tool arguments.")
        for key, value in args.items():
            validate_arguments(properties[key], value)
    if isinstance(args, list):
        if len(args) > schema.get("maxItems", 1000000):
            raise HTTPException(422, "Too many items.")
        for value in args:
            validate_arguments(schema["items"], value)
    if isinstance(args, str):
        if not schema.get("minLength", 0) <= len(args) <= schema.get("maxLength", 10000000):
            raise HTTPException(422, "Tool text length is outside bounds.")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], args):
            raise HTTPException(422, "Tool text does not match required format.")
    if type(args) is int and not schema.get("minimum", -10**20) <= args <= schema.get("maximum", 10**20):
        raise HTTPException(422, "Tool integer is outside bounds.")


class AgentTools:
    def __init__(self, store, domain, queue, provider, pagination):
        self.store, self.domain, self.queue = store, domain, queue
        self.provider, self.pagination = provider, pagination

    async def call(self, name, args, actor, scopes):
        schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
        validate_arguments(schema, args)
        name = name.removeprefix("household_")
        if name in {"purchase_create", "purchase_edit", "purchase_delete", "draft_edit", "draft_confirm"}:
            kind, action = name.split("_")
            return self.domain.mutate(action, "purchase" if kind == "purchase" else "draft", actor,
                                      args.get("purchase", args.get("changes")), args.get("id"),
                                      args.get("version"), args["idempotency_key"])
        if name == "people":
            return {"people": self.store.people()}
        if name == "models":
            return await self.provider.catalog()
        if name == "draft":
            return self.store.read(args["id"], "draft")
        if name == "purchase":
            return self.store.read(args["id"], "purchase")
        if name == "drafts":
            limit, offset = self.pagination(args)
            with self.store.db() as db:
                where = " WHERE kind='draft' AND confirmed_purchase_id IS NULL AND deleted_at IS NULL"
                total = db.execute("SELECT COUNT(*) FROM records" + where).fetchone()[0]
                rows = db.execute("SELECT * FROM records" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                  (limit, offset)).fetchall()
            return {"drafts": [self.store.present(r) for r in rows], "total": total, "limit": limit, "offset": offset}
        if name in {"audit", "draft_audit"}:
            kind = "draft" if name == "draft_audit" else args.get("kind", "purchase")
            if kind == "draft" and "drafts:read" not in scopes:
                raise HTTPException(403, "Token lacks drafts:read.")
            limit, offset = self.pagination(args)
            return self.domain.history(args.get("id"), kind, limit, offset)
        if name == "job":
            return self.queue.status(args["id"])
        if name in {"job_retry", "job_cancel"}:
            return self.queue.control(args["id"], name.split("_")[1], actor,
                                      args.get("acknowledge_cost", False), args["idempotency_key"])
        if name == "upload":
            try:
                data = base64.b64decode(args["data_base64"], validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, "data_base64 must be valid base64 (not a URL/data URI).")
            if not data or len(data) > MAX_FILE:
                raise HTTPException(413, "Decoded file must be nonempty and at most 5 MiB.")
            batch = await self.queue.batch(actor, {k: args[k] for k in ("model", "person", "source_type") if k in args},
                                           hashlib.sha256((args["idempotency_key"] + ":batch").encode()).hexdigest())
            return self.queue.reserve(actor, batch["id"], data, args["mime"], args["filename"], args["idempotency_key"])
        raise HTTPException(422, "Unknown tool.")