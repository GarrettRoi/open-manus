"""Contract tests for Apple/mail catalog entries and native tool schemas."""

from __future__ import annotations

import unittest

from services.vault.catalog import CATALOG


class AppleCatalogTests(unittest.TestCase):
    def test_catalog_has_proxy_only_apple_and_mail_presets(self):
        apple = CATALOG["apple"]
        self.assertEqual(apple["auth"]["kind"], "apple")
        self.assertIn("caldav.icloud.com", apple["allowed_hosts"])
        self.assertIn("contacts.icloud.com", apple["allowed_hosts"])
        self.assertIn("appleid.apple.com", apple["setup_help"])
        self.assertEqual(CATALOG["email"]["auth"]["kind"], "email")
        self.assertIn("imap.mail.me.com", CATALOG["email"]["allowed_hosts"])
        self.assertIn("smtp.mail.me.com", CATALOG["email"]["allowed_hosts"])

    def test_imessage_template_is_explicitly_optional(self):
        template = CATALOG["bluebubbles"]
        self.assertEqual(template["auth"]["kind"], "header")
        self.assertIn("always-on Mac", template["setup_help"])


if __name__ == "__main__":
    unittest.main()