"""Vault-routed FAL image generation (devreq #44).

Covers:
- routing selection: vault connection wins over direct/managed paths
- the queue handle contract (submit -> poll -> result)
- leak detection: the FAL credential never appears in anything the agent
  process sends or receives on the vault-routed path
"""

import json

import pytest

import tools.image_generation_tool as image_tool
from tools.fal_common import VaultFalRequestHandle, submit_fal_via_vault

FAKE_FAL_KEY = "falkey-super-secret-1234567890"


class RecordingVaultHttp:
    """Stands in for tools.vault_tools._vault_http.

    Simulates the vault proxy contract: caller sends {method, path, json,
    headers}; the vault injects the stored key server-side and scrubs it
    from responses. Records every payload so tests can assert no credential
    material ever crosses the agent boundary.
    """

    def __init__(self, status_sequence=("COMPLETED",)):
        self.calls = []
        self._status_sequence = list(status_sequence)

    def __call__(self, method, path, payload=None, timeout=None):
        self.calls.append({"method": method, "path": path, "payload": payload})
        assert path.startswith("/api/vault/proxy/FAL_MAIN")
        inner = payload or {}
        if inner.get("method") == "POST":
            return {
                "status": 200,
                "content_type": "application/json",
                "json": {
                    "request_id": "req-1",
                    "status_url": "https://queue.fal.run/fal-ai/x/requests/req-1/status",
                    "response_url": "https://queue.fal.run/fal-ai/x/requests/req-1",
                },
            }
        if str(inner.get("path", "")).endswith("/status"):
            state = self._status_sequence.pop(0)
            return {"status": 200, "json": {"status": state}}
        # Result fetch — the vault scrubs the stored key before the agent
        # ever sees the body; emulate that contract.
        return {
            "status": 200,
            "content_type": "application/json",
            "json": {
                "images": [{"url": "https://v3.fal.media/files/out.png"}],
                "reflected_auth": "***vault***",
            },
        }


@pytest.fixture
def vault_env(monkeypatch):
    monkeypatch.setenv("VAULT_FAL_CONNECTION", "fal-main")
    monkeypatch.setenv("VAULT_TOKEN", "agent-vault-token")
    monkeypatch.delenv("FAL_KEY", raising=False)


class TestResolveVaultFalConnection:
    def test_requires_both_env_vars(self, monkeypatch):
        monkeypatch.delenv("VAULT_FAL_CONNECTION", raising=False)
        monkeypatch.setenv("VAULT_TOKEN", "t")
        assert image_tool._resolve_vault_fal_connection() is None
        monkeypatch.setenv("VAULT_FAL_CONNECTION", "fal-main")
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        assert image_tool._resolve_vault_fal_connection() is None

    def test_normalizes_connection_id(self, vault_env):
        assert image_tool._resolve_vault_fal_connection() == "FAL_MAIN"


class TestVaultRouting:
    def test_submit_prefers_vault_over_direct_and_managed(self, vault_env, monkeypatch):
        http = RecordingVaultHttp()
        monkeypatch.setattr("tools.vault_tools._vault_http", http)
        # Direct key present AND managed gateway resolvable — vault must win.
        monkeypatch.setenv("FAL_KEY", FAKE_FAL_KEY)
        monkeypatch.setattr(
            image_tool, "_resolve_managed_fal_gateway",
            lambda: pytest.fail("managed gateway must not be consulted"),
        )
        monkeypatch.setattr(
            image_tool, "_load_fal_client",
            lambda: pytest.fail("fal_client must not load on the vault path"),
        )
        handle = image_tool._submit_fal_request("fal-ai/x", {"prompt": "a cat"})
        assert isinstance(handle, VaultFalRequestHandle)
        submit = http.calls[0]["payload"]
        assert submit["method"] == "POST"
        assert submit["path"] == "/fal-ai/x"
        assert submit["json"] == {"prompt": "a cat"}
        assert "x-idempotency-key" in submit["headers"]

    def test_handle_polls_until_completed_and_returns_result(self, vault_env):
        http = RecordingVaultHttp(status_sequence=("IN_QUEUE", "IN_PROGRESS", "COMPLETED"))
        handle = submit_fal_via_vault(http, "FAL_MAIN", "fal-ai/x", {"prompt": "p"})
        handle._poll_interval = 0
        result = handle.get()
        assert result["images"][0]["url"].endswith("out.png")
        status_calls = [c for c in http.calls
                        if str((c["payload"] or {}).get("path", "")).endswith("/status")]
        assert len(status_calls) == 3

    def test_terminal_error_state_raises(self, vault_env):
        http = RecordingVaultHttp(status_sequence=("ERROR",))
        handle = submit_fal_via_vault(http, "FAL_MAIN", "fal-ai/x", {"prompt": "p"})
        handle._poll_interval = 0
        with pytest.raises(ValueError, match="ERROR"):
            handle.get()

    def test_http_error_via_vault_raises_with_status(self, vault_env):
        def failing_http(method, path, payload=None, timeout=None):
            return {"status": 404, "json": {"detail": "Not Found"}}
        with pytest.raises(ValueError, match="HTTP 404"):
            submit_fal_via_vault(failing_http, "FAL_MAIN", "fal-ai/x", {"prompt": "p"})

    def test_availability_counts_vault_connection(self, vault_env, monkeypatch):
        monkeypatch.setattr(image_tool, "_resolve_managed_fal_gateway", lambda: None)
        assert image_tool.check_fal_api_key() is True


class TestLeakDetection:
    def test_credential_never_crosses_agent_boundary(self, vault_env, monkeypatch):
        """End-to-end over the vault path: nothing the agent sends contains
        the FAL key (it never has it), and nothing it receives contains it
        (the vault scrubs). Serialize every request payload and every
        response and assert the secret is absent."""
        http = RecordingVaultHttp(status_sequence=("IN_PROGRESS", "COMPLETED"))
        monkeypatch.setattr("tools.vault_tools._vault_http", http)
        monkeypatch.setenv("FAL_KEY", FAKE_FAL_KEY)  # present but must be unused

        handle = image_tool._submit_fal_request("fal-ai/x", {"prompt": "a cat"})
        handle._poll_interval = 0
        result = handle.get()

        for call in http.calls:
            assert FAKE_FAL_KEY not in json.dumps(call["payload"], default=str)
            # No auth-ish headers assembled agent-side either.
            headers = (call["payload"] or {}).get("headers") or {}
            assert not any(k.lower() == "authorization" for k in headers)
        assert FAKE_FAL_KEY not in json.dumps(result, default=str)
        assert result["reflected_auth"] == "***vault***"
