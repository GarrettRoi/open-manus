"""Tests for the agent-side vault reauth action (dev request #40)."""
import json
from unittest import mock

from tools import vault_tools


def test_reauth_returns_link():
    payload = {"connection": "GMAIL_MAIN",
               "url": "https://accounts.google.com/o/oauth2/v2/auth?state=x",
               "expires_in_seconds": 600,
               "note": "Send this link to the owner in chat."}
    with mock.patch.object(vault_tools, "_vault_http", return_value=payload):
        out = json.loads(vault_tools.vault_meta_handler(
            {"action": "reauth", "connection": "gmail main"}))
    assert out["url"].startswith("https://accounts.google.com/")
    assert out["connection"] == "GMAIL_MAIN"


def test_reauth_normalizes_connection_id():
    with mock.patch.object(vault_tools, "_vault_http", return_value={}) as m:
        vault_tools.vault_meta_handler(
            {"action": "reauth", "connection": "gmail-main"})
    assert m.call_args.args[1] == "/api/vault/reauth/GMAIL_MAIN"


def test_reauth_requires_connection():
    out = json.loads(vault_tools.vault_meta_handler({"action": "reauth"}))
    assert "connection" in out["error"]


def test_structured_vault_error_passthrough_in_proxy():
    from urllib.error import HTTPError
    import io
    detail = {"error": "oauth_reauth_required", "connection": "GMAIL_MAIN",
              "message": "token expired",
              "action": "Call the vault tool with action='reauth' ..."}
    body = json.dumps({"detail": detail}).encode()
    err = HTTPError("http://vault", 409, "Conflict", {}, io.BytesIO(body))
    with mock.patch.object(vault_tools, "_vault_http", side_effect=err):
        out = json.loads(vault_tools._proxy_call("GMAIL_MAIN", {"path": "/x"}))
    assert out["error"] == "oauth_reauth_required"
    assert "reauth" in out["action"]
