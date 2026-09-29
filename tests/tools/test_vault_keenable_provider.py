"""Mock-only Keenable vault adapter tests; never contact vault or upstream."""

import asyncio
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from plugins.web.vault_keenable import provider as vk
from services.vault.catalog import CATALOG


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("VAULT_URL", "https://vault.test")
    monkeypatch.setenv("VAULT_TOKEN", "not-a-real-token")
    monkeypatch.delenv("KEENABLE_VAULT_CONNECTION", raising=False)
    monkeypatch.setattr(vk, "_cache_until", 0)
    monkeypatch.setattr(vk, "_cached_id", None)
    monkeypatch.setattr(vk, "_cached_hint", "")
    monkeypatch.setattr(vk, "is_safe_url", lambda url: "127.0.0.1" not in url)
    monkeypatch.setattr(vk, "check_website_access", lambda url: None)


def test_catalog_and_bundled_manifest(monkeypatch):
    from hermes_cli.plugins import PluginManager, get_bundled_plugins_dir
    from tools.registry import registry
    from agent import web_search_registry

    tpl = CATALOG["keenable"]
    assert tpl["auth"] == {"kind": "header", "header_name": "X-API-Key", "prefix": ""}
    assert tpl["base_url"] == "https://api.keenable.ai"
    assert tpl["allowed_hosts"] == ["api.keenable.ai"]
    assert tpl["test_probe"]["json"]["mode"] == "pro"
    register = Mock()
    monkeypatch.setattr(web_search_registry, "register_provider", register)
    monkeypatch.setattr(registry, "register_plugin_override_policy", Mock())
    manager = PluginManager()
    matches = [m for m in manager._scan_directory(
        get_bundled_plugins_dir(), source="bundled",
    ) if m.key == "web/vault_keenable"]
    assert len(matches) == 1 and matches[0].kind == "backend"
    manager._load_plugin(matches[0])
    assert manager._plugins["web/vault_keenable"].enabled
    provider = register.call_args.args[0]
    assert provider.name == "vault_keenable"
    assert provider.supports_search() and provider.supports_extract()
    assert provider.get_setup_schema()["env_vars"] == []


def test_discovery_only_ready_granted_exact_host_header_and_hint(monkeypatch):
    calls = []
    listing = {"available_connections": [
        {"id": "bad0", "service": "keenable", "status": "pending", "auth_kind": "header",
         "base_url": "https://api.keenable.ai"},
        {"id": "bad1", "service": "keenable", "status": "ready", "auth_kind": "bearer",
         "base_url": "https://api.keenable.ai"},
        {"id": "bad2", "service": "keenable", "status": "ready", "auth_kind": "header",
         "base_url": "https://api.keenable.ai.evil.test"},
        {"id": "bad3", "service": "other", "status": "ready", "auth_kind": "header",
         "base_url": "https://api.keenable.ai"},
        {"id": "bad4", "service": "keenable", "status": "ready", "auth_kind": "header",
         "auth_header_name": "Authorization", "base_url": "https://api.keenable.ai"},
        {"id": "OK", "service": "keenable", "status": "ready", "auth_kind": "header",
         "base_url": "https://api.keenable.ai"},
    ]}
    monkeypatch.setattr(vk, "_vault_http", lambda *a, **k: (calls.append((a, k)), listing)[1])
    assert vk._connection_id() == "OK"
    assert vk._connection_id() == "OK"
    assert len(calls) == 1
    assert calls[0][0] == ("GET", "/api/vault/list")
    assert calls[0][1]["timeout"] == 3
    monkeypatch.setenv("KEENABLE_VAULT_CONNECTION", "bad1")
    assert vk._connection_id() is None
    assert vk._connection_id() is None
    assert len(calls) == 2
    monkeypatch.setenv("KEENABLE_VAULT_CONNECTION", "OK")
    assert vk._connection_id() == "OK"
    monkeypatch.setenv("KEENABLE_VAULT_CONNECTION", "bad/id")
    assert vk._connection_id() is None
    assert len(calls) == 3


def test_no_vault_without_credentials_or_on_listing_failure(monkeypatch):
    http = Mock(side_effect=TimeoutError("secret-reflected"))
    monkeypatch.setattr(vk, "_vault_http", http)
    monkeypatch.delenv("VAULT_URL")
    assert not vk.VaultKeenableWebSearchProvider().is_available()
    monkeypatch.setenv("VAULT_URL", "https://vault.test")
    monkeypatch.delenv("VAULT_TOKEN")
    assert not vk.VaultKeenableWebSearchProvider().is_available()
    http.assert_not_called()
    monkeypatch.setenv("VAULT_TOKEN", "not-a-real-token")
    assert not vk.VaultKeenableWebSearchProvider().is_available()
    assert http.call_count == 1


def test_pro_search_bounds_filters_policy_and_uses_proxy(monkeypatch):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    monkeypatch.setattr(vk, "check_website_access", lambda u: "denied" if "blocked" in u else None)
    calls = []

    def http(method, path, body, **kwargs):
        calls.append((method, path, body))
        return {"status": 200, "json": {"query": "topic", "mode": "pro", "results": [
            {"url": "https://public.example/a", "title": "Article",
             "description": "Summary", "snippet": "Extract", "headers": {"Cookie": "hidden"}},
            {"url": "https://public.example/?api_key=hidden"},
            {"url": "http://127.0.0.1/private"},
            {"url": "https://blocked.example/a"},
        ]}}

    monkeypatch.setattr(vk, "_vault_http", http)
    result = vk.VaultKeenableWebSearchProvider().search("topic", 50)
    assert result == {"success": True, "data": {"web": [
        {"url": "https://public.example/a", "title": "Article",
         "description": "Summary", "content": "Extract"},
    ]}}
    assert calls == [("POST", "/api/vault/proxy/OK", {
        "method": "POST", "path": "/v1/search",
        "json": {"query": "topic", "mode": "pro", "max_results": 10},
        "timeout": 30,
    })]
    assert "keen_" not in str(calls)


def test_get_fetch_url_guards_final_url_and_batch_cap(monkeypatch):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    calls = []

    def http(method, path, body, **kwargs):
        calls.append(body)
        return {"status": 200, "json": {
            "url": "https://final.example/a", "title": "Page", "content": "# Page",
            "headers": {"Cookie": "hidden"},
        }}

    monkeypatch.setattr(vk, "_vault_http", http)
    urls = ["http://127.0.0.1/private", "https://public.example/?token=hidden",
            "https://public.example/a"] + ["https://public.example/b"] * 5
    result = asyncio.run(vk.VaultKeenableWebSearchProvider().extract(urls))
    assert len(calls) == 3  # first two URLs blocked before proxy
    assert calls[0] == {
        "method": "GET", "path": "/v1/fetch",
        "params": {"url": urls[2], "live": True, "max_chars": 50000},
        "timeout": 45,
    }
    assert result[0]["url"] == result[1]["url"] == ""
    assert result[2]["url"] == "https://final.example/a"
    assert result[2]["content"] == result[2]["raw_content"] == "# Page"
    assert result[2]["metadata"] == {"sourceURL": "https://final.example/a"}
    assert "Cookie" not in str(result)
    assert len(result) == len(urls)
    assert all("At most" in r["error"] for r in result[5:])


@pytest.mark.parametrize("final", [
    "http://127.0.0.1/private", "https://public.example/?token=secret",
    "https://keen_example:password@public.example/path", None,
])
def test_fetch_rejects_unsafe_final_url(monkeypatch, final):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    monkeypatch.setattr(vk, "_vault_http", lambda *a, **k: {
        "status": 200, "json": {"url": final, "content": "secret content"},
    })
    result = asyncio.run(vk.VaultKeenableWebSearchProvider().extract(["https://public.example/a"]))
    assert result[0]["content"] == ""
    assert result[0]["url"] == "https://public.example/a"
    assert result[0]["error"] == "Blocked: unsafe final URL"


def test_policy_applies_before_and_after_fetch(monkeypatch):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    monkeypatch.setattr(vk, "check_website_access", lambda u: "denied" if "blocked" in u else None)
    http = Mock(return_value={"status": 200, "json": {
        "url": "https://blocked.example/a", "content": "private content",
    }})
    monkeypatch.setattr(vk, "_vault_http", http)
    result = asyncio.run(vk.VaultKeenableWebSearchProvider().extract([
        "https://blocked.example/a", "https://public.example/a",
    ]))
    assert http.call_count == 1
    assert all(r["content"] == "" and r["error"] == "Blocked by website access policy" for r in result)


@pytest.mark.parametrize("status,expected", [
    (401, "authentication rejected"), (402, "credits or payment required"),
    (403, "access denied"), (429, "rate limit reached"),
    (503, "request failed"),
])
def test_status_errors_are_actionable_and_sanitized(monkeypatch, status, expected):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    monkeypatch.setattr(vk, "_vault_http", lambda *a, **k: {
        "status": status, "json": {"error": "keen_secret-key and credentials"},
        "text": "keen_secret-key",
    })
    error = vk.VaultKeenableWebSearchProvider().search("topic")["error"]
    assert expected in error
    assert "keen_secret-key" not in error


def test_malformed_and_transport_errors_do_not_expose_bodies(monkeypatch):
    monkeypatch.setattr(vk, "_connection_id", lambda: "OK")
    monkeypatch.setattr(vk, "_vault_http", lambda *a, **k: {
        "status": 200, "json": {"error": "keen_secret-key"},
    })
    assert vk.VaultKeenableWebSearchProvider().search("topic")["error"] == "Invalid Keenable search results"
    result = asyncio.run(vk.VaultKeenableWebSearchProvider().extract(["https://public.example/a"]))
    assert result[0]["error"] == "Blocked: unsafe final URL"

    def rejected(*a, **k):
        raise HTTPError("https://vault.test", 403, "keen_secret-key", None, None)

    monkeypatch.setattr(vk, "_vault_http", rejected)
    assert vk.VaultKeenableWebSearchProvider().search("topic")["error"] == "Vault proxy rejected request (HTTP 403)"