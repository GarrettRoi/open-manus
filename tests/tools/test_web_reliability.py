"""Mock-only checks for shared web dispatch; no HTTP, Redis, or browser I/O."""
import json

import pytest

from tools import web_tools as wt
from tools.web_reliability import content_problem


class Provider:
    def __init__(self, name, search=None, extract=None, available=True):
        self.name = name
        self._search = search
        self._extract = extract
        self._available = available
        self.calls = []

    def is_available(self):
        return self._available

    def supports_search(self):
        return self._search is not None

    def supports_extract(self):
        return self._extract is not None

    def search(self, query, limit):
        self.calls.append(("search", query, limit))
        return self._search()

    async def extract(self, urls, **kwargs):
        self.calls.append(("extract", urls))
        return self._extract(urls)


@pytest.fixture
def isolated(monkeypatch):
    from agent import web_search_registry as reg

    providers = {}
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_list_registered_web_providers", lambda: list(providers.values()))
    monkeypatch.setattr(wt, "_get_search_backend", lambda: "vault_firecrawl")
    monkeypatch.setattr(wt, "_get_extract_backend", lambda: "vault_firecrawl")
    monkeypatch.setattr(reg, "get_provider", providers.get)
    monkeypatch.setattr(reg, "get_active_search_provider", lambda: None)
    monkeypatch.setattr(reg, "get_active_extract_provider", lambda: None)
    monkeypatch.setattr(reg, "_disabled_web_plugin_for", lambda **kw: None)
    monkeypatch.setattr(wt, "_debug", type("Debug", (), {"log_call": lambda *a: None,
                                                        "save": lambda *a: None})())
    async def safe(url):
        return not url.startswith("http://127.")
    monkeypatch.setattr(wt, "async_is_safe_url", safe)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    return providers


def test_auto_vault_precedes_paid_but_explicit_wins(isolated, monkeypatch):
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", search=lambda: {}, extract=lambda _: [])
    monkeypatch.setattr(wt, "_load_web_config", lambda: {})
    monkeypatch.setattr(wt, "_has_env", lambda key: key == "TAVILY_API_KEY")
    assert wt._get_backend() == "vault_firecrawl"
    monkeypatch.setattr(wt, "_load_web_config", lambda: {"backend": "tavily"})
    assert wt._get_backend() == "tavily"
    del isolated["vault_firecrawl"]  # disabled plugins aren't registered
    monkeypatch.setattr(wt, "_load_web_config", lambda: {})
    assert wt._get_backend() == "tavily"


def test_search_bounded_fallback_and_sanitized_exhaustion(isolated):
    secret = "secret-value-must-not-appear"
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", search=lambda: {"success": True, "data": {"web": []}})
    isolated["firecrawl"] = Provider("firecrawl", search=lambda: (_ for _ in ()).throw(RuntimeError(secret)))
    isolated["tavily"] = Provider("tavily", search=lambda: {"success": True, "data": {"web": "oops"}})
    isolated["exa"] = Provider("exa", search=lambda: {"success": True, "data": {"web": [{"url": "https://unused.test"}]}})
    result = json.loads(wt.web_search_tool("query"))
    assert result["success"] is False
    assert [a["provider"] for a in result["attempts"]] == ["vault_firecrawl", "firecrawl", "tavily"]
    assert "browser_navigate" in result["error"]
    assert secret not in json.dumps(result)
    assert isolated["exa"].calls == []


def test_search_falls_back_after_malformed_and_preserves_output(isolated):
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", search=lambda: None)
    isolated["firecrawl"] = Provider("firecrawl", search=lambda: {
        "success": True, "data": {"web": [{"url": "https://article.test", "title": "Article"}]}})
    result = json.loads(wt.web_search_tool("query"))
    assert result["success"] is True
    assert result["data"]["web"][0]["title"] == "Article"
    assert [a["status"] for a in result["attempts"]] == ["provider error", "success"]


@pytest.mark.asyncio
async def test_extract_partial_retries_only_failures_and_block_is_terminal(isolated):
    urls = ["https://good.test", "https://challenge.test", "https://policy.test"]
    def first(batch):
        return [
            {"url": batch[0], "content": "Full article " * 30},
            {"url": batch[1], "content": "Verify you are human. Solve the CAPTCHA."},
            {"url": batch[2], "content": "", "error": "Blocked by website policy",
             "blocked_by_policy": {"rule": "policy"}},
        ]
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", extract=first)
    isolated["firecrawl"] = Provider("firecrawl", extract=lambda batch: [
        {"url": batch[0], "content": "Recovered article text."}])
    result = json.loads(await wt.web_extract_tool(urls))
    assert isolated["firecrawl"].calls == [("extract", [urls[1]])]
    assert result["results"][0]["content"].startswith("Full article")
    assert result["results"][1]["content"] == "Recovered article text."
    assert result["results"][2]["content"] == ""
    assert result["results"][2]["blocked_by_policy"]["rule"] == "policy"
    assert "next_step" not in result


@pytest.mark.asyncio
async def test_vault_empty_url_errors_match_position_and_policy_is_terminal(isolated):
    urls = ["https://policy.test", "https://retry.test", "https://ok.test"]
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", extract=lambda batch: [
        {"url": "", "error": "Blocked by website access policy"},
        {"url": "", "error": "Firecrawl returned no markdown"},
        {"url": "https://final.test", "content": "Redirected article"},
    ])
    isolated["firecrawl"] = Provider("firecrawl", extract=lambda batch: [
        {"url": batch[0], "content": "Fallback article"},
    ])
    result = json.loads(await wt.web_extract_tool(urls))
    assert isolated["firecrawl"].calls == [("extract", [urls[1]])]
    assert result["results"][0]["content"] == ""
    assert result["results"][0]["error"] == "Blocked by website policy"
    assert result["results"][1]["content"] == "Fallback article"
    assert result["results"][2]["url"] == "https://final.test"
    assert result["results"][2]["content"] == "Redirected article"


@pytest.mark.asyncio
async def test_vault_final_url_policy_failure_never_falls_back(isolated):
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", extract=lambda batch: [
        # Vault's final-URL policy check returns an empty URL to avoid
        # exposing the rejected redirect target.
        {"url": "", "content": "", "error": "Blocked by website access policy"},
    ])
    isolated["firecrawl"] = Provider("firecrawl", extract=lambda batch: [
        {"url": batch[0], "content": "Should not be fetched"}])
    result = json.loads(await wt.web_extract_tool(["https://original.test"]))
    assert result["results"][0]["error"] == "Blocked by website policy"
    assert result["results"][0]["content"] == ""
    assert isolated["firecrawl"].calls == []


@pytest.mark.asyncio
async def test_extract_exhaustion_and_ssrf_never_fallback(isolated):
    isolated["vault_firecrawl"] = Provider("vault_firecrawl", extract=lambda batch: [
        {"url": batch[0], "content": "Checking your browser..."}])
    isolated["firecrawl"] = Provider("firecrawl", extract=lambda batch: [])
    isolated["tavily"] = Provider("tavily", extract=lambda batch: [
        {"url": batch[0], "error": "failed: secret-value-must-not-appear"}])
    result = json.loads(await wt.web_extract_tool(["https://challenge.test", "http://127.0.0.1/admin"]))
    assert len(result["attempts"]) == 3
    assert result["results"][0]["content"] == ""
    assert "browser_navigate" in result["next_step"]
    assert "secret-value-must-not-appear" not in json.dumps(result)
    assert any(r["error"].startswith("Blocked:") for r in result["results"])
    assert all("127.0.0.1" not in str(p.calls) for p in isolated.values())


def test_classifier_does_not_reject_article_about_captcha():
    assert content_problem("This article discusses CAPTCHA and why sites use it. " * 8) is None
    assert content_problem("Verify you are human. Solve the CAPTCHA.") is not None