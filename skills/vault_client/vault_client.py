#!/usr/bin/env python3
"""
Vault Client — Zero-exposure API access for Open Manus agents.

The vault holds every credential (API keys and OAuth logins). Your code
NEVER receives a key — instead you send the request through the vault
proxy, which attaches the credential server-side and returns the API
response.

Usage:
    # In Python code:
    from vault_client import vault

    # See which services you can use:
    conns = vault.list_connections()

    # Call an API through the vault (credential injected server-side):
    resp = vault.request("OPENAI", "POST", "/v1/chat/completions", json={
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    })
    print(resp["status"], resp["json"])

    # Or from the command line:
    python3 /app/skills/vault_client/vault_client.py list
    python3 /app/skills/vault_client/vault_client.py call ELEVENLABS GET /v1/voices
    python3 /app/skills/vault_client/vault_client.py call OPENAI POST /v1/chat/completions --json '{"model": "gpt-4o-mini", "messages": [...]}'
    python3 /app/skills/vault_client/vault_client.py skill GOOGLE
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VAULT_URL = os.getenv("VAULT_URL", "http://vault.railway.internal:8080")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")


class VaultError(Exception):
    """Raised when vault operations fail."""
    pass


class VaultClient:
    """Client for the Open Manus Key Vault (proxy-only)."""

    def __init__(self, url: str = None, token: str = None):
        self.url = (url or VAULT_URL).rstrip("/")
        self.token = token or VAULT_TOKEN

    # -- internals ----------------------------------------------------------

    def _http(self, method: str, path: str, payload: Optional[dict] = None,
              timeout: int = 60) -> dict:
        if not self.token:
            raise VaultError(
                "VAULT_TOKEN not set. Cannot authenticate with the vault. "
                "Ask Harmony to set up your vault access."
            )
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            f"{self.url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise VaultError(f"Vault error ({e.code}): {detail}")
        except URLError as e:
            raise VaultError(f"Cannot reach vault at {self.url}: {e.reason}")

    # -- public API -----------------------------------------------------------

    def request(self, connection: str, method: str, path: str, *,
                params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None,
                json: Optional[Any] = None,
                data: Optional[str] = None,
                timeout: float = 30) -> Dict[str, Any]:
        """Call an upstream API through the vault proxy.

        Args:
            connection: connection name from list_connections() (e.g. "OPENAI")
            method: HTTP method for the upstream API
            path: upstream path (e.g. "/v1/voices"); relative to the
                connection's base URL
            params/headers/json/data: forwarded to the upstream API
                (auth headers are set by the vault — you can't override them)
            timeout: upstream timeout in seconds (max 120)

        Returns:
            {"status": int, "content_type": str, and one of
             "json" | "text" | "body_base64"}
        """
        payload: Dict[str, Any] = {"method": method, "path": path, "timeout": timeout}
        if params:
            payload["params"] = params
        if headers:
            payload["headers"] = headers
        if json is not None:
            payload["json"] = json
        if data is not None:
            payload["data"] = data
        return self._http(
            "POST", f"/api/vault/proxy/{connection.strip().upper()}",
            payload, timeout=int(timeout) + 15,
        )

    def google(self, connection: str, product: str, operation: str,
               **kwargs: Any) -> Dict[str, Any]:
        """Structured Google Workspace call through the vault.

        Args:
            connection: the Google connection name (e.g. "GOOGLE_LEXI")
            product: one of gmail, drive, sheets, docs, slides, forms,
                tasks, chat, people, calendar
            operation: product operation, e.g. sheets: get/update/append,
                gmail: search/read/send, tasks: list/create/complete —
                or "request" (method, path, params, json) pinned to the
                product's API host.
            **kwargs: operation-specific args (sent as the "args" object)

        Returns: {"ok": bool, "status": int, "result": ... }
        The OAuth token stays inside the vault.
        """
        return self._http(
            "POST", f"/api/vault/google/{connection.strip().upper()}",
            {"product": product, "operation": operation, "args": kwargs},
            timeout=95,
        )

    def email(self, connection: str, action: str, **kwargs: Any) -> Dict[str, Any]:
        """Use an email (IMAP/SMTP) connection through the vault.

        Actions (the vault talks to the mail servers — you never see the
        password):
            "folders"                       -> {"folders": [...]}
            "list"  (folder="INBOX", limit=10, unseen_only=False)
            "search" (any of: from_, to, cc, subject, text, since/before
                YYYY-MM-DD, last_days=N, unseen=True/False, flagged,
                min_size_kb, max_size_kb, has_attachment,
                attachment_name="contract" or "*.pdf", folder, limit)
                -> {"total_matches", "messages": [{uid, from, to, subject,
                    date, seen, attachments}], "more": bool}
                NOTE: pass sender filter as from_=... (Python keyword);
                it is sent as "from".
            "read"  (uid=..., folder="INBOX")
            "attachment" (uid=..., filename=... or index=0)
                -> {"attachment": {filename, content_type, size, content_b64}}
                (decode content_b64 and write it to a file yourself)
            "send"  (to=..., subject=..., body=..., cc=..., bcc=...)

        Example:
            vault.email("GMAIL_PERSONAL", "list", unseen_only=True)
            vault.email("GMAIL_PERSONAL", "send", to="a@b.com",
                        subject="Hi", body="...")
        """
        payload: Dict[str, Any] = {"action": action}
        if "from_" in kwargs:  # 'from' is a Python keyword
            kwargs["from"] = kwargs.pop("from_")
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return self._http(
            "POST", f"/api/vault/email/{connection.strip().upper()}",
            payload, timeout=75,
        )

    def resolve(self, service: str) -> Dict[str, Any]:
        """STEP-ONE CHECK: does the vault have this service, and can I use it?

        Call this as soon as a plan needs an external API. Returns
        {"found": bool, "granted": bool, "ready": bool, "next_step": str, ...}.
        Cheap and safe to call often.
        """
        from urllib.parse import quote
        return self._http("GET", f"/api/vault/resolve?service={quote(service)}")

    def request_access(self, service: str, name: str = "", reason: str = "") -> Dict[str, Any]:
        """File a setup/access request the owner resolves in the vault dashboard.

        Use when resolve() says the service is missing or you have no grant.
        Returns a "message" you should relay to the user so they know to open
        the dashboard (add a key, or complete an OAuth login).
        """
        return self._http("POST", "/api/vault/request", {
            "service": service, "name": name, "reason": reason,
        })

    def ensure(self, service: str, reason: str = "") -> Dict[str, Any]:
        """resolve() and, if not usable, automatically request_access().

        Returns {"usable": bool, "connection": str|None, "message": str}.
        If usable, call vault.request(connection, ...) right away.
        If not, relay the message to the user and continue with other work.
        """
        info = self.resolve(service)
        if info.get("found") and info.get("granted") and info.get("ready"):
            return {"usable": True, "connection": info["connection"],
                    "message": f"'{info['connection']}' is ready — use vault.request()."}
        req = self.request_access(service, reason=reason)
        return {"usable": False, "connection": info.get("connection"),
                "status": req.get("status"), "message": req.get("message", "")}

    def list_connections(self) -> List[dict]:
        """List services this agent can call through the proxy."""
        data = self._http("GET", "/api/vault/list")
        return data.get("available_connections", data.get("available_keys", []))

    # Back-compat alias
    def list_keys(self) -> List[dict]:
        return self.list_connections()

    def get_skill(self, connection: str) -> dict:
        """Get usage notes / base URL / example call for a connection."""
        return self._http("GET", f"/api/vault/skill/{connection.strip().upper()}")

    def store(self, key_name: str, key_value: str, service: str = "",
              description: str = "", skill_description: str = "",
              base_url: str = "") -> dict:
        """Store a new API-key connection (restricted agents only)."""
        return self._http("POST", "/api/vault/store", {
            "key_name": key_name,
            "key_value": key_value,
            "service": service,
            "description": description,
            "skill_description": skill_description,
            "base_url": base_url,
        })

    # -- removed operations (fail loudly with guidance) -------------------------

    def get(self, key_name: str, use_cache: bool = True) -> str:
        raise VaultError(
            "Raw key fetch has been removed — the vault is proxy-only now. "
            f"Use vault.request({key_name!r}, 'GET', '/...') instead; the vault "
            "attaches the credential for you. Run vault.list_connections() to "
            "see what you can call."
        )

    def export_env(self) -> Dict[str, str]:
        raise VaultError(
            "export_env() has been removed — keys are never handed to agents "
            "anymore. Route API calls through vault.request(connection, method, "
            "path, ...) instead."
        )

    def clear_cache(self):
        pass  # no key cache anymore


# Singleton instance for easy import
vault = VaultClient()


def _print_response(resp: Dict[str, Any]) -> None:
    print(f"HTTP {resp.get('status')}  ({resp.get('content_type', '')})")
    if "json" in resp:
        print(json.dumps(resp["json"], indent=2)[:20000])
    elif "text" in resp:
        print(resp["text"][:20000])
    elif "body_base64" in resp:
        print(f"<binary body, {len(resp['body_base64'])} base64 chars — "
              "decode resp['body_base64'] to use it>")
    if resp.get("truncated"):
        print("[response truncated by vault]")


def main():
    """CLI interface for the vault client."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  vault_client.py list                          — List your connections")
        print("  vault_client.py call CONN METHOD PATH [opts]  — Call an API via the vault")
        print("      opts: --json '{...}'   request body")
        print("            --param k=v      query param (repeatable)")
        print("            --header k=v     extra header (repeatable)")
        print("  vault_client.py resolve SERVICE               — Step-one check: is it in the vault?")
        print("  vault_client.py ensure SERVICE [REASON]       — Check + auto-file a setup request")
        print("  vault_client.py skill CONN                    — Usage notes for a connection")
        print("  vault_client.py store NAME VALUE [--service SVC] [--desc D] [--base-url URL]")
        sys.exit(1)

    command = sys.argv[1].lower()

    try:
        if command == "list":
            conns = vault.list_connections()
            if not conns:
                print("No connections available. Ask Harmony to grant you access.")
                return
            print(f"Available connections ({len(conns)}):")
            print("-" * 60)
            for c in conns:
                print(f"  {c.get('id') or c.get('key_name')}")
                if c.get("label"):
                    print(f"    Service: {c['label']}")
                if c.get("base_url"):
                    print(f"    Base URL: {c['base_url']}")
                if c.get("example_call"):
                    print(f"    Example: {c['example_call']}")
                if c.get("description"):
                    print(f"    Description: {c['description']}")
                print()

        elif command == "call":
            if len(sys.argv) < 5:
                print("Usage: vault_client.py call CONN METHOD PATH [--json '{...}'] [--param k=v] [--header k=v]")
                sys.exit(1)
            conn, method, path = sys.argv[2], sys.argv[3].upper(), sys.argv[4]
            body = None
            params: Dict[str, str] = {}
            headers: Dict[str, str] = {}
            i = 5
            while i < len(sys.argv):
                arg = sys.argv[i]
                if arg == "--json" and i + 1 < len(sys.argv):
                    body = json.loads(sys.argv[i + 1]); i += 2
                elif arg == "--param" and i + 1 < len(sys.argv):
                    k, _, v = sys.argv[i + 1].partition("="); params[k] = v; i += 2
                elif arg == "--header" and i + 1 < len(sys.argv):
                    k, _, v = sys.argv[i + 1].partition("="); headers[k] = v; i += 2
                else:
                    i += 1
            resp = vault.request(conn, method, path, params=params or None,
                                 headers=headers or None, json=body)
            _print_response(resp)

        elif command == "resolve":
            if len(sys.argv) < 3:
                print("Usage: vault_client.py resolve SERVICE")
                sys.exit(1)
            print(json.dumps(vault.resolve(sys.argv[2]), indent=2))

        elif command == "ensure":
            if len(sys.argv) < 3:
                print("Usage: vault_client.py ensure SERVICE [REASON]")
                sys.exit(1)
            reason = " ".join(sys.argv[3:])
            out = vault.ensure(sys.argv[2], reason=reason)
            print(json.dumps(out, indent=2))
            if not out["usable"]:
                sys.exit(2)  # distinct exit code: not usable yet, request filed

        elif command == "skill":
            if len(sys.argv) < 3:
                print("ERROR: Connection name required. Usage: vault_client.py skill GOOGLE")
                sys.exit(1)
            info = vault.get_skill(sys.argv[2])
            print(f"Connection: {info.get('connection') or info.get('key_name')}")
            print(f"Service: {info.get('service', '—')}")
            print(f"Base URL: {info.get('base_url', '—')}")
            print(f"Example: {info.get('example_call', '—')}")
            print(f"Description: {info.get('description', '—')}")
            if info.get("skill_description"):
                print("\nSkill / Tool Usage Guide:")
                print("-" * 40)
                print(info["skill_description"])

        elif command == "store":
            if len(sys.argv) < 4:
                print("Usage: vault_client.py store NAME VALUE [--service SVC] [--desc D] [--base-url URL]")
                sys.exit(1)
            name, value = sys.argv[2].upper(), sys.argv[3]
            service = description = skill_desc = base_url = ""
            i = 4
            while i < len(sys.argv):
                if sys.argv[i] == "--service" and i + 1 < len(sys.argv):
                    service = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--desc" and i + 1 < len(sys.argv):
                    description = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--skill" and i + 1 < len(sys.argv):
                    skill_desc = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--base-url" and i + 1 < len(sys.argv):
                    base_url = sys.argv[i + 1]; i += 2
                else:
                    i += 1
            result = vault.store(name, value, service, description, skill_desc, base_url)
            print(f"✅ {result['message']}")

        elif command in ("get", "export"):
            print("ERROR: Raw key access has been removed — the vault is proxy-only.", file=sys.stderr)
            print("Use: vault_client.py call CONN METHOD PATH  (run 'list' to see connections)", file=sys.stderr)
            sys.exit(1)

        else:
            print(f"Unknown command: {command}")
            print("Available commands: list, call, skill, store")
            sys.exit(1)

    except VaultError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
