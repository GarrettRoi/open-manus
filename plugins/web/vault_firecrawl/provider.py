"""Firecrawl v2 web search and scrape through the credential-injecting vault proxy."""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit

from agent.redact import _PREFIX_RE
from agent.web_search_provider import WebSearchProvider
from tools.url_safety import is_safe_url, normalize_url_for_request, sensitive_query_param_name
from tools.vault_tools import _vault_http
from tools.website_policy import check_website_access

_cache_lock = threading.Lock()
_cached_id: Optional[str] = None
_cache_until = 0.0
_cached_hint = ""
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_URLS = 5


def _connection_id() -> Optional[str]:
    """Short-lived grant/ready check; failures are cached briefly, never forever."""
    global _cached_id, _cache_until, _cached_hint
    # _vault_http has a default internal URL; don't probe that URL just because
    # the bundled plugin was loaded on a non-vault installation.
    if not os.getenv("VAULT_URL") or not os.getenv("VAULT_TOKEN"):
        return None
    hint = os.getenv("FIRECRAWL_VAULT_CONNECTION", "").strip()
    now = time.monotonic()
    with _cache_lock:
        if hint == _cached_hint and now < _cache_until:
            return _cached_id
        selected = None
        try:
            if not hint or _ID_RE.fullmatch(hint):
                listing = _vault_http(
                    "GET", "/api/vault/list", timeout=3,
                    extra_headers={"X-Vault-Background": "1"},
                )
                connections = listing.get("available_connections", []) if isinstance(listing, dict) else []
                if isinstance(connections, list):
                    for conn in connections:
                        if not isinstance(conn, dict) or conn.get("status") != "ready":
                            continue
                        cid = conn.get("id")
                        if not isinstance(cid, str) or not _ID_RE.fullmatch(cid):
                            continue
                        if conn.get("auth_kind") != "bearer":
                            continue
                        base = conn.get("base_url", "")
                        try:
                            parsed = urlsplit(base)
                            exact_host = (parsed.scheme == "https" and
                                          parsed.hostname == "api.firecrawl.dev" and
                                          parsed.port in (None, 443) and
                                          not parsed.username and not parsed.password)
                        except (ValueError, TypeError):
                            exact_host = False
                        if not (conn.get("service") == "firecrawl" or exact_host):
                            continue
                        if hint and cid != hint:
                            continue
                        selected = cid
                        break
        except Exception:
            # Discovery is advisory; vault outages and malformed responses must
            # never block tool registration or surface transport error bodies.
            pass
        _cached_id, _cached_hint = selected, hint
        _cache_until = time.monotonic() + (30 if selected else 5)
        return selected


def _safe_target(url: Any) -> Optional[str]:
    """Fail closed before a third-party fetch AND before returning a redirect URL."""
    if not isinstance(url, str) or len(url) > 4096:
        return None
    normalized = normalize_url_for_request(url)
    try:
        parsed = urlsplit(normalized)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.fragment or
                sensitive_query_param_name(normalized) or
                _PREFIX_RE.search(url) or _PREFIX_RE.search(unquote(url)) or
                _PREFIX_RE.search(normalized) or _PREFIX_RE.search(unquote(normalized))):
            return None
        if not is_safe_url(normalized):
            return None
    except (ValueError, UnicodeError):
        return None
    return normalized


def _proxy(conn_id: str, path: str, body: dict, timeout: int = 30) -> dict:
    """Unwrap vault's {status,json,truncated} envelope without returning raw errors."""
    try:
        response = _vault_http(
            "POST", f"/api/vault/proxy/{conn_id}",
            {"method": "POST", "path": path, "json": body, "timeout": timeout},
            timeout=timeout + 5,
        )
    except HTTPError as exc:
        raise ValueError(f"Vault proxy rejected request (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("Vault proxy unavailable or timed out") from None
    except Exception:
        raise ValueError("Vault proxy request failed") from None
    if not isinstance(response, dict):
        raise ValueError("Invalid vault proxy response")
    status = response.get("status")
    if not isinstance(status, int) or not 200 <= status < 300:
        # Never include provider bodies or exception messages; they may echo credentials.
        code = status if isinstance(status, int) else "unknown"
        raise ValueError(f"Firecrawl request failed (HTTP {code})")
    if response.get("truncated"):
        raise ValueError("Firecrawl response exceeded vault response limit")
    data = response.get("json")
    if not isinstance(data, dict):
        raise ValueError("Invalid Firecrawl response")
    if data.get("success") is False:
        raise ValueError("Firecrawl reported an unsuccessful request")
    return data


class VaultFirecrawlWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "vault_firecrawl"

    @property
    def display_name(self) -> str:
        return "Firecrawl (Vault)"

    def is_available(self) -> bool:
        return _connection_id() is not None

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        conn_id = _connection_id()
        if not conn_id:
            return {"success": False, "error": "No ready, granted Firecrawl vault connection"}
        if not isinstance(query, str) or not query.strip():
            return {"success": False, "error": "Search query is required"}
        try:
            count = max(1, min(int(limit), 10))
        except (TypeError, ValueError):
            count = 5
        try:
            payload = _proxy(conn_id, "/v2/search", {"query": query, "limit": count})
            data = payload.get("data")
            web = data.get("web", []) if isinstance(data, dict) else data
            if not isinstance(web, list):
                raise ValueError("Invalid Firecrawl search results")
            results = []
            for hit in web[:count]:
                if not isinstance(hit, dict):
                    continue
                url = _safe_target(hit.get("url"))
                if not url or check_website_access(url):
                    continue
                results.append({
                    "url": url,
                    "title": str(hit.get("title") or "")[:500],
                    "description": str(hit.get("description") or "")[:2000],
                    "content": str(hit.get("markdown") or hit.get("content") or "")[:10000],
                })
            return {"success": True, "data": {"web": results}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        conn_id = _connection_id()
        results: List[Dict[str, Any]] = []
        # Sequential requests and a low page cap bound both billing and execution time.
        for original in urls[:_MAX_URLS]:
            entry: Dict[str, Any] = {"url": "", "title": "", "content": "", "raw_content": ""}
            url = await asyncio.to_thread(_safe_target, original)
            if url:
                # Preserve source identity for positional/fallback matching even
                # if the proxy fails. Unsafe/credential URLs are never echoed.
                entry["url"] = url
            if not url:
                entry["error"] = "Blocked: invalid, sensitive, or private URL"
            elif check_website_access(url):
                entry["error"] = "Blocked by website access policy"
            elif not conn_id:
                entry["error"] = "No ready, granted Firecrawl vault connection"
            else:
                try:
                    # Rendered markdown is standard Firecrawl scrape behavior. No proxy/stealth
                    # paid escalation is requested.
                    payload = await asyncio.to_thread(
                        _proxy, conn_id, "/v2/scrape",
                        {"url": url, "formats": ["markdown"]}, 45,
                    )
                    data = payload.get("data")
                    if not isinstance(data, dict):
                        raise ValueError("Invalid Firecrawl scrape response")
                    metadata = data.get("metadata")
                    metadata = metadata if isinstance(metadata, dict) else {}
                    final = await asyncio.to_thread(
                        _safe_target, metadata.get("sourceURL") or url,
                    )
                    if not final:
                        raise ValueError("Blocked: unsafe final URL")
                    if check_website_access(final):
                        raise ValueError("Blocked by website access policy")
                    content = data.get("markdown")
                    if not isinstance(content, str):
                        raise ValueError("Firecrawl returned no markdown")
                    entry.update({
                        "url": final,
                        "title": str(metadata.get("title") or "")[:500],
                        "content": content,
                        "raw_content": content,
                        # Deliberately exclude arbitrary metadata (cookies/headers).
                        "metadata": {"sourceURL": final},
                    })
                except ValueError as exc:
                    entry["error"] = str(exc)
            results.append(entry)
        for original in urls[_MAX_URLS:]:
            safe_url = await asyncio.to_thread(_safe_target, original)
            results.append({
                "url": safe_url or "", "title": "", "content": "",
                "error": f"At most {_MAX_URLS} URLs per extraction request",
            })
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Firecrawl (Vault)",
            "badge": "vault · paid",
            "tag": "Search and rendered markdown extraction using a granted vault connection.",
            "env_vars": [],
        }