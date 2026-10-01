"""Hermetic write/audit/queue regression tests. No real APIs, Redis, credentials or data."""
import asyncio
import base64
import io
import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from services.household.app import Store, validate_doc
from services.household.domain import migrate
from services.household.queue import MAX_FILE
from services.household.tests.test_household import (
    env, isolated_environment, image_bytes, mock_scans, model, purchase, receipt, upload,
)


def issue(env, scopes=None, agent_id="lexi"):
    body = {"name": "Lexi connection"}
    if scopes is not None:
        body["scopes"] = scopes
    if agent_id is not None:
        body["agent_id"] = agent_id
    result = env.client.post("/api/agent-tokens", json=body, headers=env.headers)
    assert result.status_code == 201, result.text
    return {"Authorization": "Bearer " + result.json()["token"]}


def rpc(env, headers, name, arguments=None):
    return env.client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}}}).json()


def tool_list(env, headers):
    return env.client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]


def batch(env):
    result = env.client.post("/api/batches", json={"model": model()["id"], "person": env.person["id"],
                                                 "source_type": "price_screenshot"}, headers=env.headers)
    assert result.status_code == 201, result.text
    return result.json()


def file_job(env, batch_id, data=None, key=None):
    return env.client.post(f"/api/batches/{batch_id}/files",
                           headers={**env.headers, **({"Idempotency-Key": key} if key else {})},
                           files={"file": ("photo.png", data or image_bytes(), "image/png")})


def test_token_defaults_identity_and_metadata(env):
    read = issue(env, agent_id=None)
    assert all("_meta" not in t for t in tool_list(env, read))
    assert "error" in rpc(env, read, "household_purchase_create", {"purchase": receipt(env), "idempotency_key": "new"})
    for agent_id in (None, "", "Agent Lexi", "../lexi", 123):
        response = env.client.post("/api/agent-tokens", headers=env.headers,
                                   json={"name": "write", "scopes": ["purchases:write"], "agent_id": agent_id})
        assert response.status_code == 422
    writer = issue(env, ["purchases:read", "purchases:write"])
    assert all(t["_meta"] == {"household_agent_id": "lexi"} for t in tool_list(env, writer))
    listed = env.client.get("/api/agent-tokens").json()["tokens"]
    assert all("hash" not in t and "token" not in t for t in listed)


def test_agent_create_edit_delete_atomic_audit_and_retry(env):
    headers = issue(env, ["purchases:read", "purchases:write", "purchases:delete"])
    args = {"purchase": receipt(env), "idempotency_key": "create-one"}
    first = rpc(env, headers, "household_purchase_create", args)["result"]["structuredContent"]
    again = rpc(env, headers, "household_purchase_create", args)["result"]["structuredContent"]
    assert first == again
    assert first["created_by"] == {"kind": "agent", "id": "lexi", "display": "lexi"}
    assert first["person_id"] == env.person["id"]
    assert first["confirmed_by"] == first["created_by"]
    conflict = rpc(env, headers, "household_purchase_create", {**args, "purchase": receipt(env, notes="different")})
    assert conflict["result"]["structuredContent"]["status"] == 409
    edit_args = {"id": first["id"], "changes": {"notes": "Agent corrected"}, "version": 1, "idempotency_key": "edit-one"}
    edited = rpc(env, headers, "household_purchase_edit", edit_args)["result"]["structuredContent"]
    assert edited["version"] == 2 and edited["last_edited_by"]["id"] == "lexi"
    stale = rpc(env, headers, "household_purchase_edit", {**edit_args, "idempotency_key": "stale"})
    assert stale["result"]["structuredContent"]["status"] == 409
    assert rpc(env, headers, "household_purchase_edit", edit_args)["result"]["structuredContent"] == edited
    delete_args = {"id": first["id"], "version": 2, "idempotency_key": "delete"}
    assert rpc(env, headers, "household_purchase_delete", delete_args)["result"]["structuredContent"] == {"ok": True}
    assert rpc(env, headers, "household_purchase_delete", delete_args)["result"]["structuredContent"] == {"ok": True}
    assert env.client.get("/api/purchases").json()["total"] == 0
    history = env.client.get(f'/api/purchases/{first["id"]}/audit').json()
    assert [r["action"] for r in history["events"]] == ["delete", "edit", "create"]
    assert history["events"][0]["tombstone"]["notes"] == "Agent corrected"
    assert history["events"][1]["diff"]["notes"]["before"] == ""
    with env.app.state.store.db() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM audit")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE audit SET action='tamper'")


@pytest.mark.parametrize("field", ["created_by", "uploaded_by", "last_edited_by", "confirmed_by", "actor", "agent_id", "version"])
def test_spoofed_actor_fields_rejected(env, field):
    headers = issue(env, ["purchases:write"])
    result = rpc(env, headers, "household_purchase_create",
                 {"purchase": receipt(env, **{field: {"id": "owner"}}), "idempotency_key": "spoof"})
    assert result["result"]["isError"]
    assert env.client.post("/api/purchases", json=receipt(env, **{field: "owner"}), headers=env.headers).status_code == 422
    assert env.client.get("/api/audit").json()["total"] == 0


def test_browser_headers_version_and_human_attribution(env):
    first = env.client.post("/api/purchases", json=receipt(env),
                            headers={**env.headers, "Idempotency-Key": "human-create"}).json()
    assert first["created_by"]["kind"] == "human"
    assert first["created_by"]["display"] == "owner"
    path = f'/api/purchases/{first["id"]}'
    response = env.client.patch(path, json={"notes": "new"},
                                headers={**env.headers, "If-Match": "1", "Idempotency-Key": "human-edit"})
    assert response.json()["version"] == 2
    assert env.client.delete(path, headers={**env.headers, "If-Match": "1"}).status_code == 409
    assert env.client.get(path).json()["notes"] == "new"


def test_queue_draft_confirm_ignores_ocr_identity(env, monkeypatch):
    malicious = receipt(env, created_by={"kind": "human", "id": "spoof"}, uploaded_by="spoof", person_id="spoof")
    mock_scans(env, monkeypatch, malicious)
    headers = issue(env, ["uploads:create", "drafts:read", "drafts:write", "drafts:confirm"])
    args = {"filename": "photo.png", "mime": "image/png", "data_base64": base64.b64encode(image_bytes()).decode(),
            "model": model()["id"], "person": env.person["id"], "source_type": "price_screenshot", "idempotency_key": "photo"}
    job = rpc(env, headers, "household_upload", args)["result"]["structuredContent"]
    assert job["status"] == "queued"
    assert rpc(env, headers, "household_upload", args)["result"]["structuredContent"] == job
    asyncio.run(env.app.state.queue.run_one())
    status = rpc(env, headers, "household_job", {"id": job["id"]})["result"]["structuredContent"]
    assert status["status"] == "needs_review"
    draft = rpc(env, headers, "household_draft", {"id": status["draft_id"]})["result"]["structuredContent"]
    assert draft["created_by"]["id"] == draft["uploaded_by"]["id"] == "lexi"
    assert draft["person_id"] == env.person["id"]
    assert env.client.get("/api/purchases").json()["total"] == 0
    confirmation = {"id": draft["id"], "version": 1, "idempotency_key": "confirm"}
    confirmed = rpc(env, headers, "household_draft_confirm", confirmation)["result"]["structuredContent"]
    assert confirmed["purchase"]["confirmed_by"]["id"] == "lexi"
    assert rpc(env, headers, "household_draft_confirm", confirmation)["result"]["structuredContent"] == confirmed
    events = env.client.get(f'/api/drafts/{draft["id"]}/audit').json()["events"]
    assert [e["action"] for e in events] == ["confirm", "create"]


def test_bulk_mixed_failure_duplicates_sources_retry_cancel(env, monkeypatch):
    mock_scans(env, monkeypatch)
    b = batch(env)
    job = file_job(env, b["id"], key="one").json()
    assert file_job(env, b["id"], key="one").json() == job
    duplicate = file_job(env, b["id"]).json()
    assert duplicate["status"] == "duplicate" and duplicate["duplicate_of"] == job["id"]
    async def fail(*args):
        raise HTTPException(502, "Provider timed out. Prior attempt may have been billed.")
    monkeypatch.setattr(env.app.state.provider, "extract", fail)
    asyncio.run(env.app.state.queue.run_one())
    failed = env.client.get(f'/api/jobs/{job["id"]}').json()
    assert failed["status"] == "failed" and "billed" in failed["error"]
    assert env.client.get(f'/api/jobs/{job["id"]}/source').status_code == 200
    assert TestClient(env.app).get(f'/api/jobs/{job["id"]}/source').status_code == 401
    assert asyncio.run(env.app.state.queue.run_one()) is False  # no auto retry
    retry_path = f'/api/jobs/{job["id"]}/retry'
    assert env.client.post(retry_path, json={}, headers=env.headers).status_code == 422
    assert env.client.post(retry_path, json={"acknowledge_cost": True}, headers=env.headers).status_code == 202
    assert env.client.post(f'/api/jobs/{job["id"]}/cancel', json={}, headers=env.headers).json()["status"] == "cancelled"
    assert env.client.post(retry_path, json={"acknowledge_cost": True}, headers=env.headers).status_code == 202
    mock_scans(env, monkeypatch)
    asyncio.run(env.app.state.queue.run_one())
    assert env.client.get(f'/api/jobs/{job["id"]}').json()["status"] == "needs_review"
    statuses = [j["status"] for j in env.client.get(f'/api/batches/{b["id"]}').json()["jobs"]]
    assert sorted(statuses) == ["duplicate", "needs_review"]


def test_batch_and_daily_caps_and_invalid_file(env, monkeypatch):
    mock_scans(env, monkeypatch)
    b = batch(env)
    oversized = env.client.post(f'/api/batches/{b["id"]}/files', headers=env.headers,
                                files={"file": ("x.png", b"x" * (MAX_FILE + 1), "image/png")})
    assert oversized.status_code == 413
    assert file_job(env, b["id"], b"not-image").status_code == 415
    for _ in range(20):
        assert file_job(env, b["id"]).status_code == 202
    assert file_job(env, b["id"]).status_code == 422
    env.cfg.scan_daily_limit = 1
    asyncio.run(env.app.state.queue.run_one())
    out = io.BytesIO()
    Image.new("RGB", (100, 100), "red").save(out, "PNG")
    b2 = batch(env)
    other = file_job(env, b2["id"], out.getvalue()).json()
    asyncio.run(env.app.state.queue.run_one())
    status = env.client.get(f'/api/jobs/{other["id"]}').json()
    assert status["status"] == "failed" and "cap" in status["error"]
    with env.app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1


def test_restart_recovery_never_auto_spends(env, monkeypatch):
    mock_scans(env, monkeypatch)
    job = file_job(env, batch(env)["id"]).json()
    async def restart():
        await env.app.state.queue.start()
        await asyncio.sleep(.02)
        await env.app.state.queue.stop()
    asyncio.run(restart())
    assert env.client.get(f'/api/jobs/{job["id"]}').json()["status"] == "interrupted"
    with env.app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0


def test_migration_preserves_legacy_and_repeated_initialization(env):
    p = purchase(env)
    store = Store(env.cfg)
    assert store.read(p["id"], "purchase")["created_by"]["display"] == "owner"
    legacy = store.insert(validate_doc(receipt(env), store, strict=True), "purchase")
    assert store.read(legacy, "purchase")["created_by"] is None
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE records(id TEXT,doc TEXT)")
        db.execute("CREATE TABLE uploads(id TEXT)")
        db.execute("CREATE TABLE agent_tokens(id TEXT)")
        db.execute("INSERT INTO records VALUES('old','{\"notes\":\"preserved\"}')")
        migrate(db)
        migrate(db)
        row = db.execute("SELECT doc,version,attribution,deleted_at FROM records").fetchone()
        assert row == ('{"notes":"preserved"}', 1, "{}", None)


def test_mcp_large_json_only_upload_and_invalid_base64(env, monkeypatch):
    mock_scans(env, monkeypatch)
    headers = issue(env, ["uploads:create", "purchases:read"])
    result = env.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
        "method": "tools/call", "params": {"name": "household_purchases", "arguments": {"q": "x" * 270000}}})
    assert result.status_code == 413
    args = {"filename": "x.png", "mime": "image/png", "data_base64": "https://evil.example/image",
            "model": model()["id"], "person": env.person["id"], "idempotency_key": "bad"}
    assert rpc(env, headers, "household_upload", args)["result"]["isError"]
    with env.app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_unconfigured_batch_fails_without_fake_draft(env):
    response = env.client.post("/api/batches", headers=env.headers,
                               json={"model": "unknown", "person": env.person["id"]})
    assert response.status_code == 503
    assert env.client.get("/api/drafts").json()["total"] == 0


def test_audit_failure_rolls_back_mutation(env, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("simulated audit failure")
    monkeypatch.setattr(env.app.state.domain, "audit", fail)
    with pytest.raises(sqlite3.IntegrityError, match="simulated"):
        env.app.state.domain.mutate("create", "purchase", {"kind": "agent", "id": "lexi", "display": "lexi"},
                                    receipt(env), key="atomic")
    with env.app.state.store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0


def test_queue_two_worker_concurrency_bound(env, monkeypatch):
    mock_scans(env, monkeypatch)
    b = batch(env)
    for color in ("red", "green", "blue"):
        out = io.BytesIO()
        Image.new("RGB", (90, 90), color).save(out, "PNG")
        assert file_job(env, b["id"], out.getvalue()).status_code == 202

    async def scenario():
        release = asyncio.Event()
        entered = 0
        async def extract(*args):
            nonlocal entered
            entered += 1
            await release.wait()
            return receipt(env), {}
        monkeypatch.setattr(env.app.state.provider, "extract", extract)
        workers = [asyncio.create_task(env.app.state.queue.run_one()) for _ in range(3)]
        for _ in range(100):
            if entered == 2:
                break
            await asyncio.sleep(.005)
        assert entered == 2
        with env.app.state.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM scans WHERE status='running'").fetchone()[0] == 2
            assert db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 1
        release.set()
        await asyncio.gather(*workers)
    asyncio.run(scenario())


def test_human_edits_agent_draft_then_confirms_keep_distinct_attribution(env, monkeypatch):
    mock_scans(env, monkeypatch)
    actor = {"kind": "agent", "id": "vivian", "display": "vivian"}
    b = asyncio.run(env.app.state.queue.batch(actor, {"model": model()["id"], "person": env.person["id"]}))
    j = env.app.state.queue.reserve(actor, b["id"], image_bytes(), "image/png", "photo.png")
    asyncio.run(env.app.state.queue.run_one())
    draft_id = env.app.state.queue.status(j["id"])["draft_id"]
    edited = env.client.patch(f"/api/drafts/{draft_id}", json={"notes": "Reviewed by owner"},
                              headers={**env.headers, "If-Match": "1"}).json()
    assert edited["uploaded_by"]["id"] == edited["created_by"]["id"] == "vivian"
    assert edited["last_edited_by"]["kind"] == "human"
    confirmed = env.client.post(f"/api/drafts/{draft_id}/confirm", json={}, headers=env.headers).json()["purchase"]
    assert confirmed["confirmed_by"]["kind"] == "human"
    assert confirmed["created_by"]["id"] == "vivian"
    assert confirmed["uploaded_by"]["id"] == "vivian"
    assert confirmed["last_edited_by"]["kind"] == "human"
    assert env.client.post(f"/api/drafts/{draft_id}/confirm", json={"created_by": "spoof"}, headers=env.headers).status_code == 422