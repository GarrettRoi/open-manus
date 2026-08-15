"""Tests for the browser_login tool: login-field parsing and credential
handling. These avoid launching a real browser — they exercise the pure
parsing helper and the credential-fetch/scrub wiring with mocks.
"""
import json
import sys
import types
from unittest import mock

import pytest

from tools.browser_tool import _find_login_fields


def test_find_fields_basic_email_password():
    snap = (
        '- textbox "Email or phone" [ref=e3]\n'
        '- textbox "Password" [ref=e4]\n'
        '- button "Log In" [ref=e5]\n'
    )
    fields = _find_login_fields(snap)
    assert fields["username"] == "@e3"
    assert fields["password"] == "@e4"
    assert fields["submit"] == "@e5"


def test_find_fields_at_ref_format():
    snap = (
        'textbox "Username" @e1\n'
        'textbox "Password" @e2\n'
        'button "Sign in" @e7\n'
    )
    fields = _find_login_fields(snap)
    assert fields["username"] == "@e1"
    assert fields["password"] == "@e2"
    assert fields["submit"] == "@e7"


def test_find_fields_username_fallback_to_first_text_input():
    # No email/username keyword — fall back to first non-password text input.
    snap = (
        '- textbox "" [ref=e10]\n'
        '- textbox "Password" [ref=e11]\n'
    )
    fields = _find_login_fields(snap)
    assert fields["username"] == "@e10"
    assert fields["password"] == "@e11"


def test_find_fields_no_password_returns_none():
    snap = '- textbox "Search" [ref=e1]\n- button "Go" [ref=e2]\n'
    fields = _find_login_fields(snap)
    assert fields["password"] is None


def test_browser_login_missing_connection():
    from tools.browser_tool import browser_login
    out = json.loads(browser_login("", task_id="t"))
    assert out["success"] is False
    assert "connection" in out["error"].lower()


def test_browser_login_credential_error_short_circuits():
    from tools import browser_tool
    with mock.patch("tools.vault_tools.fetch_browser_credentials",
                    return_value={"error": "no grant"}):
        out = json.loads(browser_tool.browser_login("FOO", task_id="t"))
    assert out["success"] is False
    assert out["error"] == "no grant"


def test_browser_login_never_returns_raw_credentials():
    from tools import browser_tool
    creds = {"username": "alice@example.com", "password": "TOP-SECRET-PW",
             "login_url": "https://example.com/login"}
    snap = json.dumps({
        "success": True,
        "snapshot": '- textbox "Email" [ref=e1]\n- textbox "Password" [ref=e2]\n- button "Log In" [ref=e3]',
    })
    with mock.patch("tools.vault_tools.fetch_browser_credentials", return_value=creds), \
         mock.patch.object(browser_tool, "browser_navigate", return_value=json.dumps({"success": True, "snapshot": ""})), \
         mock.patch.object(browser_tool, "browser_console", return_value=json.dumps({"success": True, "result": "https://example.com/login"})), \
         mock.patch.object(browser_tool, "browser_snapshot", return_value=snap), \
         mock.patch.object(browser_tool, "browser_type", return_value=json.dumps({"success": True})) as m_type, \
         mock.patch.object(browser_tool, "browser_click", return_value=json.dumps({"success": True})), \
         mock.patch.object(browser_tool, "browser_press", return_value=json.dumps({"success": True})), \
         mock.patch.object(browser_tool.time, "sleep", return_value=None):
        raw = browser_tool.browser_login("FACEBOOK_MAIN", task_id="t")
    # The password must never appear in the tool's return payload.
    assert "TOP-SECRET-PW" not in raw
    out = json.loads(raw)
    assert out["success"] is True
    assert out["steps"] == ["username", "password", "click_submit"]
    # The raw creds WERE handed to the browser type calls (server-side).
    typed_values = [c.args[1] for c in m_type.call_args_list]
    assert "TOP-SECRET-PW" in typed_values
