"""
Open Manus API Key Vault — Centralized key management for the agent team.

Architecture:
  - Admin GUI: Password-protected web dashboard for Garrett
  - Agent API: Token-authenticated endpoint for agents to fetch keys
  - Storage: Redis with Fernet encryption at rest
  - Audit: Every key access is logged
"""

import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request, Depends, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import redis
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vault")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
ADMIN_PASSWORD = os.getenv("VAULT_ADMIN_PASSWORD", "changeme")
VAULT_PORT = int(os.getenv("PORT", "8080"))

# Redis key prefixes
PFX_KEY = "vault:key:"
PFX_KEY_INDEX = "vault:keys"          # sorted set of key names
PFX_AGENT = "vault:agent:"
PFX_AGENT_INDEX = "vault:agents"      # sorted set of agent names
PFX_GRANT = "vault:grant:"            # vault:grant:{agent}:{key_name}
PFX_AUDIT = "vault:audit"             # list of audit entries
PFX_MASTER = "vault:master_key"
PFX_SKILL = "vault:skill:"            # vault:skill:{key_name} → skill/tool description
PFX_SKILL_INDEX = "vault:skills"      # sorted set

# Agent list (pre-populated)
AGENT_NAMES = [
    "harmony", "samantha", "addison", "bianca", "cora", "jade",
    "raven", "sabrina", "sasha", "scarlett", "tatiana", "valentina", "lexi"
]

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Open Manus Key Vault", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

r = redis.from_url(REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
def get_master_key() -> bytes:
    """Get or create the master encryption key."""
    stored = r.get(PFX_MASTER)
    if stored:
        return stored.encode()
    key = Fernet.generate_key()
    r.set(PFX_MASTER, key.decode())
    return key


def encrypt_value(plaintext: str) -> str:
    f = Fernet(get_master_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    f = Fernet(get_master_key())
    return f.decrypt(ciphertext.encode()).decode()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mask_value(value: str) -> str:
    """Show first 4 and last 4 chars, mask the rest."""
    if len(value) <= 12:
        return value[:2] + "*" * (len(value) - 4) + value[-2:]
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
def audit_log(agent: str, key_name: str, action: str, detail: str = ""):
    entry = {
        "agent": agent,
        "key_name": key_name,
        "action": action,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    r.lpush(PFX_AUDIT, json.dumps(entry))
    r.ltrim(PFX_AUDIT, 0, 999)  # Keep last 1000 entries


# ---------------------------------------------------------------------------
# Initialization — create agent tokens if not exist
# ---------------------------------------------------------------------------
def init_agents():
    """Ensure all agents have tokens."""
    for name in AGENT_NAMES:
        key = f"{PFX_AGENT}{name}"
        if not r.exists(key):
            token = secrets.token_urlsafe(32)
            r.hset(key, mapping={
                "name": name,
                "token_hash": hash_token(token),
                "token_plain": token,  # Stored only for admin display, encrypted at rest
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            r.zadd(PFX_AGENT_INDEX, {name: time.time()})
            logger.info(f"Created agent token for {name}")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
SESSION_TOKENS = {}  # In-memory session store (simple for single-instance)


def verify_admin_session(request: Request) -> bool:
    session_id = request.cookies.get("vault_session")
    if not session_id:
        return False
    return SESSION_TOKENS.get(session_id, 0) > time.time()


def require_admin(request: Request):
    if not verify_admin_session(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


def verify_agent_token(token: str) -> Optional[str]:
    """Verify an agent token and return the agent name."""
    token_h = hash_token(token)
    agents = r.zrange(PFX_AGENT_INDEX, 0, -1)
    for agent_name in agents:
        data = r.hgetall(f"{PFX_AGENT}{agent_name}")
        if data and data.get("token_hash") == token_h:
            return agent_name
    return None


# ---------------------------------------------------------------------------
# Admin GUI Routes
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        session_id = secrets.token_urlsafe(32)
        SESSION_TOKENS[session_id] = time.time() + 86400  # 24h session
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie("vault_session", session_id, httponly=True, max_age=86400)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("vault_session")
    if session_id:
        SESSION_TOKENS.pop(session_id, None)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("vault_session")
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    # Get all keys
    key_names = r.zrange(PFX_KEY_INDEX, 0, -1)
    keys = []
    for kn in key_names:
        data = r.hgetall(f"{PFX_KEY}{kn}")
        if data:
            try:
                masked = mask_value(decrypt_value(data.get("encrypted_value", "")))
            except Exception:
                masked = "***error***"
            data["masked_value"] = masked
            data["name"] = kn
            # Get which agents have access
            granted_agents = []
            for agent in AGENT_NAMES:
                if r.exists(f"{PFX_GRANT}{agent}:{kn}"):
                    granted_agents.append(agent)
            data["granted_agents"] = granted_agents
            data["grant_count"] = len(granted_agents)
            # Get skill description
            skill = r.hgetall(f"{PFX_SKILL}{kn}")
            data["skill_description"] = skill.get("description", "") if skill else ""
            keys.append(data)

    # Get all agents
    agents = []
    for name in AGENT_NAMES:
        data = r.hgetall(f"{PFX_AGENT}{name}")
        if data:
            # Count granted keys
            granted_keys = []
            for kn in key_names:
                if r.exists(f"{PFX_GRANT}{name}:{kn}"):
                    granted_keys.append(kn)
            data["granted_keys"] = granted_keys
            data["key_count"] = len(granted_keys)
            agents.append(data)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "keys": keys,
        "agents": agents,
        "agent_names": AGENT_NAMES,
        "key_names": key_names,
    })


@app.get("/keys", response_class=HTMLResponse)
async def keys_page(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    key_names = r.zrange(PFX_KEY_INDEX, 0, -1)
    keys = []
    for kn in key_names:
        data = r.hgetall(f"{PFX_KEY}{kn}")
        if data:
            try:
                masked = mask_value(decrypt_value(data.get("encrypted_value", "")))
            except Exception:
                masked = "***error***"
            data["masked_value"] = masked
            data["name"] = kn
            skill = r.hgetall(f"{PFX_SKILL}{kn}")
            data["skill_description"] = skill.get("description", "") if skill else ""
            keys.append(data)

    return templates.TemplateResponse("keys.html", {"request": request, "keys": keys})


@app.post("/keys/add")
async def add_key(
    request: Request,
    key_name: str = Form(...),
    key_value: str = Form(...),
    service: str = Form(""),
    description: str = Form(""),
    skill_description: str = Form(""),
):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    key_name = key_name.strip().upper().replace(" ", "_")
    now = datetime.now(timezone.utc).isoformat()

    r.hset(f"{PFX_KEY}{key_name}", mapping={
        "service": service,
        "description": description,
        "encrypted_value": encrypt_value(key_value),
        "created_at": now,
        "updated_at": now,
    })
    r.zadd(PFX_KEY_INDEX, {key_name: time.time()})

    if skill_description.strip():
        r.hset(f"{PFX_SKILL}{key_name}", mapping={
            "description": skill_description,
            "updated_at": now,
        })
        r.zadd(PFX_SKILL_INDEX, {key_name: time.time()})

    audit_log("admin", key_name, "key_created", f"Service: {service}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/keys/update")
async def update_key(
    request: Request,
    key_name: str = Form(...),
    key_value: str = Form(""),
    service: str = Form(""),
    description: str = Form(""),
    skill_description: str = Form(""),
):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    now = datetime.now(timezone.utc).isoformat()
    updates = {"updated_at": now}
    if key_value.strip():
        updates["encrypted_value"] = encrypt_value(key_value)
    if service is not None:
        updates["service"] = service
    if description is not None:
        updates["description"] = description

    r.hset(f"{PFX_KEY}{key_name}", mapping=updates)

    if skill_description is not None:
        r.hset(f"{PFX_SKILL}{key_name}", mapping={
            "description": skill_description,
            "updated_at": now,
        })
        r.zadd(PFX_SKILL_INDEX, {key_name: time.time()})

    audit_log("admin", key_name, "key_updated")
    return RedirectResponse(url="/", status_code=303)


@app.post("/keys/delete")
async def delete_key(request: Request, key_name: str = Form(...)):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    r.delete(f"{PFX_KEY}{key_name}")
    r.zrem(PFX_KEY_INDEX, key_name)
    r.delete(f"{PFX_SKILL}{key_name}")
    r.zrem(PFX_SKILL_INDEX, key_name)

    # Remove all grants for this key
    for agent in AGENT_NAMES:
        r.delete(f"{PFX_GRANT}{agent}:{key_name}")

    audit_log("admin", key_name, "key_deleted")
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Grant management
# ---------------------------------------------------------------------------
@app.get("/grants", response_class=HTMLResponse)
async def grants_page(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    key_names = r.zrange(PFX_KEY_INDEX, 0, -1)

    # Build grant matrix
    matrix = {}
    for agent in AGENT_NAMES:
        matrix[agent] = {}
        for kn in key_names:
            matrix[agent][kn] = r.exists(f"{PFX_GRANT}{agent}:{kn}")

    return templates.TemplateResponse("grants.html", {
        "request": request,
        "agents": AGENT_NAMES,
        "key_names": key_names,
        "matrix": matrix,
    })


@app.post("/grants/update")
async def update_grants(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    key_names = r.zrange(PFX_KEY_INDEX, 0, -1)
    now = datetime.now(timezone.utc).isoformat()

    for agent in AGENT_NAMES:
        for kn in key_names:
            field_name = f"grant_{agent}_{kn}"
            grant_key = f"{PFX_GRANT}{agent}:{kn}"
            if field_name in form:
                if not r.exists(grant_key):
                    r.hset(grant_key, mapping={"granted_at": now, "granted_by": "admin"})
                    audit_log("admin", kn, "grant_added", f"Granted to {agent}")
            else:
                if r.exists(grant_key):
                    r.delete(grant_key)
                    audit_log("admin", kn, "grant_removed", f"Revoked from {agent}")

    return RedirectResponse(url="/grants", status_code=303)


# ---------------------------------------------------------------------------
# Agent tokens page
# ---------------------------------------------------------------------------
@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    agents = []
    for name in AGENT_NAMES:
        data = r.hgetall(f"{PFX_AGENT}{name}")
        if data:
            agents.append(data)

    return templates.TemplateResponse("agents.html", {"request": request, "agents": agents})


@app.post("/agents/regenerate")
async def regenerate_token(request: Request, agent_name: str = Form(...)):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    token = secrets.token_urlsafe(32)
    r.hset(f"{PFX_AGENT}{agent_name}", mapping={
        "token_hash": hash_token(token),
        "token_plain": token,
    })
    audit_log("admin", agent_name, "token_regenerated")
    return RedirectResponse(url="/agents", status_code=303)


# ---------------------------------------------------------------------------
# Audit log page
# ---------------------------------------------------------------------------
@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)

    raw_entries = r.lrange(PFX_AUDIT, 0, 99)
    entries = [json.loads(e) for e in raw_entries]

    return templates.TemplateResponse("audit.html", {"request": request, "entries": entries})


# ---------------------------------------------------------------------------
# Agent API — Secure key fetch endpoint
# ---------------------------------------------------------------------------
class KeyFetchRequest(BaseModel):
    key_name: str


@app.get("/api/vault/fetch/{key_name}")
async def fetch_key(key_name: str, request: Request):
    """Agent endpoint: fetch a key value by name. Requires Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth[7:]
    agent_name = verify_agent_token(token)
    if not agent_name:
        raise HTTPException(status_code=401, detail="Invalid agent token")

    # Check grant
    if not r.exists(f"{PFX_GRANT}{agent_name}:{key_name}"):
        audit_log(agent_name, key_name, "fetch_denied", "No grant")
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' does not have access to '{key_name}'")

    # Fetch and decrypt
    data = r.hgetall(f"{PFX_KEY}{key_name}")
    if not data:
        raise HTTPException(status_code=404, detail=f"Key '{key_name}' not found")

    try:
        value = decrypt_value(data["encrypted_value"])
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt key")

    audit_log(agent_name, key_name, "fetch_success")

    return {"key_name": key_name, "value": value}


@app.get("/api/vault/list")
async def list_available_keys(request: Request):
    """Agent endpoint: list keys this agent has access to, with skill descriptions."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth[7:]
    agent_name = verify_agent_token(token)
    if not agent_name:
        raise HTTPException(status_code=401, detail="Invalid agent token")

    key_names = r.zrange(PFX_KEY_INDEX, 0, -1)
    available = []
    for kn in key_names:
        if r.exists(f"{PFX_GRANT}{agent_name}:{kn}"):
            key_data = r.hgetall(f"{PFX_KEY}{kn}")
            skill_data = r.hgetall(f"{PFX_SKILL}{kn}")
            available.append({
                "key_name": kn,
                "service": key_data.get("service", ""),
                "description": key_data.get("description", ""),
                "skill_description": skill_data.get("description", "") if skill_data else "",
            })

    audit_log(agent_name, "*", "list_keys")
    return {"agent": agent_name, "available_keys": available}


@app.get("/api/vault/skill/{key_name}")
async def get_skill_description(key_name: str, request: Request):
    """Agent endpoint: get the skill/tool usage description for a key."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth[7:]
    agent_name = verify_agent_token(token)
    if not agent_name:
        raise HTTPException(status_code=401, detail="Invalid agent token")

    if not r.exists(f"{PFX_GRANT}{agent_name}:{key_name}"):
        raise HTTPException(status_code=403, detail=f"No access to '{key_name}'")

    skill_data = r.hgetall(f"{PFX_SKILL}{key_name}")
    key_data = r.hgetall(f"{PFX_KEY}{key_name}")

    return {
        "key_name": key_name,
        "service": key_data.get("service", ""),
        "description": key_data.get("description", ""),
        "skill_description": skill_data.get("description", "") if skill_data else "",
    }


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    try:
        r.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    init_agents()
    logger.info("Vault service started. %d agents initialized.", len(AGENT_NAMES))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=VAULT_PORT)
