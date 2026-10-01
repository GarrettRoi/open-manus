"""Transactional mutations and append-only audit, independent of HTTP transport."""
import hashlib
import json
import re

from fastapi import HTTPException


def migrate(db):
    for table, column, declaration in (
        ("records", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("records", "attribution", "TEXT NOT NULL DEFAULT '{}'"),
        ("records", "deleted_at", "TEXT"),
        ("agent_tokens", "agent_id", "TEXT"),
        ("uploads", "uploaded_by", "TEXT"),
    ):
        if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
            record_kind TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL,
            action TEXT NOT NULL, diff TEXT NOT NULL, tombstone TEXT);
        CREATE INDEX IF NOT EXISTS audit_record ON audit(record_id,id);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
            BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
            BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
        CREATE TABLE IF NOT EXISTS idempotency (
            actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            response TEXT NOT NULL, PRIMARY KEY(actor,key));
        CREATE TABLE IF NOT EXISTS batches (
            id TEXT PRIMARY KEY, model TEXT NOT NULL, person TEXT NOT NULL,
            source_type TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, status TEXT NOT NULL,
            filename TEXT NOT NULL, upload_id TEXT, draft_id TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, uploaded_by TEXT NOT NULL,
            duplicate_of TEXT, scan_id TEXT);
    """)


def actor_for(user):
    return {"kind": "human", "id": user["id"], "display": user["username"]}


class Domain:
    def __init__(self, store, validate, now):
        self.store, self.validate, self.now = store, validate, now

    def audit(self, db, ident, kind, actor, action, before=None, after=None, deleted=False):
        before, after = before or {}, after or {}
        diff = {k: {"before": before.get(k), "after": after.get(k)}
                for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
        db.execute("INSERT INTO audit(record_id,record_kind,actor,at,action,diff,tombstone) VALUES(?,?,?,?,?,?,?)",
                   (ident, kind, json.dumps(actor), self.now(), action, json.dumps(diff),
                    json.dumps(before) if deleted else None))

    def transact(self, actor, key, payload, operation):
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", key)):
            raise HTTPException(422, "Idempotency key must be 1..120 letters, digits, _, ., : or -.")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        identity = actor["kind"] + ":" + actor["id"]
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                prior = db.execute("SELECT * FROM idempotency WHERE actor=? AND key=?", (identity, key)).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise HTTPException(409, "Idempotency key already used for different arguments.")
                    return json.loads(prior["response"])
            result = operation(db)
            if key:
                db.execute("INSERT INTO idempotency VALUES(?,?,?,?)",
                           (identity, key, fingerprint, json.dumps(result)))
            return result

    def insert(self, db, doc, kind, actor, upload_id=None, attribution=None):
        ident = self.store.insert(doc, kind, upload_id, db)
        attrs = attribution or {"created_by": actor, "uploaded_by": None,
                                "last_edited_by": None, "confirmed_by": actor if kind == "purchase" else None}
        db.execute("UPDATE records SET attribution=? WHERE id=?", (json.dumps(attrs), ident))
        result = self.store.present(db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone())
        self.audit(db, ident, kind, actor, "create", after=result)
        return result

    def mutate(self, action, kind, actor, body=None, ident=None, version=None, key=None):
        body = dict(body or {})
        if version is not None and (type(version) is not int or version < 1):
            raise HTTPException(422, "Version must be a positive integer.")
        if action == "confirm" and body:
            raise HTTPException(422, "Confirm accepts no editable fields; edit the draft first.")
        def operation(db):
            if action == "create":
                source = body.pop("source_type", "receipt")
                if source not in ("receipt", "price_screenshot"):
                    raise HTTPException(422, "Invalid source_type.")
                doc = self.validate(body, self.store, strict=kind == "purchase")
                doc["source_type"] = source
                return self.insert(db, doc, kind, actor)
            row = db.execute("SELECT * FROM records WHERE id=? AND kind=? AND deleted_at IS NULL",
                             (ident, kind)).fetchone()
            if not row:
                raise HTTPException(404, "Record not found.")
            before = self.store.present(row)
            if version is not None and row["version"] != version:
                raise HTTPException(409, "Stale version. Reload the record and review changes.")
            if action == "confirm" and row["confirmed_purchase_id"]:
                purchase = db.execute("SELECT * FROM records WHERE id=? AND deleted_at IS NULL",
                                      (row["confirmed_purchase_id"],)).fetchone()
                if not purchase:
                    raise HTTPException(409, "Confirmed purchase was deleted.")
                return {"purchase": self.store.present(purchase), "already_confirmed": True}
            if row["confirmed_purchase_id"]:
                raise HTTPException(409, "Draft already confirmed. Edit its purchase instead.")
            attrs = json.loads(row["attribution"])
            if action == "edit":
                doc = self.validate(body, self.store, json.loads(row["doc"]), strict=kind == "purchase")
                attrs["last_edited_by"] = actor
                db.execute("UPDATE records SET doc=?,attribution=?,version=version+1,updated_at=? WHERE id=?",
                           (json.dumps(doc), json.dumps(attrs), self.now(), ident))
            elif action == "delete":
                # Retained sources and records are private; tombstones never contribute to totals.
                related = db.execute("SELECT * FROM records WHERE confirmed_purchase_id=? AND deleted_at IS NULL",
                                     (ident,)).fetchall()
                for draft in related:
                    self.audit(db, draft["id"], "draft", actor, "delete",
                               before=self.store.present(draft), deleted=True)
                db.execute("UPDATE records SET deleted_at=?,updated_at=?,version=version+1 "
                           "WHERE id=? OR confirmed_purchase_id=?", (self.now(), self.now(), ident, ident))
                self.audit(db, ident, kind, actor, "delete", before=before, deleted=True)
                return {"ok": True}
            elif action == "confirm":
                doc = self.validate({}, self.store, json.loads(row["doc"]), strict=True)
                attrs["confirmed_by"] = actor
                purchase = self.insert(db, doc, "purchase", actor, row["upload_id"], attrs)
                db.execute("UPDATE records SET confirmed_purchase_id=?,attribution=?,version=version+1,updated_at=? WHERE id=?",
                           (purchase["id"], json.dumps(attrs), self.now(), ident))
            else:
                raise HTTPException(422, "Unknown mutation.")
            after = self.store.present(db.execute("SELECT * FROM records WHERE id=?", (ident,)).fetchone())
            self.audit(db, ident, kind, actor, action, before, after)
            return {"purchase": purchase, "already_confirmed": False} if action == "confirm" else after
        return self.transact(actor, key, [action, kind, ident, version, body], operation)

    def history(self, ident=None, kind=None, limit=30, offset=0):
        where, params = [], []
        if ident:
            where.append("record_id=?")
            params.append(ident)
        if kind:
            where.append("record_kind=?")
            params.append(kind)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.store.db() as db:
            total = db.execute("SELECT COUNT(*) FROM audit" + clause, params).fetchone()[0]
            rows = db.execute("SELECT * FROM audit" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
        return {"events": [{**dict(r), **{k: json.loads(r[k]) if r[k] else None
                                        for k in ("actor", "diff", "tombstone")}} for r in rows],
                "total": total, "limit": limit, "offset": offset}