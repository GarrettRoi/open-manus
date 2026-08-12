"""Tool-registration contract tests for the discord_read connection kind."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def _load_vault_tools():
    spec = importlib.util.spec_from_file_location(
        "vault_tools_discord_test", _repo_root / "tools" / "vault_tools.py")
    mod = importlib.util.module_from_spec(spec)
    import tools as _t  # ensure the real package resolves for `tools.registry`
    if str(_repo_root / "tools") not in list(getattr(_t, "__path__", [])):
        sys.modules.pop("tools", None)
        sys.path.insert(0, str(_repo_root))
    spec.loader.exec_module(mod)
    return mod


class DiscordReadToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_vault_tools()
        cls.conn = {"id": "DISCORD_READ_T", "label": "Reader",
                    "service": "discord_read", "auth_kind": "header"}

    def test_schema_is_readonly_operation_hub(self):
        schema = self.mod._build_conn_schema(self.conn, "vault_discord_read_t")
        self.assertIn("parameters", schema)
        self.assertEqual(schema["parameters"]["required"], ["operation"])
        ops = set(schema["parameters"]["properties"]["operation"]["enum"])
        self.assertEqual(ops, {"list_servers", "list_channels", "read_messages",
                               "extract_links", "download_attachment"})
        # No write-style operations exposed anywhere.
        desc = schema["description"].lower()
        self.assertNotIn("send_message", str(schema))
        self.assertIn("read-only", desc)

    def test_handler_routes_to_discord_endpoint(self):
        calls = []

        def fake_call(conn_id, args):
            calls.append((conn_id, args))
            return '{"ok": true}'

        self.mod._discord_read_call = fake_call
        handler = self.mod._make_conn_handler(
            "DISCORD_READ_T", auth_kind="header", service="discord_read")
        out = handler({"operation": "list_servers"})
        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(calls, [("DISCORD_READ_T", {"operation": "list_servers"})])


if __name__ == "__main__":
    unittest.main()
