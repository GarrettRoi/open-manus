"""Shared FAL.ai SDK plumbing.

Holds the stateless atoms that every FAL-backed tool needs:

* :func:`import_fal_client` — lazy import + ``lazy_deps`` integration so
  ``fal_client`` isn't pulled at cold start (it added ~64 ms per CLI
  invocation when imported eagerly).
* :class:`_ManagedFalSyncClient` — wrapper that drives a Nous-managed
  fal-queue gateway through the standard ``fal_client.SyncClient``
  primitives.
* :func:`_normalize_fal_queue_url_format`, :func:`_extract_http_status`
  — small helpers used by both the managed client wrapper and
  ``_submit_fal_request``.

Stateful pieces (cache globals, ``_managed_fal_client*`` selectors,
``_submit_fal_request``) intentionally stay on
:mod:`tools.image_generation_tool`. That module is the patch target for
existing test suites (``tests/tools/test_image_generation.py``,
``tests/tools/test_managed_media_gateways.py``) and for the
``plugins/image_gen/fal/`` plugin's ``_it`` indirection — moving the
caches here would silently defeat ``monkeypatch.setattr(image_tool,
"_managed_fal_client", None)`` because the lookups would go against
``fal_common``'s namespace instead. See the per-rule walkthrough at
issue #26241 for details.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode


def import_fal_client() -> Any:
    """Import ``fal_client`` (via ``lazy_deps`` when available) and return
    the module reference.

    Callers are responsible for caching the result on their own module
    global — keeping per-module globals lets tests monkey-patch the
    target module's ``fal_client`` attribute and have the patched value
    stick for that module's call sites.

    Raises :class:`ImportError` if the package is genuinely unavailable.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("image.fal", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))
    import fal_client  # type: ignore  # noqa: WPS433 — intentionally lazy
    return fal_client


def _normalize_fal_queue_url_format(queue_run_origin: str) -> str:
    normalized_origin = str(queue_run_origin or "").strip().rstrip("/")
    if not normalized_origin:
        raise ValueError("Managed FAL queue origin is required")
    return f"{normalized_origin}/"


class VaultFalRequestHandle:
    """Request handle for FAL jobs submitted through the vault proxy.

    Mimics the ``fal_client`` handle contract callers rely on: ``.get()``
    blocks until the queue job completes and returns the result payload.
    The FAL credential lives only in the vault; every HTTP call here goes
    through ``/api/vault/proxy/{conn_id}`` and the vault scrubs the stored
    key from responses.
    """

    _TERMINAL_ERROR_STATUSES = {"ERROR", "FAILED", "CANCELLED"}

    def __init__(
        self,
        vault_http: Any,
        conn_id: str,
        submit_response: Dict[str, Any],
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ):
        self._vault_http = vault_http
        self._conn_id = conn_id
        self._poll_interval = poll_interval
        self._timeout = timeout
        self.request_id = str(submit_response.get("request_id") or "")
        self._status_url = str(submit_response.get("status_url") or "")
        self._response_url = str(submit_response.get("response_url") or "")
        if not self.request_id or not self._response_url:
            raise ValueError(
                "Vault FAL submit returned no request_id/response_url — "
                f"unexpected queue response keys: {sorted(submit_response)}"
            )

    def _proxy(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None,
               timeout: float = 60.0) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"method": method, "path": path, "timeout": timeout}
        if json_body is not None:
            payload["json"] = json_body
        resp = self._vault_http(
            "POST", f"/api/vault/proxy/{self._conn_id}", payload,
            timeout=timeout + 15,
        )
        if not isinstance(resp, dict):
            raise ValueError(f"Vault proxy returned non-JSON response for {path}")
        if resp.get("error"):
            raise ValueError(f"Vault FAL proxy error: {resp['error']}")
        status = resp.get("status")
        if not isinstance(status, int) or status >= 400:
            detail = resp.get("json") or resp.get("text") or ""
            raise ValueError(
                f"FAL request via vault failed (HTTP {status}): {str(detail)[:500]}"
            )
        body = resp.get("json")
        if not isinstance(body, dict):
            raise ValueError(f"FAL returned a non-JSON body via vault for {path}")
        return body

    def get(self) -> Dict[str, Any]:
        """Block until the queue job finishes; return the result payload."""
        import time as _time
        deadline = _time.monotonic() + self._timeout
        status_path = self._status_url or f"{self._response_url}/status"
        while True:
            status_body = self._proxy("GET", status_path, timeout=30.0)
            state = str(status_body.get("status") or "").upper()
            if state == "COMPLETED":
                break
            if state in self._TERMINAL_ERROR_STATUSES:
                raise ValueError(
                    f"FAL request {self.request_id} ended in state {state}: "
                    f"{str(status_body)[:500]}"
                )
            if _time.monotonic() >= deadline:
                raise TimeoutError(
                    f"FAL request {self.request_id} did not complete within "
                    f"{int(self._timeout)}s (last state: {state or 'unknown'})"
                )
            _time.sleep(self._poll_interval)
        return self._proxy("GET", self._response_url, timeout=60.0)


def submit_fal_via_vault(
    vault_http: Any,
    conn_id: str,
    model: str,
    arguments: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> VaultFalRequestHandle:
    """Submit a FAL queue job through the vault proxy and return a handle.

    ``headers`` (e.g. the idempotency key) are forwarded as caller headers;
    the vault strips anything auth-ish and injects the stored key itself.
    """
    payload: Dict[str, Any] = {
        "method": "POST",
        "path": "/" + str(model).strip("/"),
        "json": arguments,
        "timeout": 60,
    }
    if headers:
        payload["headers"] = dict(headers)
    resp = vault_http(
        "POST", f"/api/vault/proxy/{conn_id}", payload, timeout=75,
    )
    if not isinstance(resp, dict):
        raise ValueError("Vault proxy returned a non-JSON response for FAL submit")
    if resp.get("error"):
        raise ValueError(f"Vault FAL proxy error: {resp['error']}")
    status = resp.get("status")
    if not isinstance(status, int) or status >= 400:
        detail = resp.get("json") or resp.get("text") or ""
        raise ValueError(
            f"FAL submit via vault failed (HTTP {status}): {str(detail)[:500]}"
        )
    body = resp.get("json")
    if not isinstance(body, dict):
        raise ValueError("FAL submit via vault returned a non-JSON body")
    return VaultFalRequestHandle(vault_http, conn_id, body)


def _extract_http_status(exc: BaseException) -> Optional[int]:
    """Return an HTTP status code from httpx/fal exceptions, else None.

    Defensive across exception shapes — httpx.HTTPStatusError exposes
    ``.response.status_code`` while fal_client wrappers may expose
    ``.status_code`` directly.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None


class _ManagedFalSyncClient:
    """Small per-instance wrapper around ``fal_client.SyncClient`` for
    managed queue hosts.

    The wrapper carries its own ``fal_client`` module reference instead
    of reaching into a module global, so callers stay in control of
    which module's ``fal_client`` is in scope (matters for the test
    patches that swap the legacy module's ``fal_client`` attribute).
    """

    def __init__(self, fal_client: Any, *, key: str, queue_run_origin: str):
        sync_client_class = getattr(fal_client, "SyncClient", None)
        if sync_client_class is None:
            raise RuntimeError("fal_client.SyncClient is required for managed FAL gateway mode")

        client_module = getattr(fal_client, "client", None)
        if client_module is None:
            raise RuntimeError("fal_client.client is required for managed FAL gateway mode")

        self._queue_url_format = _normalize_fal_queue_url_format(queue_run_origin)
        self._sync_client = sync_client_class(key=key)
        self._http_client = getattr(self._sync_client, "_client", None)
        self._maybe_retry_request = getattr(client_module, "_maybe_retry_request", None)
        self._raise_for_status = getattr(client_module, "_raise_for_status", None)
        self._request_handle_class = getattr(client_module, "SyncRequestHandle", None)
        self._add_hint_header = getattr(client_module, "add_hint_header", None)
        self._add_priority_header = getattr(client_module, "add_priority_header", None)
        self._add_timeout_header = getattr(client_module, "add_timeout_header", None)

        if self._http_client is None:
            raise RuntimeError("fal_client.SyncClient._client is required for managed FAL gateway mode")
        if self._maybe_retry_request is None or self._raise_for_status is None:
            raise RuntimeError("fal_client.client request helpers are required for managed FAL gateway mode")
        if self._request_handle_class is None:
            raise RuntimeError("fal_client.client.SyncRequestHandle is required for managed FAL gateway mode")

    def submit(
        self,
        application: str,
        arguments: Dict[str, Any],
        *,
        path: str = "",
        hint: Optional[str] = None,
        webhook_url: Optional[str] = None,
        priority: Any = None,
        headers: Optional[Dict[str, str]] = None,
        start_timeout: Optional[Union[int, float]] = None,
    ):
        url = self._queue_url_format + application
        if path:
            url += "/" + path.lstrip("/")
        if webhook_url is not None:
            url += "?" + urlencode({"fal_webhook": webhook_url})

        request_headers = dict(headers or {})
        if hint is not None and self._add_hint_header is not None:
            self._add_hint_header(hint, request_headers)
        if priority is not None:
            if self._add_priority_header is None:
                raise RuntimeError("fal_client.client.add_priority_header is required for priority requests")
            self._add_priority_header(priority, request_headers)
        if start_timeout is not None:
            if self._add_timeout_header is None:
                raise RuntimeError("fal_client.client.add_timeout_header is required for timeout requests")
            self._add_timeout_header(start_timeout, request_headers)

        response = self._maybe_retry_request(
            self._http_client,
            "POST",
            url,
            json=arguments,
            timeout=getattr(self._sync_client, "default_timeout", 120.0),
            headers=request_headers,
        )
        self._raise_for_status(response)

        data = response.json()
        return self._request_handle_class(
            request_id=data["request_id"],
            response_url=data["response_url"],
            status_url=data["status_url"],
            cancel_url=data["cancel_url"],
            client=self._http_client,
        )
