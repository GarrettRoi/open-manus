"""Keenable search and fetch using only the credential-injecting vault proxy."""

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
_cached_hint = ""
_cache_until = 0.0
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_MAX_URLS = 5


def _connection_id() -> Optional[str]:
    """Only select a ready grant for the exact Keenable origin and header auth."""
    global _cached_id, _cached_hint, _cache_until
    if not os.getenv("VAULT_URL") or not os.getenv("VAULT_TOKEN"):
        return None
    hint = os.getenv("KEENABLE_VAULT_CONNECTION", "").strip()
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
                        if conn.get("service") != "keenable" or conn.get("auth_kind") != "header":
                            continue
                        # The list view currently only exposes auth_kind; verify
                        # header configuration as well if a newer vault exposes it.
                        auth = conn.get("auth")
                        if isinstance(auth, dict) and (
                            auth.get("kind", "header") != "header"
                            or auth.get("header_name", "X-API-Key").lower() != "x-api-key"
                            or auth.get("prefix", "") != ""
                        ):
                            continue
                        if conn.get("auth_header_name", "X-API-Key").lower() != "x-api-key":
                            continue
                        if conn.get("auth_prefix", "") != "":
                            continue
                        if conn.get("base_url") not in ("https://api.keenable.ai", "https://api.keenable.ai/"):
                            continue
                        if hint and cid != hint:
                            continue
                        selected = cid
                        break
        except Exception:
            # Advisory discovery must not reveal transport/provider error bodies.
            pass
        _cached_id, _cached_hint = selected, hint
        _cache_until = time.monotonic() + (30 if selected else 5)
        return selected


def _safe_target(url: Any) -> Optional[str]:
    """Reject credential-bearing, private and malformed URLs before proxying."""
    if not isinstance(url, str) or len(url) > 4096:
        return None
    try:
        normalized = normalize_url_for_request(url)
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


def _status_error(status: Any) -> str:
    if status == 401:
        return "Keenable authentication rejected (HTTP 401); check the vault connection key"
    if status == 403:
        return "Keenable access denied (HTTP 403); check the vault grant and account permissions"
    if status == 402:
        return "Keenable credits or payment required (HTTP 402); check account quota"
    if status == 429:
        return "Keenable rate limit reached (HTTP 429); retry later"
    return f"Keenable request failed (HTTP {status if isinstance(status, int) else 'unknown'})"


def _proxy(conn_id: str, method: str, path: str, *, json_body: Optional[dict] = None,
           params: Optional[dict] = None, timeout: int = 30) -> dict:
    body: dict = {"method": method, "path": path, "timeout": timeout}
    if json_body is not None:
        body["json"] = json_body
    if params is not None:
        body["params"] = params
    try:
        response = _vault_http(
            "POST", f"/api/vault/proxy/{conn_id}", body, timeout=timeout + 5,
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
        # Never surface arbitrary upstream response bodies (they can echo keys).
        raise ValueError(_status_error(status))
    if response.get("truncated"):
        raise ValueError("Keenable response exceeded vault response limit")
    data = response.get("json")
    if not isinstance(data, dict):
        raise ValueError("Invalid Keenable response")
    return data


class VaultKeenableWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "vault_keenable"

    @property
    def display_name(self) -> str:
        return "Keenable (Vault)"

    def is_available(self) -> bool:
        return _connection_id() is not None

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        conn_id = _connection_id()
        if not conn_id:
            return {"success": False, "error": "No ready, granted Keenable vault connection"}
        if not isinstance(query, str) or not query.strip():
            return {"success": False, "error": "Search query is required"}
        try:
            count = max(1, min(int(limit), 10))
        except (TypeError, ValueError):
            count = 5
        try:
            payload = _proxy(conn_id, "POST", "/v1/search",
                             json_body={"query": query, "mode": "pro", "max_results": count})
            hits = payload.get("results")
            if not isinstance(hits, list):
                raise ValueError("Invalid Keenable search results")
            results = []
            for hit in hits[:count]:
                if not isinstance(hit, dict):
                    continue
                url = _safe_target(hit.get("url"))
                if not url or check_website_access(url):
                    continue
                results.append({
                    "url": url,
                    "title": str(hit.get("title") or "")[:500],
                    "description": str(hit.get("description") or hit.get("snippet") or "")[:2000],
                    "content": str(hit.get("snippet") or "")[:10000],
                })
            return {"success": True, "data": {"web": results}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        conn_id = _connection_id()
        results: List[Dict[str, Any]] = []
        for original in urls[:_MAX_URLS]:
            entry: Dict[str, Any] = {"url": "", "title": "", "content": "", "raw_content": ""}
            url = await asyncio.to_thread(_safe_target, original)
            if url:
                entry["url"] = url
            if not url:
                entry["error"] = "Blocked: invalid, sensitive, or private URL"
            elif check_website_access(url):
                entry["error"] = "Blocked by website access policy"
            elif not conn_id:
                entry["error"] = "No ready, granted Keenable vault connection"
            else:
                try:
                    payload = await asyncio.to_thread(
                        _proxy, conn_id, "GET", "/v1/fetch",
                        params={"url": url, "live": True, "max_chars": 50000},
                        timeout=45,
                    )
                    final = await asyncio.to_thread(_safe_target, payload.get("url"))
                    if not final:
                        raise ValueError("Blocked: unsafe final URL")
                    if check_website_access(final):
                        raise ValueError("Blocked by website access policy")
                    content = payload.get("content")
                    if not isinstance(content, str):
                        raise ValueError("Keenable returned no content")
                    entry.update({
                        "url": final,
                        "title": str(payload.get("title") or "")[:500],
                        "content": content,
                        "raw_content": content,
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
            "name": "Keenable (Vault)",
            "badge": "vault · paid",
            "tag": "Pro search and live fetch using a granted vault connection.",
            "env_vars": [],
        }