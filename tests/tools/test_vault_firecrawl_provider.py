"""Isolated vault-Firecrawl contract tests: no real vault, DNS, Redis or upstream."""

import asyncio
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from plugins.web.vault_firecrawl import provider as vf
from services.vault.catalog import CATALOG


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("VAULT_URL", "https://vault.test")
    monkeypatch.setenv("VAULT_TOKEN", "test-token-not-real")
    monkeypatch.delenv("FIRECRAWL_VAULT_CONNECTION", raising=False)
    monkeypatch.setattr(vf, "is_safe_url", lambda url: True)
    monkeypatch.setattr(vf, "check_website_access", lambda url: None)
    monkeypatch.setattr(vf, "_cache_until", 0)
    monkeypatch.setattr(vf, "_cached_id", None)
    monkeypatch.setattr(vf, "_cached_hint", "")


def test_catalog_and_plugin_registration():
    from plugins.web.vault_firecrawl import register

    ctx = Mock()
    register(ctx)
    provider = ctx.register_web_search_provider.call_args.args[0]
    assert provider.name == "vault_firecrawl"
    assert provider.supports_search() and provider.supports_extract()
    tpl = CATALOG["firecrawl"]
    assert tpl["auth"] == {"kind": "bearer"}
    assert tpl["allowed_hosts"] == ["api.firecrawl.dev"]
    assert tpl["test_probe"]["method"] == "GET"


def test_bundled_manifest_loads_and_registers_via_plugin_manager(monkeypatch):
    from hermes_cli.plugins import PluginManager, get_bundled_plugins_dir
    from tools.registry import registry
    from agent import web_search_registry

    register = Mock()
    monkeypatch.setattr(web_search_registry, "register_provider", register)
    monkeypatch.setattr(registry, "register_plugin_override_policy", Mock())
    manager = PluginManager()
    matches = [m for m in manager._scan_directory(
        get_bundled_plugins_dir(), source="bundled",
    ) if m.key == "web/vault_firecrawl"]
    assert len(matches) == 1
    assert matches[0].kind == "backend"
    manager._load_plugin(matches[0])
    assert manager._plugins["web/vault_firecrawl"].enabled
    assert register.call_args.args[0].name == "vault_firecrawl"


def test_discovery_grants_hint_and_short_negative_cache(monkeypatch):
    calls = []
    listing = {"available_connections": [
        {"id": "PENDING", "service": "firecrawl", "status": "pending", "auth_kind": "bearer"},
        {"id": "OTHER", "service": "other", "base_url": "https://evil.test", "status": "ready", "auth_kind": "bearer"},
        {"id": "FC", "service": "firecrawl", "status": "ready", "auth_kind": "bearer"},
    ]}

    def http(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return listing

    monkeypatch.setattr(vf, "_vault_http", http)
    assert vf._connection_id() == "FC"
    assert vf._connection_id() == "FC"
    assert len(calls) == 1
    assert calls[0][2]["timeout"] == 3
    monkeypatch.setenv("FIRECRAWL_VAULT_CONNECTION", "OTHER")
    assert vf._connection_id() is None
    assert len(calls) == 2
    assert vf._connection_id() is None
    assert len(calls) == 2
    monkeypatch.setenv("FIRECRAWL_VAULT_CONNECTION", "FC")
    assert vf._connection_id() == "FC"


def test_discovery_failure_does_not_raise_or_contact_datastore(monkeypatch):
    def offline(*args, **kwargs):
        raise TimeoutError("Bearer secret-should-never-leak")

    monkeypatch.setattr(vf, "_vault_http", offline)
    assert vf.VaultFirecrawlWebSearchProvider().is_available() is False


def test_discovery_never_contacts_vault_without_explicit_url_and_token(monkeypatch):
    http = Mock(side_effect=AssertionError("vault HTTP should not run"))
    monkeypatch.setattr(vf, "_vault_http", http)
    monkeypatch.delenv("VAULT_URL")
    assert not vf.VaultFirecrawlWebSearchProvider().is_available()
    monkeypatch.setenv("VAULT_URL", "https://vault.test")
    monkeypatch.delenv("VAULT_TOKEN")
    assert not vf.VaultFirecrawlWebSearchProvider().is_available()
    http.assert_not_called()


def test_search_v2_envelope_bounds_sanitization(monkeypatch):
    monkeypatch.setattr(vf, "_connection_id", lambda: "FC")
    requests = []

    def http(method, path, payload=None, **kwargs):
        requests.append((method, path, payload))
        return {"status": 200, "json": {
            "success": True,
            "data": {"web": [
                {"url": "https://public.example/article", "title": "Article",
                 "description": "Abstract", "markdown": "Markdown excerpt",
                 "headers": {"set-cookie": "sensitive"}},
                {"url": "https://public.example/?api_key=hidden", "title": "Blocked"},
            ]},
        }}

    monkeypatch.setattr(vf, "_vault_http", http)
    result = vf.VaultFirecrawlWebSearchProvider().search("topic", 100)
    assert result == {"success": True, "data": {"web": [
        {"url": "https://public.example/article", "title": "Article",
         "description": "Abstract", "content": "Markdown excerpt"}
    ]}}
    assert requests[0][1:] == ("/api/vault/proxy/FC", {
        "method": "POST", "path": "/v2/search", "json": {"query": "topic", "limit": 10},
        "timeout": 30,
    })
    assert "Authorization" not in str(requests)


def test_search_errors_are_safe(monkeypatch):
    monkeypatch.setattr(vf, "_connection_id", lambda: "FC")
    monkeypatch.setattr(vf, "_vault_http", lambda *a, **k: {
        "status": 429, "json": {"error": "my secret and cookies"},
    })
    result = vf.VaultFirecrawlWebSearchProvider().search("test")
    assert result == {"success": False, "error": "Firecrawl request failed (HTTP 429)"}
    monkeypatch.setattr(vf, "_vault_http", lambda *a, **k: {
        "status": 200, "json": {"success": False, "error": "my secret"},
    })
    assert vf.VaultFirecrawlWebSearchProvider().search("test")["error"] == "Firecrawl reported an unsuccessful request"

    def rejected(*a, **k):
        raise HTTPError("https://vault.internal", 403, "secret!", None, None)

    monkeypatch.setattr(vf, "_vault_http", rejected)
    assert vf.VaultFirecrawlWebSearchProvider().search("test")["error"] == "Vault proxy rejected request (HTTP 403)"


def test_extract_rendered_markdown_redirect_and_private_target(monkeypatch):
    monkeypatch.setattr(vf, "_connection_id", lambda: "FC")
    monkeypatch.setattr(vf, "is_safe_url", lambda url: "127.0.0.1" not in url)
    calls = []

    def http(method, path, payload, **kwargs):
        calls.append(payload)
        return {"status": 200, "json": {"success": True, "data": {
            "markdown": "# Page", "metadata": {
                "sourceURL": "https://final.example/path", "title": "Page",
                "headers": {"Cookie": "private"}, "cookies": "private",
            },
        }}}

    monkeypatch.setattr(vf, "_vault_http", http)
    result = asyncio.run(vf.VaultFirecrawlWebSearchProvider().extract([
        "http://127.0.0.1/private", "https://public.example/path",
        "https://public.example/?token=sensitive",
    ]))
    assert len(calls) == 1
    assert calls[0]["json"] == {"url": "https://public.example/path", "formats": ["markdown"]}
    assert "error" in result[0] and result[0]["url"] == ""
    assert result[1]["url"] == "https://final.example/path"
    assert result[1]["content"] == "# Page"
    assert result[1]["metadata"] == {"sourceURL": "https://final.example/path"}
    assert "cookies" not in str(result[1]) and "Cookie" not in str(result[1])
    assert "error" in result[2]


def test_extract_rechecks_final_url_and_limits_batch(monkeypatch):
    monkeypatch.setattr(vf, "_connection_id", lambda: "FC")
    monkeypatch.setattr(vf, "is_safe_url", lambda url: "127.0.0.1" not in url)
    calls = []

    def http(method, path, payload, **kwargs):
        calls.append(payload)
        return {"status": 200, "json": {"success": True, "data": {
            "markdown": "inaccessible private data",
            "metadata": {"sourceURL": "http://127.0.0.1/private"},
        }}}

    monkeypatch.setattr(vf, "_vault_http", http)
    results = asyncio.run(vf.VaultFirecrawlWebSearchProvider().extract(
        ["https://public.example/page"] * 8,
    ))
    assert len(calls) == 5
    assert all("error" in item and not item["content"] for item in results)
    assert len(results) == 8
    assert all(item["url"] == "https://public.example/page" for item in results)


def test_proxy_error_keeps_only_safe_source_url(monkeypatch):
    monkeypatch.setattr(vf, "_connection_id", lambda: "FC")
    monkeypatch.setattr(vf, "_vault_http", lambda *a, **k: {
        "status": 503, "text": "upstream reflected credentials",
    })
    urls = ["https://public.example/a", "https://public.example/?token=sensitive"]
    results = asyncio.run(vf.VaultFirecrawlWebSearchProvider().extract(urls))
    assert results[0]["url"] == urls[0]
    assert results[0]["error"] == "Firecrawl request failed (HTTP 503)"
    assert results[1]["url"] == ""
    assert "token=sensitive" not in str(results)