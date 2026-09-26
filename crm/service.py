"""One validated domain service for dashboard, agents and website deliveries."""
import hashlib
import json
import math
import re
import secrets
import time
import uuid
from datetime import date

from crm.contracts import ACTIONS, ADMIN, BUSINESSES, DESCRIPTIONS, LEAD_FIELDS, STATUSES
from crm.store import CRMStore, PREFIX


class CRMError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class CRMService:
    def __init__(self, redis_client=None):
        self.store = CRMStore(redis_client)
        self.redis = self.store.redis

    def execute(self, action, args=None, actor="unknown", role="agent"):
        try:
            import jsonschema
            if role not in {"owner", "agent", "source"}:
                raise CRMError("forbidden", "Unknown CRM role")
            if not isinstance(action, str) or action not in ACTIONS:
                raise CRMError("validation", "Unknown action; call discover")
            if (action in ADMIN and role != "owner") or (role == "source" and action != "create"):
                raise CRMError("forbidden", "This action requires owner access")
            try:
                jsonschema.validate({} if args is None else args, ACTIONS[action])
            except jsonschema.ValidationError:
                raise CRMError("validation", "Arguments do not match the discovered action schema")
            result = self._execute(action, args or {}, actor, role)
            return {"ok": True, "result": result}
        except CRMError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception:
            # Never return Redis connection URLs, submitted payloads or credentials.
            return {"ok": False, "error": {"code": "unavailable", "message": "CRM storage unavailable; retry later"}}

    def sources(self):
        defaults = [{"id": ident, "name": domain, "revision": 1, "enabled": False,
                     "connection_status": "unconnected", "mapping": {}, "assigned_agent": "",
                     "setup": "Generic server-to-server JSON only. Provider-native support is unverified. Configure mapping and routing, rotate a secret, enable, then test.",
                     "authentication": "Authorization: Bearer <source secret>",
                     "example": {"event_id": "unique-test-event", "lead": {"name": "Example lead"}}}
                    for ident, domain in [("canaok", "canaok.com"), ("vowsok", "vowsok.com"),
                                          ("webinarninja", "webinarninja.com"), ("mcgarryhomesokc", "mcgarryhomesokc.com")]]
        stored = {s["id"]: s for s in self.store.records("sources")}
        for item in defaults:
            stored.setdefault(item["id"], item)
        output = []
        for source in stored.values():
            item = {k: v for k, v in source.items() if k != "secret_hash"}
            health = self.redis.hget(PREFIX + "source_health", source["id"])
            if health:
                item.update(json.loads(health))
            item["connected"] = bool(item.get("last_delivery_ok"))
            item["instructions"] = item.get("setup", "Configure a generic server-side POST integration, set routing and mapping, rotate a secret, enable, then test. Provider-native webhook support is unverified.")
            item["auth_requirements"] = "Authorization: Bearer <per-source-secret>"
            item["example_payload"] = item.get("example", {"event_id": "unique-test-event", "lead": {"name": "Example lead"}})
            output.append(item)
        return output

    def _agent(self, name):
        if name and not self.redis.get("dispatch:roster:" + name):
            raise CRMError("validation", "Assigned agent must exist in the fleet roster")

    def _validate_lead(self, lead):
        if not any(lead.get(k, "").strip() for k in ("name", "email", "phone")):
            raise CRMError("validation", "At least one contact name, email or phone is required")
        if lead.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", lead["email"]):
            raise CRMError("validation", "Invalid email address")
        for key in ("acquisition_date", "next_action_date", "wedding_date"):
            if lead.get(key):
                try:
                    if date.fromisoformat(lead[key]).isoformat() != lead[key]:
                        raise ValueError()
                except ValueError:
                    raise CRMError("validation", f"{key} must be YYYY-MM-DD")
        self._agent(lead.get("assigned_agent", ""))
        if lead.get("source_id") and lead["source_id"] not in {s["id"] for s in self.sources()}:
            raise CRMError("validation", "Unknown source")
        fields = {f["id"]: f for f in self.store.records("fields")}
        custom = lead.get("custom_fields", {})
        if len(custom) > 100:
            raise CRMError("validation", "At most 100 custom fields are allowed")
        if set(custom) - set(fields):
            raise CRMError("validation", "Unknown custom field")
        for key, field in fields.items():
            value = custom.get(key)
            if value is None:
                if field.get("required"):
                    raise CRMError("validation", f"Required custom field: {key}")
                continue
            if field.get("required") and isinstance(value, str) and not value.strip():
                raise CRMError("validation", f"Required custom field cannot be empty: {key}")
            typ = field["type"]
            valid = ((typ in {"string", "date", "select"} and isinstance(value, str) and len(value) <= 4000)
                     or (typ == "number" and type(value) in (int, float))
                     or (typ == "boolean" and type(value) is bool))
            if not valid:
                raise CRMError("validation", f"Invalid custom field type: {key}")
            if typ == "number" and not math.isfinite(value):
                raise CRMError("validation", f"Custom field must be finite: {key}")
            if typ == "select" and value not in field.get("options", []):
                raise CRMError("validation", f"Invalid custom field option: {key}")
            if typ == "date":
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
                except ValueError:
                    raise CRMError("validation", f"Invalid custom field date: {key}")

    def _execute(self, action, args, actor, role):
        if action == "discover":
            return {"actions": [{"name": k, "description": DESCRIPTIONS[k],
                                 "input_schema": v} for k, v in ACTIONS.items() if role == "owner" or k not in ADMIN],
                    "lead_fields": LEAD_FIELDS, "fields": self.store.records("fields"),
                    "businesses": BUSINESSES, "statuses": STATUSES}
        if action == "sources":
            return {"items": self.sources()}
        if action == "fields":
            return {"items": self.store.records("fields")}
        if action == "roster":
            from tools.agent_dispatch import get_roster
            return {"items": [{"agent": r.get("agent", ""), "role": r.get("role", "")}
                              for r in get_roster(self.redis)]}
        if action == "settings":
            return self.settings()
        if action in {"history", "notes"}:
            lead = self.store.lead(args["id"])
            if not lead:
                raise CRMError("not_found", "Lead not found")
            return self._page(list(reversed(lead[action])), args)
        if action == "get":
            lead = self.store.lead(args["id"])
            if not lead:
                raise CRMError("not_found", "Lead not found")
            return self._detail(lead)
        if action in {"list", "summary"}:
            rows = self.store.records("leads")
            rows = [r for r in rows if r["archived"] == args.get("archived", False)]
            for k in ("business", "status", "source_id", "assigned_agent"):
                if k in args:
                    rows = [r for r in rows if r.get(k) == args[k]]
            if args.get("query"):
                query = args["query"].casefold()
                rows = [r for r in rows if query in " ".join(str(r.get(k, "")) for k in ("name", "email", "phone", "company", "external_id")).casefold()]
            if action == "summary":
                return {"total": len(rows), "by_business": {k: sum(r.get("business") == k for r in rows) for k in BUSINESSES},
                        "by_status": {k: sum(r.get("status") == k for r in rows) for k in STATUSES}}
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return self._page([{k: v for k, v in r.items() if k not in {"notes", "history"}} for r in rows], args)
        if action == "notifications":
            return self._page(sorted(self.store.events(), key=lambda e: e["created_at"], reverse=True), args)
        if action == "export":
            return {"version": 1, "exported_at": time.time(), "leads": self.store.records("leads"),
                    "fields": self.store.records("fields"), "sources": self.sources(),
                    "settings": self.settings(), "notifications": self.store.events()}
        return self._write(action, args, actor)

    @staticmethod
    def _normalize_optional_fields(lead):
        """Apply explicit clear semantics without storing inconsistent nulls."""
        for key in list(lead):
            if key in LEAD_FIELDS and lead[key] is None:
                if key == "assigned_agent":
                    lead[key] = ""
                elif key == "custom_fields":
                    lead[key] = {}
                else:
                    del lead[key]

    @staticmethod
    def _detail(lead):
        return {**lead, "notes": lead["notes"][-100:], "history": lead["history"][-100:],
                "notes_total": len(lead["notes"]), "history_total": len(lead["history"])}

    @staticmethod
    def _page(rows, args):
        page, limit = args.get("page", 1), args.get("limit", 50)
        return {"items": rows[(page - 1) * limit:page * limit], "total": len(rows), "page": page, "limit": limit}

    def settings(self):
        raw = self.redis.hget(PREFIX + "settings", "routing")
        return json.loads(raw) if raw else {"revision": 0, "business_routing": {}}

    def _write(self, action, args, actor):
        from redis.exceptions import WatchError
        keys = [PREFIX + x for x in ("leads", "outbox", "dedup", "sources", "fields", "settings")]
        for _ in range(10):
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(*keys)
                    writes = []
                    now = time.time()
                    audit_changes = {}
                    if action == "create":
                        dedup = hashlib.sha256((actor + "\0" + args["idempotency_key"]).encode()).hexdigest()
                        previous = pipe.hget(PREFIX + "dedup", dedup)
                        if previous:
                            return self._detail(self.store.lead(previous))
                        lead = {"business": "other", "status": "new", "assigned_agent": "", "custom_fields": {},
                                **args["lead"], "id": str(uuid.uuid4()), "revision": 1,
                                "created_at": now, "updated_at": now, "archived": False, "notes": [], "history": []}
                        self._normalize_optional_fields(lead)
                        source = next((s for s in self.sources() if s["id"] == lead.get("source_id")), {})
                        if "business" not in args["lead"]:
                            lead["business"] = source.get("business", "other")
                        if not lead["assigned_agent"]:
                            lead["assigned_agent"] = (source.get("assigned_agent")
                                                      or self.settings()["business_routing"].get(lead["business"], ""))
                        self._validate_lead(lead)
                        audit_changes = {k: {"old": None, "new": v} for k, v in args["lead"].items()}
                        lead["duplicate_ids"] = [r["id"] for r in self.store.records("leads")
                                                 if any(lead.get(k) and lead[k].casefold() == r.get(k, "").casefold() for k in ("email", "phone"))]
                        event_id = str(uuid.uuid4())
                        event = {"id": event_id, "lead_id": lead["id"], "recipient": lead["assigned_agent"],
                                 "status": "pending" if lead["assigned_agent"] else "unassigned",
                                 "attempts": 0, "next_attempt_at": 0, "created_at": now,
                                 "updated_at": now, "error": "", "chain_id": ""}
                        writes.extend([("dedup", dedup, lead["id"]), ("outbox", event_id, json.dumps(event))])
                    elif action in {"update", "note", "assign", "status", "archive"}:
                        lead = self.store.lead(args["id"])
                        if not lead:
                            raise CRMError("not_found", "Lead not found")
                        if lead["revision"] != args["revision"]:
                            raise CRMError("conflict", "Lead changed; reload before editing")
                        changes = args.get("changes", {})
                        if action == "assign":
                            changes = {"assigned_agent": args["assigned_agent"]}
                        if action == "status":
                            changes = {"status": args["status"]}
                        audit_changes = {k: {"old": lead.get(k), "new": v} for k, v in changes.items()}
                        lead.update(changes)
                        self._normalize_optional_fields(lead)
                        if action == "archive":
                            audit_changes = {"archived": {"old": lead["archived"], "new": True}}
                            lead["archived"] = True
                        if action == "note":
                            lead["notes"].append({"id": str(uuid.uuid4()), "actor": actor, "text": args["text"], "created_at": now})
                        self._validate_lead(lead)
                        lead["revision"] += 1
                        lead["updated_at"] = now
                        if "assigned_agent" in changes:
                            for event in self.store.events():
                                if event["lead_id"] == lead["id"] and event["status"] != "delivered":
                                    if event.get("chain_id") and event["recipient"] != lead["assigned_agent"]:
                                        raise CRMError("conflict", "Notification dispatch already started; resolve its chain before reassigning")
                                    if event.get("lease_until", 0) > now:
                                        raise CRMError("conflict", "Notification delivery is in progress; retry after it finishes")
                                    event.update(recipient=lead["assigned_agent"], status="pending" if lead["assigned_agent"] else "unassigned",
                                                 attempts=0, next_attempt_at=0, updated_at=now)
                                    event.pop("lease_token", None)
                                    event.pop("lease_until", None)
                                    writes.append(("outbox", event["id"], json.dumps(event)))
                    elif action == "retry":
                        raw = pipe.hget(PREFIX + "outbox", args["id"])
                        if not raw:
                            raise CRMError("not_found", "Notification not found")
                        event = json.loads(raw)
                        if event["status"] == "delivered":
                            raise CRMError("conflict", "Notification already delivered")
                        if event.get("lease_until", 0) > now:
                            raise CRMError("conflict", "Notification delivery is in progress")
                        event.update(status="pending" if event["recipient"] else "unassigned", attempts=0,
                                     next_attempt_at=0, error="", updated_at=now)
                        event.pop("lease_token", None)
                        event.pop("lease_until", None)
                        writes.append(("outbox", event["id"], json.dumps(event)))
                        result = event
                    elif action == "settings_save":
                        settings = self.settings()
                        if settings["revision"] != args["revision"]:
                            raise CRMError("conflict", "Routing settings changed; reload before editing")
                        for name in args["business_routing"].values():
                            self._agent(name)
                        result = {**args, "revision": settings["revision"] + 1,
                                  "updated_by": actor, "updated_at": now}
                        writes.append(("settings", "routing", json.dumps(result)))
                    elif action == "field_save":
                        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", args["id"]):
                            raise CRMError("validation", "Invalid field ID")
                        old = pipe.hget(PREFIX + "fields", args["id"])
                        old_revision = json.loads(old).get("revision", 1) if old else 0
                        if old and args.get("revision") != old_revision:
                            raise CRMError("conflict", "Field definition changed; reload before editing")
                        if old and json.loads(old)["type"] != args["type"]:
                            raise CRMError("conflict", "Existing field types cannot be changed")
                        if not old and self.redis.hlen(PREFIX + "fields") >= 100:
                            raise CRMError("validation", "At most 100 field definitions are allowed")
                        if args.get("required"):
                            values = [r.get("custom_fields", {}).get(args["id"]) for r in self.store.records("leads")]
                            if any(v is None or (isinstance(v, str) and not v.strip()) for v in values):
                                raise CRMError("conflict", "Populate this field on existing leads before making it required")
                        if old and json.loads(old).get("options") != args.get("options"):
                            for record in self.store.records("leads"):
                                value = record.get("custom_fields", {}).get(args["id"])
                                if value is not None and args["type"] == "select" and value not in args.get("options", []):
                                    raise CRMError("conflict", "Existing leads use an option that would be removed")
                        result = {**args, "revision": old_revision + 1, "updated_by": actor, "updated_at": now}
                        writes.append(("fields", args["id"], json.dumps(result)))
                    else:
                        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args["id"]):
                            raise CRMError("validation", "Invalid source ID")
                        raw = pipe.hget(PREFIX + "sources", args["id"])
                        if not raw and self.redis.hlen(PREFIX + "sources") >= 100:
                            raise CRMError("validation", "At most 100 sources are allowed")
                        source = json.loads(raw) if raw else next((s for s in self.sources() if s["id"] == args["id"]), {"id": args["id"], "revision": 0, "enabled": False, "mapping": {}, "connection_status": "unconnected"})
                        if action == "source_save":
                            if source["revision"] and args.get("revision") != source["revision"]:
                                raise CRMError("conflict", "Source changed; reload before editing")
                            self._agent(args.get("assigned_agent", ""))
                            mapping = args.get("mapping", {})
                            if any(k not in LEAD_FIELDS or not isinstance(v, str) or len(v) > 100 for k, v in mapping.items()):
                                raise CRMError("validation", "Mapping must map lead fields to top-level payload keys")
                            source.update(args)
                            result = {k: v for k, v in source.items() if k != "secret_hash"}
                        else:
                            secret = secrets.token_urlsafe(32)
                            source["secret_hash"] = hashlib.sha256(secret.encode()).hexdigest()
                            result = {"secret": secret}
                        source["revision"] += 1
                        source["updated_by"] = actor
                        source["updated_at"] = now
                        if action == "source_save":
                            result["revision"] = source["revision"]
                        writes.append(("sources", source["id"], json.dumps(source)))
                    if action in {"create", "update", "note", "assign", "status", "archive"}:
                        lead["history"].append({"action": action, "actor": actor, "at": now,
                                                "revision": lead["revision"], "fields": sorted(audit_changes),
                                                "changes": audit_changes})
                        writes.append(("leads", lead["id"], json.dumps(lead)))
                        result = self._detail(lead)
                    pipe.multi()
                    for name, ident, value in writes:
                        pipe.hset(PREFIX + name, ident, value)
                    pipe.execute()
                    return result
                except WatchError:
                    continue
        raise CRMError("conflict", "Concurrent CRM change; retry")