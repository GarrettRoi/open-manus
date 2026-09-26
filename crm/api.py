"""Authenticated dashboard API and narrowly authenticated external ingestion."""
import hashlib
import hmac
import json
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crm.service import CRMService
from crm.store import PREFIX

router = APIRouter(prefix="/api/crm")
MAX_BODY = 65536


def is_webhook_request(path, method):
    return method == "POST" and re.fullmatch(r"/api/crm/webhook/[a-z0-9][a-z0-9_-]{0,63}", path) is not None


def service():
    return CRMService()


def failure(code, message, status):
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


async def body(request):
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY:
            raise ValueError("payload_too_large")
    try:
        result = json.loads(chunks)
    except (ValueError, UnicodeError):
        raise ValueError("invalid_json")
    if not isinstance(result, dict):
        raise ValueError("invalid_json")
    return result


def owner(request):
    # Authentication is performed by existing dashboard middleware.
    # Interactive dashboard access is the trusted operator surface; token-only
    # service principals are deliberately not promoted to owner.
    if getattr(request.state, "token_authenticated", False):
        return None
    session = getattr(request.state, "session", None)
    if session:
        return "dashboard:" + session.provider + ":" + session.user_id
    if not getattr(request.app.state, "auth_required", False):
        from hermes_cli.web_server import _has_valid_session_token
        if _has_valid_session_token(request):
            return "dashboard:local-owner"
    return None


def envelope(result):
    codes = {"validation": 422, "forbidden": 403, "conflict": 409, "not_found": 404, "unavailable": 503}
    return JSONResponse(result, status_code=200 if result["ok"] else codes.get(result["error"]["code"], 400))


@router.post("/action")
async def action(request: Request):
    actor = owner(request)
    if not actor:
        return failure("forbidden", "Interactive owner session required", 403)
    try:
        data = await body(request)
        if set(data) - {"action", "args"} or not isinstance(data.get("action"), str):
            return failure("validation", "Expected action and args", 422)
        return envelope(service().execute(data["action"], data.get("args", {}), actor, "owner"))
    except ValueError as exc:
        return failure("validation", str(exc), 413 if str(exc) == "payload_too_large" else 422)
    except Exception:
        return failure("unavailable", "CRM storage unavailable", 503)


@router.get("/discover")
def discover(request: Request):
    actor = owner(request)
    if not actor:
        return failure("forbidden", "Interactive owner session required", 403)
    try:
        return envelope(service().execute("discover", {}, actor, "owner"))
    except Exception:
        return failure("unavailable", "CRM storage unavailable", 503)


@router.post("/webhook/{source_id}")
async def webhook(source_id: str, request: Request):
    if not is_webhook_request(request.url.path, request.method):
        return failure("not_found", "Unknown integration", 404)
    try:
        svc = service()
        raw = svc.redis.hget(PREFIX + "sources", source_id)
        source = json.loads(raw) if raw else {}
        supplied = request.headers.get("authorization", "")
        digest = hashlib.sha256(supplied[7:].encode()).hexdigest() if supplied.startswith("Bearer ") and len(supplied) < 1000 else ""
        if not source.get("enabled") or not digest or not hmac.compare_digest(digest, source.get("secret_hash", "")):
            return failure("unauthorized", "Invalid integration credentials", 401)
        rate_key = PREFIX + f"rate:{source_id}:{int(time.time()) // 60}"
        count = svc.redis.incr(rate_key)
        svc.redis.expire(rate_key, 120)
        if count > 60:
            return failure("rate_limited", "Source rate limit exceeded", 429)
        data = await body(request)
        event_id = data.get("event_id")
        if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
            return failure("validation", "Stable event_id is required", 422)
        mapping = source.get("mapping", {})
        lead = {k: data[v] for k, v in mapping.items() if v in data} if mapping else data.get("lead")
        if not isinstance(lead, dict):
            return failure("validation", "Expected lead object", 422)
        lead = dict(lead)
        # External data cannot choose an agent, business, or source identity.
        lead.update(source_id=source_id, assigned_agent=source.get("assigned_agent", ""),
                    business=source.get("business", "other"))
        result = svc.execute("create", {"lead": lead, "idempotency_key": event_id}, "source:" + source_id, "source")
        # Store delivery health separately; do not overwrite concurrently rotated
        # credentials or routing settings with this handler's earlier snapshot.
        svc.redis.hset(PREFIX + "source_health", source_id, json.dumps({
            "last_delivery_at": time.time(), "last_delivery_ok": result["ok"],
            "last_error": None if result["ok"] else result["error"]["code"],
        }))
        if result["ok"]:
            # Do not expose stored contact/history data back to a website.
            result["result"] = {"id": result["result"]["id"], "revision": result["result"]["revision"]}
        return envelope(result)
    except ValueError as exc:
        return failure("validation", str(exc), 413 if str(exc) == "payload_too_large" else 422)
    except Exception:
        return failure("unavailable", "CRM ingestion unavailable; retry same event_id", 503)