"""Hermetic add-on tests: dedicated temp SQLite only; all external networking denied."""
import io
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from services.household.app import (
    Config, MAX_UPLOAD, Store, create_app, digest, password_ok, validate_doc, validate_file,
)

PASSWORD = "test-only-household-password"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in list(os.environ):
        if (key.startswith(("HOUSEHOLD_", "REDIS", "VAULT_", "OPENROUTER_", "HERMES_"))
                or key.endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))):
            monkeypatch.delenv(key, raising=False)
    def denied(*args, **kwargs):
        raise AssertionError("External network forbidden in household isolated tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def env(tmp_path):
    cfg = Config(data_dir=tmp_path / "dedicated-household-test", production=False,
                 owner_username="owner", owner_password=PASSWORD)
    app = create_app(cfg)
    client = TestClient(app)
    result = client.post("/api/login", json={"username": "owner", "password": PASSWORD})
    assert result.status_code == 200, result.text
    headers = {"X-CSRF-Token": result.json()["csrf"]}
    person = client.post("/api/people", json={"name": "Purchaser one"}, headers=headers).json()
    yield SimpleNamespace(cfg=cfg, app=app, client=client, headers=headers, person=person)
    client.close()


def receipt(env, **changes):
    value = {
        "store": "Test market", "date": "2026-08-15", "currency": "USD", "person_id": env.person["id"],
        "items": [
            {"label": "Organic Granola", "normalized_label": "granola", "category": "Snacks",
             "quantity": "2", "unit_price_cents": 350, "line_total_cents": 700},
            {"label": "Shampoo bottle", "normalized_label": "shampoo", "category": "Personal care",
             "quantity": "1", "unit_price_cents": 1299, "line_total_cents": 1299},
        ],
        "subtotal_cents": 1999, "tax_cents": 101, "discount_cents": 100, "total_cents": 2000,
        "notes": "",
    }
    return {**value, **changes}


def purchase(env, **changes):
    response = env.client.post("/api/purchases", json=receipt(env, **changes), headers=env.headers)
    assert response.status_code == 201, response.text
    return response.json()


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (120, 120), "white").save(out, "PNG")
    return out.getvalue()


def model():
    return {"id": "test/live-image-model", "name": "Test metadata model", "input_modalities": ["text", "image"],
            "output_modalities": ["text"], "supports_images": True, "supports_native_pdf": False,
            "supports_documents": True, "document_mode": "cloudflare-ai parser",
            "pricing": {"prompt": "0.000001", "completion": "0.000002", "image": None, "request": None},
            "input_usd_per_million": "1", "output_usd_per_million": "2", "pricing_overrides": []}


def mock_scans(env, monkeypatch, body=None):
    env.cfg.inference_key = "test-only-not-a-real-key"
    async def catalog():
        return {"models": [model()]}
    async def extract(*args):
        return body or receipt(env), {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.0001}
    monkeypatch.setattr(env.app.state.provider, "catalog", catalog)
    monkeypatch.setattr(env.app.state.provider, "extract", extract)


def upload(env):
    return env.client.post("/api/uploads", headers=env.headers,
                           data={"model": model()["id"], "person": env.person["id"], "source_type": "price_screenshot"},
                           files={"file": ("receipt.png", image_bytes(), "image/png")})


def test_fail_closed_no_defaults(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="dedicated"):
        Config.from_env()
    monkeypatch.setenv("HOUSEHOLD_PREVIEW", "1")
    with pytest.raises(RuntimeError, match="scripts"):
        Config.from_env()
    with pytest.raises(RuntimeError, match="HTTPS"):
        create_app(Config(data_dir=tmp_path))
    with pytest.raises(RuntimeError, match="No household owner"):
        create_app(Config(data_dir=tmp_path, public_url="https://household.example", persistent_ack=True))


def test_independent_auth_csrf(env):
    outsider = TestClient(env.app)
    for endpoint in ("/api/purchases", "/api/summary", "/api/models", "/api/drafts", "/api/people", "/api/export"):
        assert outsider.get(endpoint).status_code == 401
    assert outsider.get("/api/session").json()["authenticated"] is False
    assert outsider.post("/api/signup", json={}).status_code == 404
    assert env.client.post("/api/people", json={"name": "No CSRF"}).status_code == 403
    assert env.client.post("/api/people", json={"name": "Cross origin"}, headers={**env.headers, "Origin": "https://evil.example"}).status_code == 403
    with env.app.state.store.db() as db:
        user = db.execute("SELECT * FROM users").fetchone()
        session = db.execute("SELECT * FROM sessions").fetchone()
    assert PASSWORD not in user["password"] and password_ok(PASSWORD, user["password"])
    assert env.client.cookies.get("household_session") not in session["hash"]
    assert env.client.post("/api/logout", json={}, headers=env.headers).status_code == 200
    assert env.client.get("/api/purchases").status_code == 401


def test_login_rate_limit(env):
    client = TestClient(env.app)
    for _ in range(9):
        assert client.post("/api/login", json={"username": "owner", "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"username": "owner", "password": PASSWORD}).status_code == 429


def test_partner_cannot_manage_agent_access(env):
    env.app.state.store.add_user("partner", PASSWORD, "partner")
    client = TestClient(env.app)
    session = client.post("/api/login", json={"username": "partner", "password": PASSWORD}).json()
    assert client.get("/api/purchases").status_code == 200
    assert client.get("/api/agent-tokens").status_code == 403
    assert client.post("/api/agent-tokens", json={"name": "No", "scopes": ["summary:read"]},
                       headers={"X-CSRF-Token": session["csrf"]}).status_code == 403
    with pytest.raises(ValueError, match="at most two"):
        env.app.state.store.add_user("third", PASSWORD)


def test_exact_arithmetic_and_currency_separation(env):
    purchase(env)
    purchase(env, currency="EUR")
    report = env.client.get("/api/summary").json()
    assert len(report["currencies"]) == 2
    for c in report["currencies"]:
        assert c["total_cents"] == 2000
        assert c["item_total_cents"] == 1999
        assert c["tax_cents"] == 101 and c["discount_cents"] == 100
        assert sum(x["total_cents"] for x in c["by_category"]) == 1999
        assert sum(x["total_cents"] for x in c["by_person"]) == 2000
    assert "never combined" in report["basis"]


def test_item_filters_no_receipt_overcount(env):
    purchase(env)
    for params, amount in (({"q": "shampoo"}, 1299), ({"category": "snacks"}, 700),
                           ({"q": "shampoo", "category": "Snacks"}, None)):
        report = env.client.get("/api/summary", params=params).json()
        if amount is None:
            assert report["currencies"] == []
        else:
            c = report["currencies"][0]
            assert c["total_cents"] == amount
            assert c["tax_cents"] == c["discount_cents"] == 0
            assert sum(i["total_cents"] for i in c["by_item"]) == amount
    assert env.client.get("/api/summary", params={"q": "Test market"}).json()["currencies"][0]["total_cents"] == 1999


def test_dates_person_and_pagination(env):
    p1 = purchase(env, date="2026-08-01")
    p2 = purchase(env, date="2026-08-31")
    purchase(env, date="2026-09-01")
    other = env.client.post("/api/people", json={"name": "Second person"}, headers=env.headers).json()
    purchase(env, person_id=other["id"])
    result = env.client.get("/api/purchases", params={"start": "2026-08-01", "end": "2026-08-31",
                                                   "person": env.person["id"], "limit": 1}).json()
    assert result["total"] == 2 and len(result["purchases"]) == 1
    result = env.client.get("/api/purchases", params={"start": "2026-08-01", "end": "2026-08-31",
                                                   "person": env.person["id"], "limit": 1, "offset": 1}).json()
    assert result["purchases"][0]["id"] == p1["id"]
    assert env.client.get("/api/purchases", params={"start": "2026-02-30"}).status_code == 422
    assert env.client.get("/api/summary", params={"start": "2026-09-01", "end": "2026-08-01"}).status_code == 422
    assert env.client.get("/api/purchases", params={"limit": 101}).status_code == 422


@pytest.mark.parametrize("changes", [
    {"total_cents": 20.00}, {"total_cents": True}, {"tax_cents": -1}, {"currency": "JPY"},
    {"total_cents": 2001}, {"person_id": "foreign-household"}, {"date": None},
    {"items": []}, {"items": [{"label": "bad", "quantity": "0", "line_total_cents": 10}]},
])
def test_invalid_financial_records_rejected(env, changes):
    assert env.client.post("/api/purchases", json=receipt(env, **changes), headers=env.headers).status_code == 422
    assert env.client.get("/api/summary").json()["currencies"] == []


def test_edit_delete_persistence_and_normalization(env):
    data = receipt(env)
    data["items"][0]["normalized_label"] = ""
    p = purchase(env, **data)
    assert p["items"][0]["normalized_label"] == "organic granola"
    response = env.client.patch("/api/purchases/" + p["id"], json={"store": "Updated"}, headers=env.headers)
    assert response.status_code == 200
    assert env.client.patch("/api/purchases/" + p["id"], json={"total_cents": 1}, headers=env.headers).status_code == 422
    app2 = create_app(Config(data_dir=env.cfg.data_dir, production=False))
    assert app2.state.store.read(p["id"], "purchase")["store"] == "Updated"
    assert env.client.delete("/api/purchases/" + p["id"], headers=env.headers).status_code == 200
    assert env.client.get("/api/purchases/" + p["id"]).status_code == 404


@pytest.mark.parametrize("source_type", ["receipt", "price_screenshot"])
def test_real_manual_editor_payload_and_purchase_edit(env, source_type):
    # Exact readEditor form shape: only manual create includes source_type.
    payload = {**receipt(env), "source_type": source_type}
    assert env.client.get("/api/purchases").json()["total"] == 0
    response = env.client.post("/api/purchases", json=payload, headers=env.headers)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["status"] == "confirmed" and record["source_type"] == source_type
    edit_payload = {k: v for k, v in payload.items() if k != "source_type"}
    edit_payload["store"] = "UI corrected merchant"
    edited = env.client.patch("/api/purchases/" + record["id"], json=edit_payload, headers=env.headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()["source_type"] == source_type
    assert edited.json()["store"] == "UI corrected merchant"
    assert env.client.get("/api/summary").json()["currencies"][0]["total_cents"] == 2000


@pytest.mark.parametrize("source_type", ["unknown", "", None, {}, []])
def test_manual_source_type_is_validated_without_creating_spending(env, source_type):
    result = env.client.post("/api/purchases", json={**receipt(env), "source_type": source_type}, headers=env.headers)
    assert result.status_code == 422
    assert env.client.get("/api/purchases").json()["total"] == 0


def test_manual_screenshot_requires_complete_explicit_confirmation(env):
    result = env.client.post("/api/purchases", json={**receipt(env, date=None), "source_type": "price_screenshot"},
                             headers=env.headers)
    assert result.status_code == 422
    assert env.client.get("/api/summary").json()["currencies"] == []


def test_ui_maximum_person_and_token_name_lengths(env):
    name = "n" * 100
    created = env.client.post("/api/people", json={"name": name}, headers=env.headers)
    assert created.status_code == 201
    edited = env.client.patch("/api/people/" + created.json()["id"], json={"name": name, "active": False}, headers=env.headers)
    assert edited.status_code == 200 and edited.json()["active"] is False
    token = env.client.post("/api/agent-tokens", json={"name": name, "scopes": ["purchases:read", "summary:read"], "expires_days": 90},
                            headers=env.headers)
    assert token.status_code == 201


def test_draft_crud_confirm_duplicate_private_source(env, monkeypatch):
    mock_scans(env, monkeypatch)
    result = upload(env)
    assert result.status_code == 201, result.text
    draft = result.json()
    assert draft["source_type"] == "price_screenshot"
    assert env.client.get("/api/summary").json()["currencies"] == []
    dup = upload(env)
    assert dup.status_code == 409 and dup.json()["duplicate"]["id"] == draft["id"]
    assert TestClient(env.app).get(f'/api/drafts/{draft["id"]}/source').status_code == 401
    assert env.client.get(f'/api/drafts/{draft["id"]}/source').status_code == 200
    assert env.client.get(f'/static/{draft["id"]}.jpg').status_code == 404
    # Actual UI saves a full readEditor payload without source_type, then confirms with {}.
    draft_edit = env.client.patch(f'/api/drafts/{draft["id"]}', json=receipt(env, store="Corrected"), headers=env.headers)
    assert draft_edit.status_code == 200
    assert draft_edit.json()["source_type"] == "price_screenshot"
    assert env.client.get("/api/summary").json()["currencies"] == []
    confirm = env.client.post(f'/api/drafts/{draft["id"]}/confirm', json={}, headers=env.headers)
    assert confirm.status_code == 200, confirm.text
    p = confirm.json()["purchase"]
    assert confirm.json()["already_confirmed"] is False and p["total_cents"] == 2000
    again = env.client.post(f'/api/drafts/{draft["id"]}/confirm', json={}, headers=env.headers).json()
    assert again["already_confirmed"] is True and again["purchase"]["id"] == p["id"]
    assert env.client.get("/api/purchases").json()["total"] == 1
    assert env.client.patch(f'/api/drafts/{draft["id"]}', json={}, headers=env.headers).status_code == 409
    assert env.client.get("/api/drafts").json()["total"] == 0
    env.client.delete("/api/purchases/" + p["id"], headers=env.headers)
    assert not list(env.app.state.store.files.glob("*.jpg"))
    assert env.client.get(f'/api/drafts/{draft["id"]}').status_code == 404


def test_missing_ocr_fields_require_review_and_delete(env, monkeypatch):
    mock_scans(env, monkeypatch, {"store": "", "date": None, "items": [], "total_cents": None})
    d = upload(env).json()
    assert d["warnings"] and d["total_cents"] is None
    assert env.client.post(f'/api/drafts/{d["id"]}/confirm', json={}, headers=env.headers).status_code == 422
    assert env.client.patch(f'/api/drafts/{d["id"]}', json=receipt(env), headers=env.headers).status_code == 200
    assert env.client.delete(f'/api/drafts/{d["id"]}', headers=env.headers).status_code == 200
    assert not list(env.app.state.store.files.glob("*.jpg"))


def test_missing_ocr_currency_quantity_not_invented(env, monkeypatch):
    mock_scans(env, monkeypatch, {"store": None, "date": None, "currency": None,
                                 "items": [{"label": "Unknown price", "quantity": None,
                                            "category": None, "normalized_label": None,
                                            "unit_price_cents": None, "line_total_cents": None}],
                                 "notes": None})
    d = upload(env).json()
    assert d["currency"] == "" and d["items"][0]["quantity"] == ""
    assert any("currency" in w for w in d["warnings"]) and any("quantity" in w for w in d["warnings"])
    assert env.client.post(f'/api/drafts/{d["id"]}/confirm', json={}, headers=env.headers).status_code == 422


def test_chunked_body_and_json_bounds(env):
    def chunks():
        yield b'--bounded\r\nContent-Disposition: form-data; name="file"; filename="big.png"\r\nContent-Type: image/png\r\n\r\n'
        for _ in range(12):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--bounded--\r\n"
    response = env.client.post("/api/uploads", content=chunks(),
                               headers={**env.headers, "Content-Type": "multipart/form-data; boundary=bounded"})
    assert response.status_code in {400, 413}  # parser aborts before unbounded disk/memory use
    response = env.client.post("/api/purchases", content=b" " * (256 * 1024 + 1),
                               headers={**env.headers, "Content-Type": "application/json"})
    assert response.status_code == 413


def test_no_inference_key_explicit_unavailable_and_no_provisioning_fallback(env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVISIONING_KEY", "test-provisioning-NEVER-inference")
    assert env.client.get("/api/session").json()["readiness"]["ocr_available"] is False
    result = upload(env)
    assert result.status_code == 503 and "OCR unavailable" in result.text
    assert env.client.get("/api/drafts").json()["total"] == 0


def test_upload_type_size_signature_bounds(env):
    for name, mime, data, expected in (
        ("bad.svg", "image/svg+xml", b"<svg></svg>", 415),
        ("bad.jpg", "image/jpeg", image_bytes(), 415),
        ("bad.pdf", "application/pdf", b"not a pdf", 415),
        ("too-big.png", "image/png", b"x" * (MAX_UPLOAD + 1), 413),
    ):
        response = env.client.post("/api/uploads", headers=env.headers,
                                   data={"model": model()["id"], "person": env.person["id"]},
                                   files={"file": (name, data, mime)})
        assert response.status_code == expected, response.text


def pdf_bytes(pages=1):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{i + 3} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"] * pages
    raw = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(raw)
    raw.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        raw.extend(f"{offset:010} 00000 n \n".encode())
    raw.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(raw)


def test_real_pdf_validation_bounds(env):
    data, mime, _ = validate_file(pdf_bytes(1), "application/pdf", "receipt.pdf", env.app.state.store.files)
    assert mime == "application/pdf" and data.startswith(b"%PDF")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        validate_file(pdf_bytes(21), "application/pdf", "receipt.pdf", env.app.state.store.files)
    assert exc.value.status_code == 413
    assert not list(env.app.state.store.files.glob("tmp*.pdf"))


def test_export_all_records_no_silent_pagination_and_csv_injection(env):
    for _ in range(32):
        purchase(env, store="=HYPERLINK(\"evil\")")
    assert env.client.get("/api/purchases").json()["total"] == 32
    assert len(env.client.get("/api/purchases").json()["purchases"]) == 30
    out = env.client.get("/api/export", params={"format": "json", "limit": 1}).json()
    assert len(out["purchases"]) == 32
    csv = env.client.get("/api/export", params={"format": "csv", "category": "Snacks"})
    assert csv.status_code == 200 and len(csv.text.splitlines()) == 33
    assert "'=HYPERLINK" in csv.text
    assert "do not sum receipt columns" in csv.text


def rpc(env, token, method, params=None):
    return env.client.post("/mcp", headers={"Authorization": "Bearer " + token},
                           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def test_mcp_initialize_scoped_read_only_snapshot_and_revocation(env):
    purchase(env, date="2026-08-31")
    purchase(env, date="2026-09-01")
    result = env.client.post("/api/agent-tokens", headers=env.headers,
                             json={"name": "Agent only summary", "scopes": ["summary:read"], "expires_days": 7})
    assert result.status_code == 201
    t = result.json()
    token = t["token"]
    with env.app.state.store.db() as db:
        row = db.execute("SELECT * FROM agent_tokens WHERE id=?", (t["id"],)).fetchone()
    assert row["hash"] == digest(token) and token not in str(dict(row))
    assert token not in env.client.get("/api/agent-tokens").text
    assert env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
    init = rpc(env, token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).json()
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    tools = rpc(env, token, "tools/list").json()["result"]["tools"]
    assert len(tools) == 3 and all(t["annotations"]["readOnlyHint"] for t in tools)
    denied = rpc(env, token, "tools/call", {"name": "household_purchases", "arguments": {}}).json()
    assert "error" in denied
    malformed = env.client.post("/mcp", content=b"not-json",
                                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    assert malformed.status_code == 400 and malformed.json()["error"]["code"] == -32700
    snap = rpc(env, token, "tools/call", {"name": "household_snapshot", "arguments": {"month": "2026-08", "q": "shampoo"}}).json()
    c = snap["result"]["structuredContent"]["currencies"][0]
    assert c["total_cents"] == 1299 and c["purchase_count"] == 1
    bad = rpc(env, token, "tools/call", {"name": "household_items", "arguments": {"month": "2026-08"}}).json()
    assert bad["result"]["isError"] is True
    assert env.client.delete("/api/agent-tokens/" + t["id"], headers=env.headers).status_code == 200
    assert rpc(env, token, "tools/list").status_code == 401


def test_live_catalog_metadata_with_explicit_mock_http_only(env, monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def upstream(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [
            {"id": "live/vision", "name": "Vision", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
             "pricing": {"prompt": "0.0000005", "completion": "0.000001", "image": "0.001"}},
            {"id": "live/native", "architecture": {"input_modalities": ["text", "file"], "output_modalities": ["text"]},
             "pricing": {"prompt": "0.000002"}},
        ]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    catalog = env.client.get("/api/models").json()
    assert catalog["models"][0]["input_usd_per_million"] == "0.5000000"
    assert catalog["models"][0]["supports_images"] and not catalog["models"][0]["supports_native_pdf"]
    assert catalog["models"][1]["supports_native_pdf"] and catalog["models"][1]["output_usd_per_million"] is None
    assert catalog["parser"]["engine"] == "cloudflare-ai"
    assert env.client.get("/api/models", params={"q": "native"}).json()["models"][0]["id"] == "live/native"
    assert len(seen) == 1


def test_catalog_failure_not_fake(env, monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda req: httpx.Response(503)), **kwargs))
    response = env.client.get("/api/models")
    assert response.status_code == 503 and "no stale/fabricated" in response.text


def test_vault_proxy_http_contract_and_pdf_parser_explicit(env, monkeypatch):
    cfg = env.cfg
    cfg.vault_url, cfg.vault_connection, cfg.vault_token = "https://vault.example", "household-openrouter", "test-only-vault-token"
    original = httpx.AsyncClient
    seen = []
    def upstream(request):
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"status": 200, "truncated": False,
            "json": {"choices": [{"message": {"content": json.dumps(receipt(env))}}], "usage": {"cost": 0.0002}}})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    import asyncio
    obj, usage = asyncio.run(env.app.state.provider.extract(pdf_bytes(), "application/pdf", model(), "receipt"))
    assert obj["total_cents"] == 2000 and usage["cost"] == 0.0002
    url, auth, body = seen[0]
    assert url == "https://vault.example/api/vault/proxy/household-openrouter"
    assert auth == "Bearer test-only-vault-token"
    assert body["json"]["plugins"] == [{"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}]
    assert body["json"]["messages"][0]["content"][1]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert "test-only-vault-token" not in json.dumps(body)


def test_inference_upstream_failure_sanitized(env, monkeypatch):
    env.cfg.inference_key = "test-only-sensitive-key"
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda req: httpx.Response(401, text="test-only-sensitive-key raw debug")), **kwargs))
    import asyncio
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(env.app.state.provider.extract(image_bytes(), "image/png", model(), "receipt"))
    assert "sensitive" not in exc.value.detail and "401" in exc.value.detail


def test_preview_isolation_no_secrets_and_unmistakable_demo(tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setenv("HOUSEHOLD_DATA_DIR", str(tmp_path / "production-should-never-exist"))
    monkeypatch.setenv("HOUSEHOLD_OPENROUTER_API_KEY", "test-secret-not-loaded")
    with tempfile.TemporaryDirectory(prefix="household-demo-") as directory:
        path = Path(directory)
        (path / ".household-demo").touch()
        app = create_app(Config(data_dir=path, production=False, preview=True))
        client = TestClient(app, client=("127.0.0.1", 10000))
        s = client.get("/api/session").json()
        assert s["authenticated"] and s["demo"] and not s["readiness"]["ocr_available"]
        data = client.get("/api/purchases").json()
        assert data["demo"] and data["total"] > 0
        assert all(p["demo"] and p["store"].startswith("DEMO") and "DEMO" in p["notes"] for p in data["purchases"])
        assert client.post("/api/agent-tokens", json={"name": "No", "scopes": ["summary:read"]},
                           headers={"X-CSRF-Token": s["csrf"]}).status_code == 403
        remote = TestClient(app, client=("203.0.113.1", 10000))
        remote_session = remote.get("/api/session").json()
        assert remote_session["authenticated"] and remote_session["demo"]
        proxy_headers = {"X-CSRF-Token": remote_session["csrf"], "Host": "internal-preview",
                         "X-Forwarded-Host": "household.preview.replit.dev",
                         "Origin": "https://household.preview.replit.dev"}
        assert remote.post("/api/people", json={"name": "DEMO proxy visitor"}, headers=proxy_headers).status_code == 201
        assert remote.post("/api/people", json={"name": "No CSRF"},
                           headers={k: v for k, v in proxy_headers.items() if k != "X-CSRF-Token"}).status_code == 403
        assert remote.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
        assert app.state.config.inference_key == ""
    assert not (tmp_path / "production-should-never-exist").exists()


def test_production_session_cookie_secure(tmp_path):
    cfg = Config(data_dir=tmp_path, public_url="https://household.example", persistent_ack=True,
                 owner_username="owner", owner_password=PASSWORD)
    client = TestClient(create_app(cfg), base_url="https://household.example")
    r = client.post("/api/login", json={"username": "owner", "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/session").json()["authenticated"]
    assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 400