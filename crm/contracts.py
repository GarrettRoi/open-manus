"""Schemas used by discovery and validation, for every CRM adapter."""
BUSINESSES = ["dj_wedding", "real_estate", "other"]
STATUSES = ["new", "contacted", "qualified", "proposal", "won", "lost"]
LEAD_FIELDS = {
    key: {"type": "string", "maxLength": 1000}
    for key in ("name", "email", "phone", "company", "source_id", "external_id",
                "lead_type", "assigned_agent", "action_timeframe")
}
LEAD_FIELDS.update({
    "business": {"type": "string", "enum": BUSINESSES},
    "status": {"type": "string", "enum": STATUSES},
    "estimated_value": {"type": "string", "pattern": r"^\d+(\.\d{1,2})?$"},
    "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
    "custom_fields": {"type": "object"},
})
for key in ("acquisition_date", "next_action_date", "wedding_date"):
    LEAD_FIELDS[key] = {"type": "string", "format": "date"}
# Null explicitly clears an optional field. Business/status are lifecycle
# classifications and cannot be cleared. Empty strings remain valid only where
# the underlying string schema allows them.
for key, schema in list(LEAD_FIELDS.items()):
    if key not in {"business", "status"}:
        LEAD_FIELDS[key] = {"anyOf": [schema, {"type": "null"}]}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


S = {"type": "string"}
ID = {"id": S}
REV = {**ID, "revision": {"type": "integer", "minimum": 1}}
LEAD_SCHEMA = obj(LEAD_FIELDS)
READ_FILTERS = {
    **{k: S for k in ("query", "business", "status", "source_id", "assigned_agent")},
    "archived": {"type": "boolean"},
}
URGENCY = {"type": "string", "enum": ["all", "ready_now", "overdue"]}
PAGE = {"page": {"type": "integer", "minimum": 1, "maximum": 1000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
ACTIONS = {
    "discover": obj({}),
    "list": obj({**READ_FILTERS, **PAGE, "urgency": URGENCY,
                 "sort": {"type": "string", "enum": ["newest", "next_action"]}}),
    "summary": obj({**READ_FILTERS, "urgency": URGENCY}),
    "activity": obj({**READ_FILTERS, **PAGE}),
    "get": obj(ID, ["id"]),
    "roster": obj({}),
    "history": obj({**ID, "page": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["id"]),
    "notes": obj({**ID, "page": {"type": "integer", "minimum": 1},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["id"]),
    "create": obj({"lead": LEAD_SCHEMA, "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 200}}, ["lead", "idempotency_key"]),
    "update": obj({**REV, "changes": LEAD_SCHEMA}, ["id", "revision", "changes"]),
    "note": obj({**REV, "text": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["id", "revision", "text"]),
    "assign": obj({**REV, "assigned_agent": S}, ["id", "revision", "assigned_agent"]),
    "status": obj({**REV, "status": LEAD_FIELDS["status"]}, ["id", "revision", "status"]),
    "archive": obj(REV, ["id", "revision"]),
    "notifications": obj({"page": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    "retry": obj(ID, ["id"]),
    "sources": obj({}),
    "fields": obj({}),
    "settings": obj({}),
    "settings_save": obj({"revision": {"type": "integer", "minimum": 0},
                          "business_routing": obj({k: S for k in BUSINESSES})},
                         ["revision", "business_routing"]),
    "source_save": obj({**ID, "revision": {"type": "integer", "minimum": 1},
                        "name": S, "business": LEAD_FIELDS["business"],
                        "assigned_agent": S, "mapping": {"type": "object"},
                        "enabled": {"type": "boolean"}}, ["id"]),
    "source_rotate_secret": obj(ID, ["id"]),
    "field_save": obj({**ID, "revision": {"type": "integer", "minimum": 0},
                      "label": S, "type": {"enum": ["string", "number", "boolean", "date", "select"]},
                      "required": {"type": "boolean"}, "options": {"type": "array", "items": S, "maxItems": 100}}, ["id", "label", "type"]),
    "export": obj({}),
}
ADMIN = {"source_save", "source_rotate_secret", "field_save", "settings_save", "export"}

DESCRIPTIONS = {
    "discover": "Read allowed actions, exact input schemas and current typed custom-field definitions before calling other actions.",
    "list": "Search contact name/email/phone/company/external ID; filter business/status/source/assignee/archive. Returns items,total,page,limit; pages start at 1 and limit is at most 100.",
    "summary": "Count filtered leads by business/status and urgency. Returns timezone/today; ready_now is active and due today or earlier, overdue is strictly before today.",
    "activity": "Read paginated cross-lead audit activity, newest first, with lead links and authenticated actors. Filters use current lead values; archived defaults false.",
    "get": "Read a lead by ID with its current revision and latest 100 notes/history entries. Use notes/history actions for older entries.",
    "roster": "List canonical fleet agent names and roles to choose an explicit valid assignee.",
    "history": "Read attributable lead changes newest first; page starts at 1, limit is at most 100.",
    "notes": "Read lead notes newest first; page starts at 1, limit is at most 100.",
    "create": "Persist a lead and alert atomically. Supply a stable idempotency_key and reuse it on retries. Replays return the original lead; repeated contacts are flagged, not merged. Routing may remain unassigned.",
    "update": "Apply only supplied lead fields using the current revision. Null clears optional fields; null assigned_agent becomes empty and null custom_fields becomes {}. Business/status cannot be cleared. On conflict reload; never blindly retry an old revision. custom_fields replaces that entire object.",
    "note": "Append a note attributed to the authenticated caller using the current lead revision. On conflict reload before retrying.",
    "assign": "Assign a roster agent or clear assignment with an empty string; requires current lead revision. Active or already-chained notification reassignment may conflict.",
    "status": "Change a lead's lifecycle status using its current revision; on conflict reload before editing.",
    "archive": "Archive (not delete) a lead using its current revision. Records and history remain durable.",
    "notifications": "Read alert delivery status, attempts and safe errors; page starts at 1, limit is at most 100.",
    "retry": "Requeue an undelivered notification by notification ID, preserving its dispatch association. Active leases conflict; unassigned events still require assignment.",
    "sources": "Read safe source mappings, setup instructions, routing and delivery health. Credentials are never returned; generic delivery success is not verified provider-native support.",
    "fields": "Read current custom-field types, options, required flags and revisions.",
    "settings": "Read business-routing defaults and the settings revision.",
    "settings_save": "Owner only: replace business_routing using the current settings revision and canonical roster names.",
    "source_save": "Owner only: create or edit a generic source mapping/routing/enabled state. Existing sources require current revision; mapping maps lead fields to top-level payload keys.",
    "source_rotate_secret": "Owner only: rotate a source's bearer credential. Returns the secret once and immediately invalidates the old credential; keep it server-side.",
    "field_save": "Owner only: create or edit a typed custom field. Existing definitions require revision; existing types cannot change. Populate existing leads before requiring a field.",
    "export": "Owner only: export authoritative CRM records, settings and delivery state. Credentials and replay indexes require a Redis backup, not this redacted export.",
}