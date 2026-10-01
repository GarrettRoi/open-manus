"""Small SQLite-backed queue. No automatic retry of potentially billable work."""
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException

MAX_FILE = 5 * 1024 * 1024


class ScanQueue:
    def __init__(self, store, provider, domain, validate_file, validate_doc, uid, now, editable):
        self.store, self.provider, self.domain = store, provider, domain
        self.validate_file, self.validate_doc = validate_file, validate_doc
        self.uid, self.now, self.editable = uid, now, editable
        self.tasks = []
        self.wake = asyncio.Event()

    async def start(self):
        with self.store.db() as db:
            db.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')",
                       ("Service restarted. Review possible billing before explicitly retrying.", self.now()))
            db.execute("UPDATE scans SET status='interrupted' WHERE status='running'")
        self.tasks = [asyncio.create_task(self.worker()) for _ in range(2)]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    async def batch(self, actor, body, key=None):
        if set(body) - {"model", "person", "source_type"}:
            raise HTTPException(422, "Batch accepts model, person, source_type only.")
        if not self.provider.readiness()["ocr_available"]:
            raise HTTPException(503, self.provider.readiness()["message"])
        model, person, source = body.get("model"), body.get("person"), body.get("source_type", "receipt")
        if not isinstance(model, str) or len(model) > 200:
            raise HTTPException(422, "Select a live model.")
        if source not in ("receipt", "price_screenshot"):
            raise HTTPException(422, "Invalid source_type.")
        if not any(p["id"] == person and p["active"] for p in self.store.people()):
            raise HTTPException(422, "Choose an active purchaser.")
        catalog = await self.provider.catalog()
        if not any(m["id"] == model for m in catalog["models"]):
            raise HTTPException(422, "Choose a model from the current live catalog.")
        def create(db):
            if db.execute("SELECT COUNT(*) FROM batches WHERE created_at>=?",
                          (self.now()[:10],)).fetchone()[0] >= 100:
                raise HTTPException(429, "Daily batch cap reached (100).")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 40:
                raise HTTPException(429, "Queue full (40 pending files). Wait for existing files.")
            ident = self.uid()
            db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)",
                       (ident, model, person, source, self.now(), json.dumps(actor)))
            return self.batch_status(ident, db)
        return self.domain.transact(actor, key, ["batch", body], create)

    def batch_status(self, ident, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.batch_status(ident, connection)
        row = db.execute("SELECT * FROM batches WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "Batch not found.")
        jobs = db.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at,id", (ident,)).fetchall()
        return {**dict(row), "created_by": json.loads(row["created_by"]), "jobs": [self.present(r) for r in jobs]}

    def present(self, row):
        return {k: json.loads(row[k]) if k == "uploaded_by" else row[k] for k in row.keys() if k != "scan_id"}

    def status(self, ident, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.status(ident, connection)
        row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "Job not found.")
        return self.present(row)

    def reserve(self, actor, batch_id, data, mime, filename, key=None, maximum=MAX_FILE):
        if not data or len(data) > maximum:
            raise HTTPException(413, f"File must be nonempty and at most {maximum // (1024 * 1024)} MiB.")
        # File decoding/validation precedes reservation, but inference never does.
        cleaned, mime, extension = self.validate_file(data, mime, filename, self.store.files)
        fingerprint = hashlib.sha256(cleaned).hexdigest()
        filename = re.sub(r"[^\w .()-]", "_", Path(filename or "receipt").name)[:160]
        written = []
        def create(db):
            self.batch_status(batch_id, db)
            if db.execute("SELECT COUNT(*) FROM jobs WHERE created_at>=?",
                          (self.now()[:10],)).fetchone()[0] >= 100:
                raise HTTPException(429, "Daily uploaded-file cap reached (100).")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE batch_id=?", (batch_id,)).fetchone()[0] >= 20:
                raise HTTPException(422, "A batch supports at most 20 files. Create another batch.")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 40:
                raise HTTPException(429, "Queue full (40 pending files).")
            duplicate = db.execute("SELECT id FROM uploads WHERE hash=?", (fingerprint,)).fetchone()
            ident, upload_id, now = self.uid(), self.uid(), self.now()
            status, duplicate_of = "queued", None
            if duplicate:
                status, upload_id = "duplicate", duplicate["id"]
                prior = db.execute("SELECT id FROM jobs WHERE upload_id=? AND status!='duplicate' ORDER BY created_at LIMIT 1",
                                   (upload_id,)).fetchone()
                duplicate_of = prior["id"] if prior else upload_id
            else:
                path = self.store.files / (upload_id + extension)
                path.write_bytes(cleaned)
                os.chmod(path, 0o600)
                written.append(path)
                db.execute("INSERT INTO uploads(id,hash,filename,mime,created_at,uploaded_by) VALUES(?,?,?,?,?,?)",
                           (upload_id, fingerprint, path.name, mime, now, json.dumps(actor)))
                self.domain.audit(db, upload_id, "upload", actor, "upload",
                                  after={"filename": filename, "hash": fingerprint, "mime": mime})
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,NULL,NULL,?,?,?,?,NULL)",
                       (ident, batch_id, status, filename, upload_id, now, now, json.dumps(actor), duplicate_of))
            return self.status(ident, db)
        try:
            job = self.domain.transact(actor, key, ["upload", batch_id, fingerprint, filename], create)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        self.wake.set()
        return job

    def control(self, ident, action, actor, acknowledge=False, key=None):
        def change(db):
            job = self.status(ident, db)
            if action == "cancel":
                if job["status"] not in {"queued", "failed", "interrupted"}:
                    raise HTTPException(409, "Only queued, failed or interrupted jobs can be cancelled.")
                status = "cancelled"
            else:
                if acknowledge is not True:
                    raise HTTPException(422, "Explicit acknowledge_cost:true is required; a previous attempt may have been billed.")
                if job["status"] not in {"failed", "interrupted", "cancelled"}:
                    raise HTTPException(409, "Only failed, interrupted or cancelled jobs can be retried.")
                if not self.provider.readiness()["ocr_available"]:
                    raise HTTPException(503, self.provider.readiness()["message"])
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 40:
                    raise HTTPException(429, "Queue full.")
                status = "queued"
            db.execute("UPDATE jobs SET status=?,error=NULL,updated_at=? WHERE id=?", (status, self.now(), ident))
            self.domain.audit(db, ident, "job", actor, action,
                              before={"status": job["status"]}, after={"status": status})
            return self.status(ident, db)
        result = self.domain.transact(actor, key, ["job", ident, action, acknowledge], change)
        self.wake.set()
        return result

    async def worker(self):
        while True:
            self.wake.clear()
            if await self.run_one():
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def run_one(self):
        with self.store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT j.*,b.model,b.person,b.source_type,u.filename AS stored_filename,u.mime "
                             "FROM jobs j JOIN batches b ON b.id=j.batch_id JOIN uploads u ON u.id=j.upload_id "
                             "WHERE j.status='queued' ORDER BY j.created_at LIMIT 1").fetchone()
            if not row:
                return False
            job = dict(row)
            today = datetime.now(ZoneInfo(self.store.cfg.timezone)).date()
            count = sum(datetime.fromisoformat(r["at"]).astimezone(ZoneInfo(self.store.cfg.timezone)).date() == today
                        for r in db.execute("SELECT at FROM scans"))
            running = db.execute("SELECT COUNT(*) FROM scans WHERE status='running'").fetchone()[0]
            if running >= 2:
                return False
            if count >= self.store.cfg.scan_daily_limit:
                db.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?",
                           ("Daily scan cap reached before inference. Explicitly retry on another day.", self.now(), job["id"]))
                return True
            scan_id = self.uid()
            db.execute("INSERT INTO scans VALUES(?,?,?,?,?,NULL)",
                       (scan_id, self.now(), json.loads(job["uploaded_by"])["id"], job["model"], "running"))
            db.execute("UPDATE jobs SET status='running',scan_id=?,updated_at=? WHERE id=?",
                       (scan_id, self.now(), job["id"]))
        try:
            catalog = await self.provider.catalog()
            model = next((m for m in catalog["models"] if m["id"] == job["model"]), None)
            if not model:
                raise HTTPException(422, "Selected model no longer available. Source retained; enter manually or explicitly retry if that model returns.")
            data = (self.store.files / job["stored_filename"]).read_bytes()
            raw, usage = await self.provider.extract(data, job["mime"], model, job["source_type"])
            raw = {k: v for k, v in raw.items() if k in self.editable}
            raw["person_id"] = job["person"]
            doc = self.validate_doc(raw, self.store, ocr=True)
            doc.update(model=job["model"], usage=usage, source_type=job["source_type"])
            actor = json.loads(job["uploaded_by"])
            with self.store.db() as db:
                db.execute("BEGIN IMMEDIATE")
                result = self.domain.insert(db, doc, "draft", actor, job["upload_id"],
                                            {"created_by": actor, "uploaded_by": actor,
                                             "last_edited_by": None, "confirmed_by": None})
                db.execute("UPDATE jobs SET status='needs_review',draft_id=?,error=NULL,updated_at=? WHERE id=?",
                           (result["id"], self.now(), job["id"]))
                db.execute("UPDATE scans SET status='success',usage=? WHERE id=?", (json.dumps(usage), scan_id))
        except asyncio.CancelledError:
            # Recovery requires explicit consent rather than assuming a cancelled network call was free.
            with self.store.db() as db:
                db.execute("UPDATE jobs SET status='interrupted',error=?,updated_at=? WHERE id=?",
                           ("Worker stopped; previous inference may have been billed. Explicit retry required.", self.now(), job["id"]))
                db.execute("UPDATE scans SET status='interrupted' WHERE id=?", (scan_id,))
            raise
        except Exception as exc:
            error = str(exc.detail) if isinstance(exc, HTTPException) else "Scan processing failed. Source retained; review and explicitly retry or enter manually."
            # Never persist provider objects, raw exceptions, tokens, or request payloads.
            with self.store.db() as db:
                db.execute("UPDATE jobs SET status='failed',error=?,updated_at=? WHERE id=?", (error, self.now(), job["id"]))
                db.execute("UPDATE scans SET status='failed' WHERE id=?", (scan_id,))
        return True