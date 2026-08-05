"""Contract tests for Apple/mail catalog entries and native tool schemas."""

from __future__ import annotations

import unittest
from pathlib import Path

from services.vault.catalog import CATALOG


class AppleCatalogTests(unittest.TestCase):
    def test_catalog_has_proxy_only_apple_and_mail_presets(self):
        apple = CATALOG["apple"]
        self.assertEqual(apple["auth"]["kind"], "apple")
        self.assertIn("caldav.icloud.com", apple["allowed_hosts"])
        self.assertIn("contacts.icloud.com", apple["allowed_hosts"])
        self.assertIn("appleid.apple.com", apple["setup_help"])
        email = CATALOG["email"]
        self.assertEqual(email["auth"]["kind"], "email")
        # Generic IMAP/SMTP preset; iCloud Mail is documented in the setup help.
        field_names = {f["name"] for f in email["fields"]}
        self.assertIn("imap_host", field_names)
        self.assertIn("smtp_host", field_names)
        self.assertIn("imap.mail.me.com", email["setup_help"])

    def test_imessage_template_is_explicitly_optional(self):
        template = CATALOG["bluebubbles"]
        self.assertEqual(template["auth"]["kind"], "header")
        self.assertIn("always-on Mac", template["setup_help"])

    def test_dashboard_uses_distinct_apple_and_email_password_fields(self):
        root = Path(__file__).parents[1]
        template = (root / "services" / "vault" / "templates" / "services.html").read_text()
        app = (root / "services" / "vault" / "app.py").read_text()
        # Apple uses its own password field; the email form uses `password`.
        # No two credential groups may share a field name inside one form.
        self.assertIn('name="apple_app_password"', template)
        self.assertNotIn('name="app_password"', template)
        self.assertIn('form.get("apple_app_password")', app)
        # The simplified iCloud-only email flow must stay deleted in favor of
        # the generic IMAP/SMTP one (duplicate route/preset regression guard).
        self.assertEqual(app.count('@app.post("/api/vault/email/{conn_id}")'), 1)
        self.assertEqual(template.count('id="grp_email"'), 1)


if __name__ == "__main__":
    unittest.main()