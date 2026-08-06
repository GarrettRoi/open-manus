"""
Open Manus API Key Vault — Zero-exposure credential management for the agent team.

Architecture:
  - Admin GUI: password-protected dashboard for managing service connections,
    grants, and OAuth logins.
  - Agent API: token-authenticated. Agents can LIST connections and PROXY
    requests through them — they can never fetch raw credentials.
  - Proxy: the vault injects the credential (API key or auto-refreshed OAuth
    access token) server-side and returns only the upstream response.
  - Storage: Redis with Fernet encryption at rest.
  - Audit: every proxy call, grant change, and admin action is logged.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
import redis
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from apple_ops import AppleOpsError, run_apple_operation

import email_ops
import google_ops
from catalog import CATALOG, get_template
from connections import (
    PFX_GRANT,
    AuthInjectionError,
    ConnectionStore,
    build_auth,
    normalize_id,
)
import oauth as oauth_mod
from oauth import OAuthError
import backup as vault_backup
import replit_mcp as replit_mcp_mod
from replit_mcp import ReplitMCPError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vault")

# ---------------------------------------------------------------------------
# Configuration
#
# Railway token push feature — required env vars (set on the vault Railway
# service, service ID 61b10056-76e2-4995-82a9-5c3c9c4681f0):
#   RAILWAY_ACCOUNT_API      Railway personal/account API token used to call
#                            the Railway GraphQL API (variableCollectionUpsert).
#   RAILWAY_VAULT_SERVICE_ID The vault's own Railway service ID
#                            (61b10056-76e2-4995-82a9-5c3c9c4681f0).
# Optional (defaults below match the Open Manus Agents project):
#   RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID
# If RAILWAY_ACCOUNT_API is missing, token pushes are skipped with a warning —
# the vault still works, but VAULT_TOKEN must be set on agents manually.
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
ADMIN_PASSWORD = os.getenv("VAULT_ADMIN_PASSWORD", "changeme")
# Shared secret for the JSON admin API (used by the fleet dashboard).
# Falls back to VAULT_ADMIN_PASSWORD unless that is still the default.
ADMIN_API_TOKEN = os.getenv("VAULT_ADMIN_TOKEN", "").strip()
VAULT_PORT = int(os.getenv("PORT", "8080"))
# Public URL of this vault (Railway domain) — required for OAuth callbacks.
PUBLIC_URL = (os.getenv("VAULT_PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
if PUBLIC_URL and not PUBLIC_URL.startswith("http"):
    PUBLIC_URL = f"https://{PUBLIC_URL}"

PFX_AGENT = "vault:agent:"
PFX_AGENT_INDEX = "vault:agents"
PFX_AUDIT = "vault:audit"
PFX_MASTER = "vault:master_key"

AGENT_NAMES = [
    "harmony", "samantha", "addison", "bianca", "cora", "jade",
    "raven", "sabrina", "sasha", "scarlett", "tatiana", "valentina", "lexi",
    "victoria", "vivian",
]

# Railway GraphQL — for pushing VAULT_TOKEN to each agent's service.
RAILWAY_API = "https://backboard.railway.com/graphql/v2"
RAILWAY_ACCOUNT_API = os.getenv("RAILWAY_ACCOUNT_API", "").strip()
RAILWAY_VAULT_SERVICE_ID = os.getenv(
    "RAILWAY_VAULT_SERVICE_ID", "61b10056-76e2-4995-82a9-5c3c9c4681f0").strip()
RAILWAY_PROJECT_ID = os.getenv(
    "RAILWAY_PROJECT_ID", "ea6649cb-ac92-44fd-bea9-3fbf6ad5e473").strip()
RAILWAY_ENVIRONMENT_ID = os.getenv(
    "RAILWAY_ENVIRONMENT_ID", "e57f146e-e0b8-4d5c-a443-c30e0baf016f").strip()

# Agent Railway service IDs (mirrors scripts/provision_env_vars.py).
RAILWAY_AGENT_SERVICE_IDS = {
    "harmony":   "fb56002a-09d9-48c5-87ab-6453bae2b325",
    "samantha":  "55729960-9915-4b58-be4b-0502418e5f60",
    "tatiana":   "35016475-6a1e-42a2-95be-1c3ef62982cb",
    "jade":      "5e296395-c8f8-451b-ab2d-5d46e9cf9699",
    "sasha":     "52155bb0-e561-4e58-9c1e-13ae5b359943",
    "scarlett":  "f4a3cad5-3328-4bf5-aab5-ce185ebb99ff",
    "sabrina":   "85b08450-d0f7-4454-a3b8-2118bd30cd6c",
    "cora":      "144238cf-424d-4e4c-af6b-b8ebdd25cebe",
    "raven":     "333c04b2-a264-429c-a11c-343b7eca19b7",
    "bianca":    "03310b9d-eb82-48d1-aef9-b206c358e85a",
    "valentina": "ffe6a337-2475-47ab-83f0-8fceb80312b0",
    "addison":   "4fbd8c66-944b-46b5-83b2-ce2f1c8b6bd9",
    "lexi":      "08006723-2b99-4fa5-aec0-f4afe96a242c",
    "victoria":  "",  # TODO: fill in once Victoria's Railway service exists
    "vivian":    "",  # TODO: fill in once Vivian's Railway service exists
}

# Agents allowed to create new API-key connections via the API.
STORE_ALLOWED_AGENTS = {"valentina", "harmony", "admin"}

# Proxy limits
PROXY_TIMEOUT_MAX = 120
PROXY_BODY_MAX = 10 * 1024 * 1024  # 10MB response cap
_BLOCKED_REQUEST_HEADERS = {
    "authorization", "cookie", "host", "content-length", "transfer-encoding",
    "x-n8n-api-key", "xi-api-key", "x-api-key",
}

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
    # Env var first (survives a Redis wipe — the July 2026 incident lost the
    # Redis-stored key along with everything it encrypted).
    env_key = os.getenv("VAULT_MASTER_KEY", "").strip()
    if env_key:
        return env_key.encode()
    stored = r.get(PFX_MASTER)
    if stored:
        return stored.encode()
    key = Fernet.generate_key()
    r.set(PFX_MASTER, key.decode())
    logger.warning(
        "VAULT_MASTER_KEY env var not set — generated a key in Redis. "
        "Set VAULT_MASTER_KEY on this service so the key survives Redis loss."
    )
    return key


def encrypt_value(plaintext: str) -> str:
    return Fernet(get_master_key()).encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    return Fernet(get_master_key()).decrypt(ciphertext.encode()).decode()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


store = ConnectionStore(r, encrypt_value, decrypt_value)


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
def audit_log(agent: str, target: str, action: str, detail: str = ""):
    entry = {
        "agent": agent,
        "key_name": target,  # field name kept for audit template compat
        "action": action,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    r.lpush(PFX_AUDIT, json.dumps(entry))
    r.ltrim(PFX_AUDIT, 0, 1999)


# ---------------------------------------------------------------------------
# Railway token push
# ---------------------------------------------------------------------------
async def push_vault_token_to_railway(agent_name: str, token_plain: str) -> bool:
    """Push VAULT_TOKEN to the agent's Railway service via GraphQL.

    Records the outcome on the agent's Redis hash (railway_sync_status /
    railway_sync_at). Never raises — returns True on success, False otherwise.
    """
    key = f"{PFX_AGENT}{agent_name}"

    def _record(status: str):
        try:
            r.hset(key, mapping={
                "railway_sync_status": status,
                "railway_sync_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass

    if not RAILWAY_ACCOUNT_API:
        logger.warning("RAILWAY_ACCOUNT_API not set — skipping Railway token "
                       "push for %s", agent_name)
        _record("pending")
        return False
    service_id = RAILWAY_AGENT_SERVICE_IDS.get(agent_name, "")
    if not service_id:
        logger.warning("No Railway service ID known for agent %s — skipping "
                       "token push", agent_name)
        _record("pending")
        return False

    mutation = """
    mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
        variableCollectionUpsert(input: $input)
    }
    """
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "projectId": RAILWAY_PROJECT_ID,
                "environmentId": RAILWAY_ENVIRONMENT_ID,
                "serviceId": service_id,
                "variables": {"VAULT_TOKEN": token_plain},
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                RAILWAY_API, json=payload,
                headers={"Authorization": f"Bearer {RAILWAY_ACCOUNT_API}"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
    except Exception as e:
        logger.warning("Railway token push failed for %s: %s", agent_name, e)
        _record("failed")
        return False
    logger.info("Pushed VAULT_TOKEN to Railway for %s", agent_name)
    _record("ok")
    audit_log("admin", agent_name, "railway_token_push", "VAULT_TOKEN synced to Railway")
    return True


# ---------------------------------------------------------------------------
# Agents / auth
# ---------------------------------------------------------------------------
async def init_agents():
    for name in AGENT_NAMES:
        key = f"{PFX_AGENT}{name}"
        if not r.exists(key):
            token = secrets.token_urlsafe(32)
            r.hset(key, mapping={
                "name": name,
                "token_hash": hash_token(token),
                "token_plain": token,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            r.zadd(PFX_AGENT_INDEX, {name: time.time()})
            logger.info("Created agent token for %s", name)
            # New token → push straight to the agent's Railway service.
            await push_vault_token_to_railway(name, token)


SESSION_TOKENS: Dict[str, float] = {}


def verify_admin_session(request: Request) -> bool:
    session_id = request.cookies.get("vault_session")
    return bool(session_id) and SESSION_TOKENS.get(session_id, 0) > time.time()


def verify_admin_api(request: Request) -> bool:
    """Admin auth for the JSON API: browser session OR shared-secret header.

    The header token is ``VAULT_ADMIN_TOKEN`` when set; otherwise the admin
    password is accepted — but never while it is still the insecure default.
    """
    if verify_admin_session(request):
        return True
    supplied = (request.headers.get("X-Vault-Admin-Token") or "").strip()
    if not supplied:
        return False
    expected = ADMIN_API_TOKEN or (ADMIN_PASSWORD if ADMIN_PASSWORD != "changeme" else "")
    return bool(expected) and secrets.compare_digest(supplied, expected)


def require_admin_api(request: Request) -> None:
    if not verify_admin_api(request):
        raise HTTPException(status_code=401, detail="Admin auth required")


def verify_agent_token(token: str) -> Optional[str]:
    token_h = hash_token(token)
    for agent_name in r.zrange(PFX_AGENT_INDEX, 0, -1):
        data = r.hgetall(f"{PFX_AGENT}{agent_name}")
        if data and data.get("token_hash") == token_h:
            return agent_name
    return None


def require_agent(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    agent_name = verify_agent_token(auth[7:])
    if not agent_name:
        raise HTTPException(status_code=401, detail="Invalid agent token")
    return agent_name


def _conn_view(conn: Dict[str, Any], include_secret_state: bool = True) -> Dict[str, Any]:
    """Public (secret-free) view of a connection."""
    tpl = get_template(conn.get("service", "")) or {}
    view = {
        "id": conn["id"],
        "service": conn.get("service", ""),
        "label": conn.get("label", ""),
        "base_url": conn.get("base_url", ""),
        "auth_kind": (conn.get("auth") or {}).get("kind", ""),
        "status": conn.get("status", ""),
        "description": conn.get("description", ""),
        "skill_description": conn.get("skill_description", ""),
        "example_call": tpl.get("example_call", ""),
        "created_at": conn.get("created_at", ""),
        "updated_at": conn.get("updated_at", ""),
    }
    conn_oauth = (conn.get("auth") or {}).get("oauth")
    if isinstance(conn_oauth, dict):
        view["custom_oauth"] = {
            "authorize_url": conn_oauth.get("authorize_url", ""),
            "token_url": conn_oauth.get("token_url", ""),
            "scopes": " ".join(conn_oauth.get("scopes") or []),
        }
    # Public key is public by definition (client-side/publishable keys) —
    # safe to show to admins AND agents; the secret key never leaves the vault.
    _pk_secrets = store.get_secrets(conn["id"])
    if _pk_secrets.get("public_key"):
        view["public_key"] = _pk_secrets["public_key"]
    if include_secret_state:
        secrets_d = _pk_secrets
        if isinstance(secrets_d.get("extra_headers"), dict):
            # names only — values may embed credentials
            view["extra_header_names"] = sorted(secrets_d["extra_headers"].keys())
        if view["auth_kind"] == "header":
            view["auth_header_name"] = (conn.get("auth") or {}).get("header_name", "")
            view["auth_prefix"] = (conn.get("auth") or {}).get("prefix", "")
        if view["auth_kind"] == "oauth2":
            view["connected"] = bool(secrets_d.get("access_token"))
            exp = secrets_d.get("expires_at")
            view["token_expires_at"] = (
                datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat() if exp else ""
            )
            view["has_client"] = bool(secrets_d.get("client_id"))
        elif view["auth_kind"] == "apple":
            view["connected"] = bool(secrets_d.get("apple_id") and secrets_d.get("app_password"))
        elif view["auth_kind"] == "email":
            view["connected"] = bool(secrets_d.get("password"))
            view["email_address"] = secrets_d.get("username", "")
        else:
            view["connected"] = bool(secrets_d.get("api_key"))
    return view


# ---------------------------------------------------------------------------
# Admin GUI — login/logout
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        session_id = secrets.token_urlsafe(32)
        SESSION_TOKENS[session_id] = time.time() + 86400
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie("vault_session", session_id, httponly=True, max_age=86400,
                            samesite="lax", secure=True)
        return response
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid password"})


@app.get("/logout")
async def logout(request: Request):
    session_id = request.cookies.get("vault_session")
    if session_id:
        SESSION_TOKENS.pop(session_id, None)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("vault_session")
    return response


def _admin_or_redirect(request: Request):
    if not verify_admin_session(request):
        return RedirectResponse(url="/login", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Admin GUI — dashboard & services
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp

    conn_ids = store.list_ids()
    conns = []
    for cid in conn_ids:
        conn = store.get(cid)
        if not conn:
            continue
        view = _conn_view(conn)
        view["granted_agents"] = [a for a in AGENT_NAMES if store.has_grant(a, cid)]
        view["grant_count"] = len(view["granted_agents"])
        conns.append(view)

    agents = []
    for name in AGENT_NAMES:
        data = r.hgetall(f"{PFX_AGENT}{name}")
        if data:
            granted = [cid for cid in conn_ids if store.has_grant(name, cid)]
            data["granted_keys"] = granted
            data["key_count"] = len(granted)
            agents.append(data)

    return templates.TemplateResponse(request, "dashboard.html", {
        "connections": conns,
        "agents": agents,
        "agent_names": AGENT_NAMES,
    })


@app.get("/services", response_class=HTMLResponse)
async def services_page(request: Request, error: str = "", notice: str = ""):
    if (resp := _admin_or_redirect(request)):
        return resp

    conns = []
    for cid in store.list_ids():
        conn = store.get(cid)
        if conn:
            conns.append(_conn_view(conn))

    return templates.TemplateResponse(request, "services.html", {
        "connections": conns,
        "catalog": CATALOG,
        "requests": store.list_requests(),
        "redirect_uri": oauth_mod.redirect_uri(PUBLIC_URL) if PUBLIC_URL else "",
        "public_url_missing": not PUBLIC_URL,
        "error": error,
        "notice": notice,
    })


@app.post("/requests/dismiss")
async def dismiss_request(request: Request, request_id: str = Form(...)):
    if (resp := _admin_or_redirect(request)):
        return resp
    store.delete_request(request_id)
    return RedirectResponse("/services?notice=Request+dismissed", status_code=303)


@app.post("/requests/approve-grant")
async def approve_grant_request(request: Request, request_id: str = Form(...)):
    """One-click approve for a pending grant request."""
    if (resp := _admin_or_redirect(request)):
        return resp
    parts = request_id.split(":", 2)
    # Grant only through a real pending request — keeps audit trail consistent.
    if len(parts) == 3 and parts[0] == "grant" and store.get_request(request_id):
        _, agent, cid = parts
        if agent in AGENT_NAMES and store.get(cid):
            store.set_grant(agent, cid, True)
            audit_log("admin", cid, "grant_added", f"Approved request from {agent}")
            store.delete_request(request_id)
            return RedirectResponse(
                f"/services?notice=Granted+{cid}+to+{agent}", status_code=303)
    return RedirectResponse("/services?error=Invalid+grant+request", status_code=303)


def _validate_oauth_endpoint_url(label: str, url: str) -> str:
    """Strict validation for admin-supplied OAuth endpoint URLs (SSRF/open-
    redirect guard). Returns an error message, or '' if the URL is safe."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    if not url:
        return f"{label} is required for a custom OAuth app"
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"{label} is not a valid URL"
    if parts.scheme != "https":
        return f"{label} must start with https://"
    if parts.username or parts.password:
        return f"{label} must not contain credentials"
    host = parts.hostname or ""
    if not host or "." not in host:
        return f"{label} must use a public hostname"
    # Reject IP literals and hostnames resolving to private/reserved ranges.
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return f"{label}: hostname does not resolve"
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_global:
            return f"{label} must not point at a private or internal address"
    return ""


def _validate_mail_host(label: str, host: str) -> str:
    """SSRF guard for admin-supplied IMAP/SMTP hostnames. Returns an error
    message, or '' if the host is safe (public hostname, resolves to global
    IPs only — no IP literals, loopback, private ranges, or metadata IPs)."""
    import ipaddress
    import socket
    if not host:
        return f"{label} server is required"
    if any(c in host for c in "/@:?#[] \t"):
        return f"{label} server must be a bare hostname (no URL, port, or path)"
    try:
        ipaddress.ip_address(host)
        return f"{label} server must be a hostname, not an IP address"
    except ValueError:
        pass
    if "." not in host or host.endswith(".internal") or host.endswith(".local"):
        return f"{label} server must be a public hostname"
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except socket.gaierror:
        return f"{label} server hostname does not resolve"
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_global:
            return f"{label} server must not point at a private or internal address"
    return ""


def _parse_email_form(form) -> tuple:
    """Validate email (IMAP/SMTP) connection fields. Returns (secrets, error)."""
    username = (form.get("username") or "").strip()
    password = (form.get("password") or form.get("api_key") or "").strip()
    imap_host = (form.get("imap_host") or "").strip().lower()
    smtp_host = (form.get("smtp_host") or "").strip().lower()
    if not username or "@" not in username:
        return None, "A valid email address is required"
    if not password:
        return None, "The mailbox password (or app password) is required"
    if not imap_host or not smtp_host:
        return None, "IMAP and SMTP server hostnames are required"
    for label, host in (("IMAP", imap_host), ("SMTP", smtp_host)):
        if (err := _validate_mail_host(label, host)):
            return None, err
    try:
        imap_port = int(form.get("imap_port") or 993)
        smtp_port = int(form.get("smtp_port") or 587)
    except (TypeError, ValueError):
        return None, "Ports must be numbers"
    if not (0 < imap_port < 65536 and 0 < smtp_port < 65536):
        return None, "Ports must be between 1 and 65535"
    return {
        "username": username,
        "password": password,
        "imap_host": imap_host,
        "imap_port": str(imap_port),
        "smtp_host": smtp_host,
        "smtp_port": str(smtp_port),
    }, ""


_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection",
                      "cookie", "upgrade", "te", "trailer",
                      # the vault injects auth itself — extras may never carry
                      # or override the credential header
                      "authorization", "proxy-authorization"}


def _parse_extra_headers(form) -> tuple:
    """Extra static headers for custom API connections.

    Accepts either repeated form fields (extra_header_name / extra_header_value
    — from the dashboard's add-row UI) or an ``extra_headers`` object (JSON
    admin API). Returns (dict | None, error). None means "not provided".
    """
    pairs: list = []
    if hasattr(form, "getlist"):
        names = form.getlist("extra_header_name")
        values = form.getlist("extra_header_value")
        pairs = list(zip(names, values))
    raw = form.get("extra_headers") if hasattr(form, "get") else None
    if raw is not None and not pairs:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None, "extra_headers must be a JSON object"
        if not isinstance(raw, dict):
            return None, "extra_headers must be an object of header name -> value"
        pairs = list(raw.items())
    if not pairs:
        return None, ""
    headers: Dict[str, str] = {}
    for name, value in pairs:
        name = str(name or "").strip()
        value = str(value or "").strip()
        if not name and not value:
            continue  # empty row from the UI
        if not _HEADER_NAME_RE.match(name):
            return None, f"Invalid header name: {name!r}"
        if name.lower() in _FORBIDDEN_HEADERS:
            return None, f"Header {name!r} cannot be overridden"
        if len(value) > 4096:
            return None, f"Header {name!r} value is too long"
        headers[name] = value
    return headers, ""


def _parse_custom_oauth_form(form) -> tuple:
    """Validate custom-OAuth fields from a form. Returns (oauth_cfg, error)."""
    authorize_url = (form.get("authorize_url") or "").strip()
    token_url = (form.get("token_url") or "").strip()
    scopes = [s for s in (form.get("scopes") or "").replace(",", " ").split() if s]
    for label, url in (("Authorization URL", authorize_url), ("Token URL", token_url)):
        if (err := _validate_oauth_endpoint_url(label, url)):
            return None, err
    cfg: Dict[str, Any] = {"authorize_url": authorize_url, "token_url": token_url,
                           "scopes": scopes}
    return cfg, ""


@app.post("/services/add")
async def add_service(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp

    form = await request.form()
    service = (form.get("service") or "custom").strip().lower()
    tpl = get_template(service)
    if not tpl:
        return RedirectResponse(url="/services?error=Unknown+service", status_code=303)

    name = form.get("name") or service
    conn_id = normalize_id(name)
    if not conn_id:
        return RedirectResponse(url="/services?error=Name+required", status_code=303)

    auth = dict(tpl["auth"])
    base_url = (form.get("base_url") or tpl.get("base_url") or "").strip()
    if auth["kind"] == "header":
        # allow custom-template header overrides
        # None = field absent (keep default); "" = intentionally blank
        _raw_name = form.get("header_name")
        if _raw_name is not None:
            # blank means "use Authorization" — store explicitly
            auth["header_name"] = _raw_name.strip() or "Authorization"
        if form.get("prefix") is not None and service == "custom":
            # blank means "no prefix / raw key" — preserve as-is (no strip:
            # trailing space in "Bearer " is intentional)
            auth["prefix"] = str(form.get("prefix"))

    secrets_d: Dict[str, Any] = {}
    status = "ready"
    if auth["kind"] == "oauth2":
        client_id = (form.get("client_id") or "").strip()
        client_secret = (form.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            return RedirectResponse(
                url="/services?error=Client+ID+and+secret+are+required+for+OAuth+services",
                status_code=303)
        if tpl.get("custom_oauth"):
            oauth_cfg, err = _parse_custom_oauth_form(form)
            if err:
                return RedirectResponse(url=f"/services?error={err.replace(' ', '+')}", status_code=303)
            if not base_url:
                return RedirectResponse(url="/services?error=Base+URL+required", status_code=303)
            auth["oauth"] = oauth_cfg
        secrets_d = {"client_id": client_id, "client_secret": client_secret}
        status = "needs_login"
    elif auth["kind"] == "apple":
        apple_id = (form.get("apple_id") or "").strip()
        app_password = (form.get("apple_app_password") or "").strip()
        if not apple_id or not app_password:
            return RedirectResponse(
                url="/services?error=Apple+ID+and+app-specific+password+are+required",
                status_code=303)
        try:
            await run_apple_operation(apple_id, app_password, "calendar_list", {})
        except AppleOpsError as exc:
            return RedirectResponse(
                url=f"/services?error=Apple+connection+failed:+{str(exc)[:120].replace(' ', '+')}",
                status_code=303)
        secrets_d = {"apple_id": apple_id, "app_password": app_password}
    elif auth["kind"] == "email":
        secrets_d, err = _parse_email_form(form)
        if err:
            return RedirectResponse(
                url=f"/services?error={err.replace(' ', '+')}", status_code=303)
        base_url = ""
    else:
        api_key = (form.get("api_key") or "").strip()
        if not api_key:
            return RedirectResponse(url="/services?error=API+key+required", status_code=303)
        secrets_d = {"api_key": api_key}
        public_key = (form.get("public_key") or "").strip()
        if public_key:
            secrets_d["public_key"] = public_key
        extra_headers, err = _parse_extra_headers(form)
        if err:
            return RedirectResponse(url=f"/services?error={err.replace(' ', '+')}", status_code=303)
        if extra_headers:
            secrets_d["extra_headers"] = extra_headers
        if not base_url:
            return RedirectResponse(url="/services?error=Base+URL+required", status_code=303)

    store.save(
        conn_id, service=service, label=form.get("label") or tpl["label"],
        base_url=base_url, auth=auth, secrets=secrets_d,
        description=form.get("description") or "",
        skill_description=form.get("skill_description") or "",
        status=status,
    )
    audit_log("admin", conn_id, "connection_created", f"Service: {service}")

    if status == "needs_login":
        return RedirectResponse(url=f"/services/{conn_id}/connect", status_code=303)
    return RedirectResponse(url="/services?notice=Connection+added", status_code=303)


@app.post("/services/update")
async def update_service(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp
    form = await request.form()
    conn_id = normalize_id(form.get("conn_id") or "")
    conn = store.get(conn_id)
    if not conn:
        return RedirectResponse(url="/services?error=Not+found", status_code=303)

    secrets_d = store.get_secrets(conn_id)
    api_key = (form.get("api_key") or "").strip()
    if api_key:
        secrets_d["api_key"] = api_key
    public_key = (form.get("public_key") or "").strip()
    if public_key:
        secrets_d["public_key"] = public_key
    extra_headers, hdr_err = _parse_extra_headers(form)
    if hdr_err:
        return RedirectResponse(
            url=f"/services?error={hdr_err.replace(' ', '+')}", status_code=303)
    if extra_headers is not None:
        # any non-empty rows replace the whole set; a single row with name
        # "-" and empty value clears all extra headers
        if extra_headers == {"-": ""} or list(extra_headers.keys()) == ["-"]:
            secrets_d.pop("extra_headers", None)
        elif extra_headers:
            secrets_d["extra_headers"] = extra_headers
    # auth header name / prefix edits for header-kind (custom) connections
    # Semantics: None (field absent from POST) = unchanged; "" = explicit clear.
    # header_name blank → store "Authorization" explicitly (cleaner than relying
    # on the builder's `or` fallback).
    # prefix blank → store "" (raw key, no prefix — e.g. Alpaca-style).
    # Do NOT strip prefix: trailing space in "Bearer " is intentional.
    _auth0 = conn.get("auth") or {}
    if _auth0.get("kind") == "header":
        changed = False
        _auth0 = dict(_auth0)
        _raw_name = form.get("header_name")   # None if field not in POST body
        _raw_pfx  = form.get("prefix")        # None if field not in POST body
        if _raw_name is not None:
            _auth0["header_name"] = _raw_name.strip() or "Authorization"
            changed = True
        if _raw_pfx is not None:
            _auth0["prefix"] = str(_raw_pfx)
            changed = True
        if changed:
            conn["auth"] = _auth0
    if (conn.get("auth") or {}).get("kind") == "email":
        for fld in ("username", "password", "imap_host", "imap_port",
                    "smtp_host", "smtp_port"):
            val = (form.get(fld) or "").strip()
            if not val:
                continue
            if fld in ("imap_host", "smtp_host"):
                val = val.lower()
                if (err := _validate_mail_host(fld.split("_")[0].upper(), val)):
                    return RedirectResponse(
                        url=f"/services?error={err.replace(' ', '+')}",
                        status_code=303)
            if fld in ("imap_port", "smtp_port") and not val.isdigit():
                return RedirectResponse(
                    url="/services?error=Ports+must+be+numbers", status_code=303)
            secrets_d[fld] = val
    client_id = (form.get("client_id") or "").strip()
    client_secret = (form.get("client_secret") or "").strip()
    if client_id:
        secrets_d["client_id"] = client_id
    if client_secret:
        secrets_d["client_secret"] = client_secret
    if (conn.get("auth") or {}).get("kind") == "apple":
        apple_id = (form.get("apple_id") or "").strip()
        app_password = (form.get("apple_app_password") or "").strip()
        if apple_id:
            secrets_d["apple_id"] = apple_id
        if app_password:
            secrets_d["app_password"] = app_password
    if (conn.get("auth") or {}).get("kind") == "email":
        email_address = (form.get("email_address") or "").strip()
        app_password = (form.get("email_app_password") or "").strip()
        if email_address:
            secrets_d["email_address"] = email_address
        if app_password:
            secrets_d["app_password"] = app_password

    auth = conn.get("auth")
    # Custom OAuth connections can update their endpoint config too.
    if (auth or {}).get("kind") == "oauth2" and (auth or {}).get("oauth") is not None \
            and (form.get("authorize_url") or form.get("token_url") or form.get("scopes")):
        merged = dict(auth.get("oauth") or {})
        if form.get("authorize_url"):
            merged["authorize_url"] = form.get("authorize_url").strip()
        if form.get("token_url"):
            merged["token_url"] = form.get("token_url").strip()
        if form.get("scopes"):
            merged["scopes"] = [s for s in form.get("scopes").replace(",", " ").split() if s]
        for label, key in (("Authorization URL", "authorize_url"), ("Token URL", "token_url")):
            if (err := _validate_oauth_endpoint_url(label, str(merged.get(key, "")))):
                return RedirectResponse(
                    url=f"/services?error={err.replace(' ', '+')}", status_code=303)
        auth = dict(auth)
        auth["oauth"] = merged

    store.save(
        conn_id, service=conn["service"],
        label=form.get("label") or conn.get("label", ""),
        base_url=(form.get("base_url") or conn.get("base_url", "")).strip(),
        auth=auth,
        secrets=secrets_d,
        description=form.get("description", conn.get("description", "")),
        skill_description=form.get("skill_description", conn.get("skill_description", "")),
        status=conn.get("status", "ready"),
    )
    audit_log("admin", conn_id, "connection_updated")
    return RedirectResponse(url="/services?notice=Connection+updated", status_code=303)


@app.post("/services/delete")
async def delete_service(request: Request, conn_id: str = Form(...)):
    if (resp := _admin_or_redirect(request)):
        return resp
    store.delete(normalize_id(conn_id), AGENT_NAMES)
    audit_log("admin", conn_id, "connection_deleted")
    return RedirectResponse(url="/services?notice=Connection+deleted", status_code=303)


# ---------------------------------------------------------------------------
# Admin GUI — OAuth connect flow
# ---------------------------------------------------------------------------
@app.get("/services/{conn_id}/connect")
async def oauth_connect(request: Request, conn_id: str):
    if (resp := _admin_or_redirect(request)):
        return resp
    conn = store.get(normalize_id(conn_id))
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if (conn.get("auth") or {}).get("kind") != "oauth2":
        return RedirectResponse(url="/services?error=Not+an+OAuth+service", status_code=303)
    if not PUBLIC_URL:
        return RedirectResponse(
            url="/services?error=Set+VAULT_PUBLIC_URL+env+var+to+enable+OAuth+logins",
            status_code=303)
    secrets_d = store.get_secrets(conn["id"])
    if not secrets_d.get("client_id"):
        return RedirectResponse(url="/services?error=Add+the+OAuth+client+ID+first", status_code=303)
    try:
        url = oauth_mod.build_authorize_url(
            conn["service"], conn["id"], secrets_d["client_id"], PUBLIC_URL, store,
            conn=conn)
    except OAuthError as exc:
        return RedirectResponse(url=f"/services?error={str(exc).replace(' ', '+')}", status_code=303)
    return RedirectResponse(url=url, status_code=303)


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    # Auth model: the single-use, server-generated OAuth state token IS the
    # authorization for this callback (it can only exist if an admin — via
    # the vault UI or the token-authed dashboard API — started the flow).
    # Requiring a vault session here would break dashboard-initiated logins.
    has_session = verify_admin_session(request)
    if error:
        if has_session:
            return RedirectResponse(url=f"/services?error=OAuth+denied:+{error}", status_code=303)
        return HTMLResponse(f"<h3>OAuth denied: {error}</h3><p>You can close this tab.</p>", status_code=400)
    pending = store.pop_oauth_state(state) if state else None
    if not pending or not code:
        if has_session:
            return RedirectResponse(url="/services?error=Invalid+or+expired+OAuth+state", status_code=303)
        return HTMLResponse("<h3>Invalid or expired OAuth state.</h3><p>Go back to the dashboard and click Connect again.</p>", status_code=400)

    conn_id = pending["conn_id"]
    conn = store.get(conn_id)
    if not conn:
        if has_session:
            return RedirectResponse(url="/services?error=Connection+vanished", status_code=303)
        return HTMLResponse("<h3>Connection no longer exists.</h3>", status_code=404)
    secrets_d = store.get_secrets(conn_id)
    try:
        tokens = await oauth_mod.exchange_code(
            pending["service"], code, secrets_d.get("client_id", ""),
            secrets_d.get("client_secret", ""), PUBLIC_URL, conn=conn)
    except OAuthError as exc:
        audit_log("admin", conn_id, "oauth_failed", str(exc)[:200])
        return RedirectResponse(url=f"/services?error={str(exc)[:120].replace(' ', '+')}", status_code=303)

    secrets_d.update(tokens)
    store.set_secrets(conn_id, secrets_d)
    r.hset(f"vault:conn:{conn_id}", "status", "ready")
    audit_log("admin", conn_id, "oauth_connected", f"Service: {pending['service']}")
    if has_session:
        return RedirectResponse(url="/services?notice=Connected+successfully", status_code=303)
    return HTMLResponse(
        "<h3>Connected successfully ✅</h3>"
        "<p>This account is now stored in the vault. You can close this tab "
        "and return to the dashboard.</p>")


# ---------------------------------------------------------------------------
# Admin GUI — grants / agents / audit
# ---------------------------------------------------------------------------
@app.get("/grants", response_class=HTMLResponse)
async def grants_page(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp
    conn_ids = store.list_ids()
    matrix = {
        agent: {cid: store.has_grant(agent, cid) for cid in conn_ids}
        for agent in AGENT_NAMES
    }
    return templates.TemplateResponse(request, "grants.html", {
        "agents": AGENT_NAMES,
        "key_names": conn_ids,
        "matrix": matrix,
    })


@app.post("/grants/update")
async def update_grants(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp
    form = await request.form()
    conn_ids = store.list_ids()
    for agent in AGENT_NAMES:
        for cid in conn_ids:
            granted = f"grant_{agent}_{cid}" in form
            if store.set_grant(agent, cid, granted):
                audit_log("admin", cid,
                          "grant_added" if granted else "grant_removed",
                          f"{'Granted to' if granted else 'Revoked from'} {agent}")
    return RedirectResponse(url="/grants", status_code=303)


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp
    agents = [d for name in AGENT_NAMES if (d := r.hgetall(f"{PFX_AGENT}{name}"))]
    return templates.TemplateResponse(request, "agents.html", {"agents": agents})


@app.post("/agents/regenerate")
async def regenerate_token(request: Request, agent_name: str = Form(...)):
    if (resp := _admin_or_redirect(request)):
        return resp
    token = secrets.token_urlsafe(32)
    r.hset(f"{PFX_AGENT}{agent_name}", mapping={
        "token_hash": hash_token(token),
        "token_plain": token,
    })
    audit_log("admin", agent_name, "token_regenerated")
    await push_vault_token_to_railway(agent_name, token)
    return RedirectResponse(url="/agents", status_code=303)


@app.post("/api/admin/sync-tokens")
async def sync_all_tokens(request: Request):
    """Push every agent's current VAULT_TOKEN to Railway (bulk recovery)."""
    if not verify_admin_session(request):
        raise HTTPException(status_code=401, detail="Admin session required")
    results: Dict[str, str] = {}
    for name in AGENT_NAMES:
        data = r.hgetall(f"{PFX_AGENT}{name}")
        token = (data or {}).get("token_plain", "")
        if not token:
            results[name] = "no_token"
            continue
        results[name] = "ok" if await push_vault_token_to_railway(name, token) else "failed"
    ok = sum(1 for v in results.values() if v == "ok")
    audit_log("admin", "all_agents", "railway_token_sync",
              f"Bulk sync: {ok}/{len(results)} pushed")
    return {"synced": ok, "total": len(results), "results": results}


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    if (resp := _admin_or_redirect(request)):
        return resp
    entries = [json.loads(e) for e in r.lrange(PFX_AUDIT, 0, 199)]
    return templates.TemplateResponse(request, "audit.html", {"entries": entries})


# ---------------------------------------------------------------------------
# Agent API
# ---------------------------------------------------------------------------
@app.get("/api/vault/fetch/{key_name}")
async def fetch_key_removed(key_name: str, request: Request):
    """Raw key fetch is gone — the vault is proxy-only now."""
    agent = None
    try:
        agent = require_agent(request)
    except HTTPException:
        pass
    audit_log(agent or "unknown", key_name, "fetch_blocked", "Raw fetch removed")
    raise HTTPException(
        status_code=410,
        detail=(
            "Raw key fetch has been removed. Use the proxy instead: "
            "POST /api/vault/proxy/{connection} with "
            '{"method": "GET", "path": "/..."} — the vault attaches the '
            "credential for you. See /api/vault/list for your connections."
        ),
    )


@app.get("/api/vault/list")
async def list_connections(request: Request):
    agent_name = require_agent(request)
    available = []
    for cid in store.list_ids():
        if not store.has_grant(agent_name, cid):
            continue
        conn = store.get(cid)
        if not conn:
            continue
        view = _conn_view(conn, include_secret_state=False)
        view.pop("created_at", None)
        view.pop("updated_at", None)
        if view.get("auth_kind") == "email":
            view["how_to_call"] = (
                f"POST {{vault}}/api/vault/email/{cid} with JSON "
                '{"action": "folders|list|search|read|send|attachment", ...} — e.g. '
                '{"action": "list", "limit": 10, "unseen_only": true}, '
                '{"action": "search", "from": "john", "last_days": 20, '
                '"has_attachment": true}, '
                '{"action": "read", "uid": "..."}, or '
                '{"action": "send", "to": "a@b.com", "subject": "...", "body": "..."}'
            )
        else:
            view["how_to_call"] = (
                f"POST {{vault}}/api/vault/proxy/{cid} with JSON "
                '{"method": "GET|POST|...", "path": "/...", "params": {...}, '
                '"json": {...}, "headers": {...}}'
            )
        available.append(view)
    # Agents' background tool-sync polls this every few minutes; keep those
    # out of the audit trail so real activity stays visible.
    if request.headers.get("x-vault-background") != "1":
        audit_log(agent_name, "*", "list_keys")
    return {"agent": agent_name, "available_connections": available,
            # legacy field name so old client code degrades readably
            "available_keys": available}


@app.get("/api/vault/skill/{conn_id}")
async def get_skill_description(conn_id: str, request: Request):
    agent_name = require_agent(request)
    cid = normalize_id(conn_id)
    if not store.has_grant(agent_name, cid):
        raise HTTPException(status_code=403, detail=f"No access to '{cid}'")
    conn = store.get(cid)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection '{cid}' not found")
    if (conn.get("auth") or {}).get("kind") in {"apple", "email"}:
        raise HTTPException(
            status_code=409,
            detail="Use the connection's dedicated Apple or email operation tool",
        )
    view = _conn_view(conn, include_secret_state=False)
    return {
        "key_name": cid,
        "connection": cid,
        "service": view["service"],
        "description": view["description"],
        "skill_description": view["skill_description"],
        "base_url": view["base_url"],
        "example_call": view["example_call"],
    }


@app.get("/api/vault/resolve")
async def resolve_service(request: Request, service: str):
    """Lightweight 'does this exist?' check — agents call this FIRST when a
    task needs an external API. Returns whether a connection exists, whether
    this agent can use it, and how to get it set up if not."""
    agent_name = require_agent(request)
    q = service.strip()
    conn = store.find_connection(q)
    audit_log(agent_name, q, "resolve", f"found={bool(conn)}")

    if conn:
        cid = conn["id"]
        granted = store.has_grant(agent_name, cid)
        view = _conn_view(conn, include_secret_state=False)
        ready = conn.get("status") == "ready" and (
            (conn.get("auth") or {}).get("kind") != "oauth2"
            or bool(store.get_secrets(cid).get("access_token"))
        )
        return {
            "found": True,
            "connection": cid,
            "service": view["service"],
            "label": view["label"],
            "granted": granted,
            "ready": ready,
            "example_call": view["example_call"] if granted else None,
            "next_step": (
                "Use POST /api/vault/proxy/" + cid if granted and ready else
                "POST /api/vault/request to ask for access — the owner will see it in the vault dashboard"
            ),
        }

    # No connection — do we at least have a template for it?
    tpl_id = q.lower()
    tpl = get_template(tpl_id)
    return {
        "found": False,
        "template": tpl_id if tpl else None,
        "template_label": tpl["label"] if tpl else None,
        "oauth": bool(tpl and tpl["auth"]["kind"] == "oauth2") if tpl else None,
        "known_templates": sorted(CATALOG.keys()),
        "next_step": (
            "POST /api/vault/request with {service, name, reason} to file a setup "
            "request — the owner will finish it in the vault dashboard"
        ),
    }


class AccessRequest(BaseModel):
    service: str
    name: str = ""
    reason: str = ""


@app.post("/api/vault/request")
async def request_access(request: Request, body: AccessRequest):
    """Agent-initiated setup: file a pending request the owner resolves in the
    dashboard (add the key / complete the OAuth login / flip the grant)."""
    agent_name = require_agent(request)
    q = body.service.strip()[:64]
    if not q:
        raise HTTPException(status_code=400, detail="'service' is required")
    if not store.check_request_rate(agent_name):
        audit_log(agent_name, q, "request_rate_limited")
        raise HTTPException(
            status_code=429,
            detail="Too many vault requests this hour — a request is probably "
                   "already pending. Ask the owner to check the dashboard.")
    conn = store.find_connection(q)

    if conn:
        cid = conn["id"]
        if store.has_grant(agent_name, cid):
            ready = conn.get("status") == "ready"
            return {"status": "already_available" if ready else "pending_setup",
                    "connection": cid,
                    "message": (
                        f"You already have access to '{cid}' — call it via the proxy."
                        if ready else
                        f"'{cid}' exists and you have access, but the owner still needs "
                        f"to finish its setup (status: {conn.get('status')})."
                    )}
        req = store.create_request(agent_name, "grant", cid,
                                   service=conn.get("service", ""), reason=body.reason)
        audit_log(agent_name, cid, "access_requested", body.reason[:200])
        return {"status": "grant_requested", "connection": cid,
                "already_pending": req["already_pending"],
                "message": (
                    f"'{cid}' is already set up in the vault — I've filed a request for you "
                    f"to be granted access. Tell the user: approve it at {PUBLIC_URL or 'the vault dashboard'}/services."
                )}

    tpl_id = q.lower()
    tpl = get_template(tpl_id)
    target = (body.name.strip()[:64] or (tpl_id if tpl else q))
    req = store.create_request(agent_name, "connection", target,
                               service=tpl_id if tpl else "custom", reason=body.reason)
    audit_log(agent_name, target, "connection_requested",
              f"service={tpl_id if tpl else 'custom'} {body.reason[:150]}")
    is_oauth = bool(tpl and tpl["auth"]["kind"] == "oauth2")
    return {"status": "connection_requested",
            "service": tpl_id if tpl else "custom",
            "oauth": is_oauth,
            "already_pending": req["already_pending"],
            "message": (
                f"No '{q}' connection exists yet — I've filed a setup request. "
                f"Tell the user: open {PUBLIC_URL or 'the vault dashboard'}/services to "
                + ("log in with their account (OAuth)." if is_oauth else "add the API key.")
            )}


class KeyStoreRequest(BaseModel):
    key_name: str
    key_value: str
    service: str = ""
    description: str = ""
    skill_description: str = ""
    base_url: str = ""


@app.post("/api/vault/store")
async def store_key(request: Request, key_data: KeyStoreRequest):
    """Create an API-key connection (restricted agents only)."""
    agent_name = require_agent(request)
    if agent_name not in STORE_ALLOWED_AGENTS:
        audit_log(agent_name, key_data.key_name, "store_denied", "Insufficient privileges")
        raise HTTPException(status_code=403, detail="Agent does not have permission to store keys")

    service = (key_data.service or "custom").strip().lower()
    tpl = get_template(service)
    if not tpl or tpl["auth"]["kind"] == "oauth2":
        service, tpl = "custom", CATALOG["custom"]

    api_key = (key_data.key_value or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="key_value must not be empty")

    conn_id = store.save(
        key_data.key_name, service=service,
        label=key_data.service or key_data.key_name,
        base_url=key_data.base_url or tpl.get("base_url") or "",
        secrets={"api_key": api_key},
        description=key_data.description,
        skill_description=key_data.skill_description,
        status="ready" if (key_data.base_url or tpl.get("base_url")) else "needs_base_url",
    )
    audit_log(agent_name, conn_id, "connection_created_via_api", f"Service: {service}")
    return {"success": True, "key_name": conn_id, "connection": conn_id,
            "message": f"Connection '{conn_id}' stored successfully"}


class AppleOperationRequest(BaseModel):
    operation: str
    args: Dict[str, Any] = Field(default_factory=dict)


class EmailOperationRequest(BaseModel):
    operation: str
    args: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/vault/apple/{conn_id}")
async def apple_operation(conn_id: str, request: Request, body: AppleOperationRequest):
    agent_name = require_agent(request)
    cid = normalize_id(conn_id)
    if not store.has_grant(agent_name, cid):
        audit_log(agent_name, cid, "apple_denied", "No grant")
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' does not have access to '{cid}'")
    conn = store.get(cid)
    if not conn or conn.get("service") != "apple":
        raise HTTPException(status_code=404, detail="Apple connection not found")
    secrets_d = store.get_secrets(cid)
    try:
        result = await run_apple_operation(
            secrets_d.get("apple_id", ""), secrets_d.get("app_password", ""),
            body.operation, body.args or {})
    except AppleOpsError as exc:
        audit_log(agent_name, cid, "apple_error", str(exc)[:200])
        raise HTTPException(status_code=502, detail=str(exc))
    audit_log(agent_name, cid, "apple_operation", body.operation[:100])
    return {"ok": True, "operation": body.operation, "result": result}


class GoogleOperationRequest(BaseModel):
    product: str
    operation: str
    args: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/vault/google/{conn_id}")
async def google_operation(conn_id: str, request: Request, body: GoogleOperationRequest):
    """Structured per-product Google Workspace operations.

    Body: {"product": "sheets|gmail|drive|docs|slides|forms|tasks|chat|people|calendar",
           "operation": "...", "args": {...}}
    The vault attaches the connection's OAuth token itself — agents never
    see it. All requests are pinned to the product's googleapis host.
    """
    agent_name = require_agent(request)
    cid = normalize_id(conn_id)
    if not store.has_grant(agent_name, cid):
        audit_log(agent_name, cid, "google_denied", "No grant")
        raise HTTPException(status_code=403,
                            detail=f"Agent '{agent_name}' does not have access to '{cid}'")
    conn = store.get(cid)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection '{cid}' not found")
    if conn.get("service") != "google" or (conn.get("auth") or {}).get("kind") != "oauth2":
        raise HTTPException(
            status_code=409,
            detail=f"'{cid}' is not a Google OAuth connection — use the HTTP proxy instead.")

    try:
        spec = google_ops.build_request(body.product, body.operation, body.args)
    except google_ops.GoogleOpsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    host = httpx.URL(spec["url"]).host
    if host not in store.allowed_hosts(conn):
        audit_log(agent_name, cid, "google_blocked_host", host or "?")
        raise HTTPException(status_code=403,
                            detail=f"Host '{host}' is not allowed for connection '{cid}'")

    secrets_d = store.get_secrets(cid)
    scrub_values = [v for v in secrets_d.values() if isinstance(v, str)]
    try:
        access_token, _ = await oauth_mod.get_valid_access_token(
            conn["service"], cid, store, conn=conn)
        scrub_values.append(access_token)
    except OAuthError as exc:
        audit_log(agent_name, cid, "google_auth_error", str(exc)[:200])
        raise HTTPException(status_code=409, detail=str(exc))

    kwargs: Dict[str, Any] = {
        "headers": {"Authorization": f"Bearer {access_token}"},
        "params": spec["params"] or None,
        "timeout": 60,
    }
    if spec["json"] is not None:
        kwargs["json"] = spec["json"]
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            upstream = await client.request(spec["method"], spec["url"], **kwargs)
    except httpx.RequestError as exc:
        audit_log(agent_name, cid, "google_upstream_error", str(exc)[:200])
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

    raw = upstream.content[:PROXY_BODY_MAX]
    truncated = len(upstream.content) > PROXY_BODY_MAX
    content_type = upstream.headers.get("content-type", "")

    def _scrub(text: str) -> str:
        for sv in scrub_values:
            if len(sv) >= 8 and sv in text:
                text = text.replace(sv, "***vault***")
        return text

    result: Dict[str, Any] = {
        "ok": upstream.status_code < 400,
        "status": upstream.status_code,
        "product": body.product,
        "operation": body.operation,
        "truncated": truncated,
    }
    if "application/json" in content_type:
        try:
            result["result"] = json.loads(_scrub(raw.decode(upstream.encoding or "utf-8", "replace")))
        except ValueError:
            result["text"] = _scrub(raw.decode(upstream.encoding or "utf-8", "replace"))
    elif content_type.startswith("text/") or "xml" in content_type or not raw:
        result["text"] = _scrub(raw.decode(upstream.encoding or "utf-8", "replace"))
    else:
        import base64 as _b64
        result["body_base64"] = _b64.b64encode(raw).decode()

    audit_log(agent_name, cid, "google_operation",
              f"{body.product}.{body.operation} -> {upstream.status_code}")
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# Agent API — the proxy (the whole point)
# ---------------------------------------------------------------------------
@app.post("/api/vault/proxy/{conn_id}")
async def proxy_request(conn_id: str, request: Request):
    agent_name = require_agent(request)
    cid = normalize_id(conn_id)

    if not store.has_grant(agent_name, cid):
        audit_log(agent_name, cid, "proxy_denied", "No grant")
        raise HTTPException(status_code=403, detail=f"Agent '{agent_name}' does not have access to '{cid}'")

    conn = store.get(cid)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection '{cid}' not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    method = str(body.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise HTTPException(status_code=400, detail=f"Unsupported method: {method}")

    if (conn.get("auth") or {}).get("kind") == "email":
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{cid}' is an email (IMAP/SMTP) connection — use "
                f"POST /api/vault/email/{cid} with "
                '{"action": "folders|list|read|send", ...} instead of the HTTP proxy.'
            ),
        )

    path = str(body.get("path") or "/")
    base_url = (conn.get("base_url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=409, detail="Connection has no base URL configured — fix it in the vault dashboard")

    # Resolve target URL. Absolute URLs allowed only for allowlisted hosts.
    allowed = store.allowed_hosts(conn)
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    else:
        if not path.startswith("/"):
            path = "/" + path
        url = base_url + path
    host = httpx.URL(url).host
    if host not in allowed:
        audit_log(agent_name, cid, "proxy_blocked_host", host or "?")
        raise HTTPException(status_code=403, detail=f"Host '{host}' is not allowed for connection '{cid}'")
    if httpx.URL(url).scheme != "https":
        raise HTTPException(status_code=403, detail="Only https upstream URLs are allowed")

    # Caller headers minus anything auth-ish; vault injects the real credential.
    headers = {
        k: v for k, v in (body.get("headers") or {}).items()
        if isinstance(k, str) and k.lower() not in _BLOCKED_REQUEST_HEADERS
    }

    secrets_d = store.get_secrets(cid)
    auth_kind = (conn.get("auth") or {}).get("kind")
    # Everything actually injected upstream must be scrubbed from responses —
    # including a freshly-refreshed OAuth token that isn't in secrets_d yet.
    scrub_values = [v for v in secrets_d.values() if isinstance(v, str)]
    if isinstance(secrets_d.get("extra_headers"), dict):
        scrub_values.extend(
            str(v) for v in secrets_d["extra_headers"].values() if v)
    try:
        if auth_kind == "oauth2":
            access_token, _ = await oauth_mod.get_valid_access_token(
                conn["service"], cid, store, conn=conn)
            scrub_values.append(access_token)
            injected = build_auth(conn, secrets_d, access_token=access_token)
        else:
            injected = build_auth(conn, secrets_d)
    except (AuthInjectionError, OAuthError) as exc:
        audit_log(agent_name, cid, "proxy_auth_error", str(exc)[:200])
        raise HTTPException(status_code=409, detail=str(exc))

    headers.update(injected["headers"])
    params = dict(body.get("params") or {})
    params.update(injected["params"])

    timeout = min(float(body.get("timeout") or 30), PROXY_TIMEOUT_MAX)
    kwargs: Dict[str, Any] = {"headers": headers, "params": params or None, "timeout": timeout}
    if body.get("json") is not None:
        kwargs["json"] = body["json"]
    elif body.get("data") is not None:
        kwargs["content"] = str(body["data"]).encode()

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            upstream = await client.request(method, url, **kwargs)
    except httpx.RequestError as exc:
        audit_log(agent_name, cid, "proxy_upstream_error", str(exc)[:200])
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

    raw = upstream.content[:PROXY_BODY_MAX]
    truncated = len(upstream.content) > PROXY_BODY_MAX
    content_type = upstream.headers.get("content-type", "")

    # Never echo credentials back, even if the upstream reflects them.
    def _scrub(text: str) -> str:
        for sv in scrub_values:
            if len(sv) >= 8 and sv in text:
                text = text.replace(sv, "***vault***")
        return text

    result: Dict[str, Any] = {
        "status": upstream.status_code,
        "content_type": content_type,
        "truncated": truncated,
    }
    if "application/json" in content_type:
        try:
            result["json"] = json.loads(_scrub(raw.decode(upstream.encoding or "utf-8", "replace")))
        except ValueError:
            result["text"] = _scrub(raw.decode(upstream.encoding or "utf-8", "replace"))
    elif content_type.startswith("text/") or "xml" in content_type or not raw:
        result["text"] = _scrub(raw.decode(upstream.encoding or "utf-8", "replace"))
    else:
        import base64
        result["body_base64"] = base64.b64encode(raw).decode()

    audit_log(agent_name, cid, "proxy_call",
              f"{method} {httpx.URL(url).path} -> {upstream.status_code}")
    return JSONResponse(result, status_code=200)


@app.post("/api/vault/email/{conn_id}")
async def email_request(conn_id: str, request: Request):
    """IMAP/SMTP actions for email-kind connections (agents never see the
    password — the vault talks to the mail servers itself).

    Body: {"action": "folders" | "list" | "read" | "send", ...}
    """
    agent_name = require_agent(request)
    cid = normalize_id(conn_id)

    if not store.has_grant(agent_name, cid):
        audit_log(agent_name, cid, "email_denied", "No grant")
        raise HTTPException(status_code=403,
                            detail=f"Agent '{agent_name}' does not have access to '{cid}'")
    conn = store.get(cid)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Connection '{cid}' not found")
    if (conn.get("auth") or {}).get("kind") != "email":
        raise HTTPException(
            status_code=409,
            detail=f"'{cid}' is not an email connection — use the HTTP proxy instead.")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    action = str(body.get("action") or "").strip().lower()

    secrets_d = store.get_secrets(cid)
    if not secrets_d.get("password"):
        raise HTTPException(status_code=409,
                            detail="No mailbox password stored — fix this connection in the dashboard")

    import asyncio
    try:
        result = await asyncio.to_thread(email_ops.run_action, action, secrets_d, body)
    except email_ops.EmailOpError as exc:
        audit_log(agent_name, cid, f"email_{action or 'unknown'}_error", str(exc)[:200])
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("Email action failed for %s", cid)
        audit_log(agent_name, cid, f"email_{action or 'unknown'}_error", str(exc)[:200])
        raise HTTPException(status_code=502, detail=f"Email operation failed: {exc}")

    detail = {"list": f"folder={body.get('folder') or 'INBOX'}",
              "search": ("filters=" + ",".join(sorted(
                  k for k in ("from", "to", "cc", "subject", "text", "query",
                              "since", "before", "last_days", "unseen",
                              "unseen_only", "flagged", "has_attachment",
                              "attachment_name", "min_size_kb", "max_size_kb")
                  if body.get(k) not in (None, "", False)))),
              "read": f"uid={body.get('uid')}",
              "attachment": f"uid={body.get('uid')} file={body.get('filename') or body.get('index')}",
              "send": f"to={result.get('to')}"}.get(action, "")
    audit_log(agent_name, cid, f"email_{action}", detail)
    # Belt-and-braces scrub (the password should never appear in results).
    pw = secrets_d.get("password", "")
    text = json.dumps(result, ensure_ascii=False, default=str)
    if pw and len(pw) >= 8 and pw in text:
        text = text.replace(pw, "***vault***")
        result = json.loads(text)
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# Health & startup
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# JSON admin API — used by the fleet dashboard (Lexi) to manage connections
# and grants remotely. Auth: admin session cookie OR X-Vault-Admin-Token.
# ---------------------------------------------------------------------------
@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    require_admin_api(request)
    conns = [(_conn_view(c)) for cid in store.list_ids() if (c := store.get(cid))]
    grants = {
        agent: [cid for cid in store.list_ids() if store.has_grant(agent, cid)]
        for agent in AGENT_NAMES
    }
    catalog_view = {
        key: {
            "label": tpl.get("label", key),
            "auth_kind": tpl["auth"]["kind"],
            "setup_help": tpl.get("setup_help", ""),
            "fields": tpl.get("fields", []),
            "base_url": tpl.get("base_url", ""),
            "scopes": (tpl.get("oauth") or {}).get("scopes", []),
        }
        for key, tpl in CATALOG.items()
    }
    return {
        "connections": conns,
        "agents": AGENT_NAMES,
        "grants": grants,
        "catalog": catalog_view,
        "redirect_uri": oauth_mod.redirect_uri(PUBLIC_URL) if PUBLIC_URL else "",
        "public_url_missing": not PUBLIC_URL,
    }


def _form_like(body: Dict[str, Any]):
    """Adapter so JSON bodies flow through the same form-parsing helpers."""
    class _D(dict):
        def get(self, k, d=None):
            v = super().get(k, d)
            return v if v is None else str(v)
    return _D(body or {})


@app.post("/api/admin/connections")
async def admin_add_connection(request: Request):
    require_admin_api(request)
    _require_json_content_type(request)
    body = await request.json()
    form = _form_like(body)

    service = (form.get("service") or "custom").strip().lower()
    tpl = get_template(service)
    if not tpl:
        raise HTTPException(status_code=400, detail="Unknown service")
    name = form.get("name") or service
    conn_id = normalize_id(name)
    if not conn_id:
        raise HTTPException(status_code=400, detail="Name required")
    if store.get(conn_id):
        raise HTTPException(status_code=409, detail=f"A connection named '{conn_id}' already exists")

    auth = dict(tpl["auth"])
    base_url = (form.get("base_url") or tpl.get("base_url") or "").strip()
    if auth["kind"] == "header":
        if form.get("header_name"):
            auth["header_name"] = form.get("header_name").strip()
        if form.get("prefix") is not None and service == "custom":
            auth["prefix"] = form.get("prefix")

    secrets_d: Dict[str, Any] = {}
    status = "ready"
    if auth["kind"] == "oauth2":
        client_id = (form.get("client_id") or "").strip()
        client_secret = (form.get("client_secret") or "").strip()
        if not client_id or not client_secret:
            raise HTTPException(status_code=400,
                                detail="Client ID and secret are required for OAuth services")
        if tpl.get("custom_oauth"):
            oauth_cfg, err = _parse_custom_oauth_form(form)
            if err:
                raise HTTPException(status_code=400, detail=err)
            if not base_url:
                raise HTTPException(status_code=400, detail="Base URL required")
            auth["oauth"] = oauth_cfg
        secrets_d = {"client_id": client_id, "client_secret": client_secret}
        status = "needs_login"
    elif auth["kind"] == "email":
        secrets_d, err = _parse_email_form(form)
        if err:
            raise HTTPException(status_code=400, detail=err)
        base_url = ""
    else:
        api_key = (form.get("api_key") or "").strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="API key required")
        if not base_url:
            raise HTTPException(status_code=400, detail="Base URL required")
        secrets_d = {"api_key": api_key}
        public_key = (form.get("public_key") or "").strip()
        if public_key:
            secrets_d["public_key"] = public_key
        extra_headers, hdr_err = _parse_extra_headers(body)
        if hdr_err:
            raise HTTPException(status_code=400, detail=hdr_err)
        if extra_headers:
            secrets_d["extra_headers"] = extra_headers

    store.save(
        conn_id, service=service, label=form.get("label") or tpl["label"],
        base_url=base_url, auth=auth, secrets=secrets_d,
        description=form.get("description") or "",
        skill_description=form.get("skill_description") or "",
        status=status,
    )
    audit_log("admin", conn_id, "connection_created", f"Service: {service} (via dashboard)")

    # Optional immediate grants for one or more agents.
    for agent in (body.get("grant_agents") or []):
        if agent in AGENT_NAMES:
            store.set_grant(agent, conn_id, True)
            audit_log("admin", conn_id, "grant_added", f"Granted to {agent} (via dashboard)")

    conn = store.get(conn_id)
    return {"connection": _conn_view(conn), "needs_login": status == "needs_login"}


@app.post("/api/admin/connections/{conn_id}/delete")
async def admin_delete_connection(conn_id: str, request: Request):
    require_admin_api(request)
    cid = normalize_id(conn_id)
    if not store.get(cid):
        raise HTTPException(status_code=404, detail="Connection not found")
    store.delete(cid, AGENT_NAMES)
    audit_log("admin", cid, "connection_deleted", "(via dashboard)")
    return {"ok": True}


@app.post("/api/admin/connections/{conn_id}/connect-link")
async def admin_connect_link(conn_id: str, request: Request):
    """Return a provider authorize URL for an OAuth connection.

    The dashboard opens this URL in the user's browser; the callback is
    validated by the single-use OAuth state, so no vault session is needed.
    """
    require_admin_api(request)
    conn = store.get(normalize_id(conn_id))
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if (conn.get("auth") or {}).get("kind") != "oauth2":
        raise HTTPException(status_code=400, detail="Not an OAuth service")
    if not PUBLIC_URL:
        raise HTTPException(status_code=503,
                            detail="Set VAULT_PUBLIC_URL on the vault service to enable OAuth logins")
    secrets_d = store.get_secrets(conn["id"])
    if not secrets_d.get("client_id"):
        raise HTTPException(status_code=400, detail="Add the OAuth client ID first")
    try:
        url = oauth_mod.build_authorize_url(
            conn["service"], conn["id"], secrets_d["client_id"], PUBLIC_URL, store,
            conn=conn)
    except OAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"url": url, "redirect_uri": oauth_mod.redirect_uri(PUBLIC_URL)}


@app.post("/api/admin/grants")
async def admin_set_grant(request: Request):
    require_admin_api(request)
    _require_json_content_type(request)
    body = await request.json()
    agent = (body.get("agent") or "").strip().lower()
    cid = normalize_id(body.get("conn_id") or "")
    granted = bool(body.get("granted"))
    if agent not in AGENT_NAMES:
        raise HTTPException(status_code=400, detail="Unknown agent")
    if not store.get(cid):
        raise HTTPException(status_code=404, detail="Connection not found")
    if store.set_grant(agent, cid, granted):
        audit_log("admin", cid,
                  "grant_added" if granted else "grant_removed",
                  f"{'Granted to' if granted else 'Revoked from'} {agent} (via dashboard)")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Replit MCP bridge — approve a dev request in Discord and a Replit Agent
# run starts automatically on the Open Manus project.
# ---------------------------------------------------------------------------
replit_mcp = replit_mcp_mod.ReplitMCP(r, encrypt_value, decrypt_value, PUBLIC_URL)


@app.get("/api/admin/replit-mcp/status")
async def replit_mcp_status(request: Request):
    require_admin_api(request)
    return {"connected": replit_mcp.connected(),
            "target_repl": replit_mcp.target_repl(),
            "redirect_uri": replit_mcp.redirect_uri}


@app.get("/admin/replit-mcp/connect")
async def replit_mcp_connect(request: Request):
    """Browser entry point: redirects the logged-in admin to Replit's
    OAuth consent screen. One-time setup for the auto-dispatch bridge."""
    if not verify_admin_api(request):
        return RedirectResponse(url="/login?next=/admin/replit-mcp/connect",
                                status_code=303)
    try:
        url = await replit_mcp.build_authorize_url()
    except ReplitMCPError as exc:
        return HTMLResponse(f"<h3>Could not start Replit login</h3><p>{exc}</p>",
                            status_code=502)
    return RedirectResponse(url=url, status_code=303)


@app.get("/oauth/replit-mcp/callback")
async def replit_mcp_callback(code: str = "", state: str = "", error: str = ""):
    # Like /oauth/callback: the single-use server-generated state token IS
    # the authorization (it only exists if an admin started the flow).
    # No request-controlled text is ever reflected into the HTML.
    import html as _html
    if error:
        # Consume the state (if any) so the link can't be replayed, then show
        # a fixed message — the provider's error text is only logged.
        if state:
            replit_mcp._consume_pkce_state(state)
        logger.warning("Replit MCP OAuth denied: %s", error[:200])
        return HTMLResponse("<h3>Replit login was denied or cancelled.</h3>"
                            "<p>You can close this tab and try again from the "
                            "dashboard.</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h3>Missing code/state.</h3>", status_code=400)
    try:
        await replit_mcp.handle_callback(code, state)
    except ReplitMCPError as exc:
        audit_log("admin", "REPLIT_MCP", "replit_mcp_connect_failed", str(exc)[:200])
        return HTMLResponse("<h3>Replit connection failed</h3><p>"
                            f"{_html.escape(str(exc)[:300])}</p>",
                            status_code=502)
    audit_log("admin", "REPLIT_MCP", "replit_mcp_connected", "")
    return HTMLResponse(
        "<h3>Replit connected ✅</h3>"
        "<p>Approved dev requests will now start Replit Agent runs "
        "automatically. You can close this tab.</p>")


@app.post("/api/admin/replit-mcp/dispatch/{req_id}")
async def replit_mcp_dispatch(req_id: str, request: Request):
    """Manually (re-)queue an approved dev request for dispatch."""
    require_admin_api(request)
    rid = re.sub(r"[^0-9A-Za-z_-]", "", req_id)[:32]
    raw = r.get(f"devreq:item:{rid}") if rid else None
    if not raw:
        raise HTTPException(status_code=404, detail=f"Dev request '{rid}' not found")
    try:
        item = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=500, detail="Corrupt request record")
    # Only owner-approved requests may be dispatched — the Discord approval
    # is the trust boundary; this endpoint is retry-only.
    if item.get("status") != "approved":
        raise HTTPException(status_code=409,
                            detail=f"Request '{rid}' is {item.get('status')!r}, "
                                   "not approved — approve it in Discord first")
    r.lpush(replit_mcp_mod.DISPATCH_QUEUE, rid)
    audit_log("admin", "REPLIT_MCP", "replit_mcp_requeued", f"request={rid}")
    return {"ok": True, "queued": rid}


@app.get("/health")
async def health():
    try:
        r.ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception as e:
        return JSONResponse({"status": "unhealthy", "error": str(e)}, status_code=500)


@app.on_event("startup")
async def startup():
    await init_agents()
    migrated = store.migrate_legacy_keys()
    logger.info("Vault started. %d agents, %d legacy keys migrated, %d connections.",
                len(AGENT_NAMES), migrated, len(store.list_ids()))
    if not os.getenv("VAULT_MASTER_KEY", "").strip():
        logger.warning("VAULT_MASTER_KEY is not set — encryption key would not "
                       "survive a Redis wipe. Set it on this service.")
    # Nightly backups of all vault:* keys to local disk (Railway volume).
    import asyncio
    asyncio.create_task(vault_backup.backup_loop(r))
    # Auto-dispatch approved dev requests to Replit Agent (MCP bridge).
    asyncio.create_task(replit_mcp_mod.dispatch_loop(replit_mcp))


# ---------------------------------------------------------------------------
# Backups (admin-only)
# ---------------------------------------------------------------------------
def _require_json_content_type(request: Request):
    """CSRF guard for cookie-authed mutation endpoints: cross-site forms
    cannot send application/json without a CORS preflight (which we reject)."""
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ct != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")


@app.post("/api/admin/backup")
async def admin_backup_now(request: Request):
    if not verify_admin_session(request):
        raise HTTPException(status_code=401, detail="Admin session required")
    _require_json_content_type(request)
    import asyncio
    result = await asyncio.to_thread(vault_backup.backup_now, r)
    audit_log("admin", result["file"], "backup_created", f"{result['keys']} keys")
    return result


@app.get("/api/admin/backups")
async def admin_list_backups(request: Request):
    if not verify_admin_session(request):
        raise HTTPException(status_code=401, detail="Admin session required")
    return {"backups": vault_backup.list_backups()}


@app.post("/api/admin/restore")
async def admin_restore(request: Request):
    if not verify_admin_session(request):
        raise HTTPException(status_code=401, detail="Admin session required")
    _require_json_content_type(request)
    body = await request.json()
    filename = body.get("file", "")
    overwrite = bool(body.get("overwrite", False))
    import asyncio
    try:
        result = await asyncio.to_thread(
            vault_backup.restore_from_file, r, filename, overwrite=overwrite)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup file not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit_log("admin", filename, "backup_restored",
              f"{result['restored']} restored, {result['skipped_existing']} skipped")
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=VAULT_PORT)
