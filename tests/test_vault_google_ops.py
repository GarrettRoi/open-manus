"""Contract tests for the structured Google Workspace vault operations."""

from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from services.vault.google_ops import (
    OPERATIONS,
    PRODUCTS,
    GoogleOpsError,
    build_request,
)


class GoogleOpsTests(unittest.TestCase):
    def test_every_product_has_operations_and_host(self):
        self.assertEqual(set(PRODUCTS), set(OPERATIONS))
        for product, ops in OPERATIONS.items():
            self.assertIn("request", ops)
            self.assertTrue(PRODUCTS[product]["host"].endswith("googleapis.com"))

    def test_sheets_update_builds_put_with_user_entered(self):
        spec = build_request("sheets", "update", {
            "spreadsheet_id": "abc123", "range": "Sheet1!A1:B2",
            "values": [["a", "b"], ["c", "d"]]})
        self.assertEqual(spec["method"], "PUT")
        self.assertIn("/v4/spreadsheets/abc123/values/Sheet1%21A1%3AB2", spec["url"])
        self.assertEqual(spec["params"]["valueInputOption"], "USER_ENTERED")
        self.assertEqual(spec["json"]["values"][1][0], "c")

    def test_gmail_send_builds_rfc822_raw(self):
        spec = build_request("gmail", "send", {
            "to": "a@b.com", "subject": "Hi", "body": "Hello", "cc": "c@d.com"})
        self.assertTrue(spec["url"].endswith("/gmail/v1/users/me/messages/send"))
        raw = spec["json"]["raw"]
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
        self.assertIn("To: a@b.com", decoded)
        self.assertIn("Cc: c@d.com", decoded)
        self.assertIn("Hello", decoded)

    def test_tasks_complete_and_calendar_event(self):
        spec = build_request("tasks", "complete", {"task_id": "t1"})
        self.assertEqual(spec["method"], "PATCH")
        self.assertIn("/tasks/v1/lists/%40default/tasks/t1", spec["url"])
        spec = build_request("calendar", "create_event", {
            "summary": "Standup", "start": "2026-08-07T09:00:00-05:00",
            "end": "2026-08-07T09:15:00-05:00", "attendees": ["x@y.com"]})
        self.assertEqual(spec["json"]["attendees"], [{"email": "x@y.com"}])
        self.assertIn("/calendar/v3/calendars/primary/events", spec["url"])

    def test_missing_args_and_unknown_product_fail_loudly(self):
        with self.assertRaises(GoogleOpsError):
            build_request("sheets", "get", {})
        with self.assertRaises(GoogleOpsError):
            build_request("outlook", "search", {})
        with self.assertRaises(GoogleOpsError):
            build_request("gmail", "frobnicate", {})

    def test_raw_request_is_pinned_to_product_prefix(self):
        spec = build_request("drive", "request", {"method": "GET",
                                                  "path": "/drive/v3/about"})
        self.assertEqual(spec["url"], "https://www.googleapis.com/drive/v3/about")
        # Escaping the product surface must fail — even to another Google API.
        with self.assertRaises(GoogleOpsError):
            build_request("drive", "request", {"method": "GET",
                                               "path": "/oauth2/v4/token"})
        with self.assertRaises(GoogleOpsError):
            build_request("sheets", "request", {"method": "GET",
                                                "path": "/v4/../oauth2"})

    def test_path_segments_are_escaped(self):
        with self.assertRaises(GoogleOpsError):
            build_request("gmail", "read", {"id": "abc/../../evil"})
        spec = build_request("drive", "get", {"file_id": "f 1"})
        self.assertIn("/drive/v3/files/f%201", spec["url"])

    def test_raw_request_rejects_encoded_traversal(self):
        for path in ("/calendar/v3/%2e%2e/drive/v3/files",
                     "/calendar/v3/%252e%252e/oauth2",
                     "/calendar/v3/..%2foauth2",
                     "/calendar/v3/a//b"):
            with self.assertRaises(GoogleOpsError, msg=path):
                build_request("calendar", "request", {"method": "GET", "path": path})

    def test_special_resource_names_reject_traversal(self):
        with self.assertRaises(GoogleOpsError):
            build_request("chat", "messages", {"space": "spaces/../../evil"})
        with self.assertRaises(GoogleOpsError):
            build_request("people", "get", {"resource_name": "people/../me"})
        spec = build_request("chat", "send", {"space": "AAA-bb_1", "text": "hi"})
        self.assertIn("/v1/spaces/AAA-bb_1/messages", spec["url"].replace("%2D", "-"))

    def test_tool_schema_uses_parameters_contract(self):
        # Import by file path: `tests/tools/` shadows the real `tools`
        # package when unittest discovery puts the tests dir on sys.path.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vault_tools_under_test", _repo_root / "tools" / "vault_tools.py")
        mod = importlib.util.module_from_spec(spec)
        real_tools = _repo_root / "tools"
        import tools as _t  # ensure the real package resolves for `tools.registry`
        if str(real_tools) not in list(getattr(_t, "__path__", [])):
            sys.modules.pop("tools", None)
            sys.path.insert(0, str(_repo_root))
        spec.loader.exec_module(mod)
        _build_google_schema = mod._build_google_schema
        GOOGLE_PRODUCTS = mod.GOOGLE_PRODUCTS
        conn = {"id": "GOOGLE_TEST", "label": "Test"}
        for product in GOOGLE_PRODUCTS:
            schema = _build_google_schema(conn, f"vault_google_test_{product}", product)
            self.assertIn("parameters", schema, product)
            self.assertNotIn("input_schema", schema, product)
            self.assertEqual(schema["parameters"]["required"], ["operation"])

    def test_catalog_google_preset_covers_all_product_hosts(self):
        from services.vault.catalog import CATALOG
        allowed = set(CATALOG["google"]["allowed_hosts"])
        for product, info in PRODUCTS.items():
            self.assertIn(info["host"], allowed, f"{product} host missing from catalog")


if __name__ == "__main__":
    unittest.main()
