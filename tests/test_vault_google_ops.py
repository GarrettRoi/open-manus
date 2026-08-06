"""Contract tests for the structured Google Workspace vault operations."""

from __future__ import annotations

import base64
import unittest

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

    def test_catalog_google_preset_covers_all_product_hosts(self):
        from services.vault.catalog import CATALOG
        allowed = set(CATALOG["google"]["allowed_hosts"])
        for product, info in PRODUCTS.items():
            self.assertIn(info["host"], allowed, f"{product} host missing from catalog")


if __name__ == "__main__":
    unittest.main()
