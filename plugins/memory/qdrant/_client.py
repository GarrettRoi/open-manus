"""Minimal Qdrant REST client + OpenAI-compatible embedding helper.

Uses plain ``requests`` so the fleet image needs no extra pip dependency
(qdrant-client pulls grpc/pydantic pins we don't want to fight).

All methods raise on HTTP/connection errors — the provider wraps calls in
its circuit breaker and treats every failure as best-effort.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15


class QdrantClient:
    """Tiny REST wrapper for the handful of Qdrant endpoints we use."""

    def __init__(self, url: str, api_key: str = ""):
        self.base = url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["api-key"] = api_key

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        resp = requests.request(
            method, f"{self.base}{path}", headers=self._headers,
            json=body, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def ensure_collection(self, name: str, vector_size: int) -> None:
        """Create the collection if it doesn't exist (idempotent)."""
        try:
            self._req("GET", f"/collections/{name}")
            return
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
        self._req("PUT", f"/collections/{name}", {
            "vectors": {"size": vector_size, "distance": "Cosine"},
        })
        # Payload indexes for the filters we always use.
        for field, schema in (
            ("agent_id", "keyword"), ("scope", "keyword"),
            ("superseded", "bool"), ("ts", "float"),
        ):
            try:
                self._req("PUT", f"/collections/{name}/index",
                          {"field_name": field, "field_schema": schema})
            except Exception:
                pass  # index creation is an optimization, never fatal

    def upsert(self, collection: str, points: List[Dict[str, Any]]) -> None:
        self._req("PUT", f"/collections/{collection}/points?wait=true",
                  {"points": points})

    def search(self, collection: str, vector: List[float], *,
               limit: int, flt: Optional[dict] = None) -> List[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "vector": vector, "limit": limit, "with_payload": True,
        }
        if flt:
            body["filter"] = flt
        return self._req("POST", f"/collections/{collection}/points/search",
                         body).get("result", [])

    def retrieve(self, collection: str, point_ids: List[str]) -> List[Dict[str, Any]]:
        return self._req("POST", f"/collections/{collection}/points",
                         {"ids": point_ids, "with_payload": True}).get("result", [])

    def set_payload(self, collection: str, point_ids: List[str],
                    payload: Dict[str, Any]) -> None:
        self._req("POST", f"/collections/{collection}/points/payload?wait=true",
                  {"points": point_ids, "payload": payload})

    def delete(self, collection: str, point_ids: List[str]) -> None:
        self._req("POST", f"/collections/{collection}/points/delete?wait=true",
                  {"points": point_ids})


class Embedder:
    """OpenAI-compatible /embeddings caller. Fail-soft: returns None on error."""

    def __init__(self, base_url: str, api_key: str, model: str, dim: int):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim

    @classmethod
    def from_env(cls) -> "Embedder":
        """Resolve the embedding backend.

        Precedence: explicit QDRANT_EMBED_* config, then OPENAI_API_KEY
        against the OpenAI API, then OPENROUTER_API_KEY against OpenRouter's
        OpenAI-compatible /embeddings endpoint (every fleet agent already
        carries a working OpenRouter key).
        """
        explicit_key = os.environ.get("QDRANT_EMBED_API_KEY", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        if explicit_key or openai_key or not openrouter_key:
            base = os.environ.get("QDRANT_EMBED_BASE_URL", "https://api.openai.com/v1")
            key = explicit_key or openai_key
            model = os.environ.get("QDRANT_EMBED_MODEL", "text-embedding-3-small")
        else:
            base = os.environ.get("QDRANT_EMBED_BASE_URL", "https://openrouter.ai/api/v1")
            key = openrouter_key
            model = os.environ.get("QDRANT_EMBED_MODEL", "openai/text-embedding-3-small")
        return cls(
            base_url=base,
            api_key=key,
            model=model,
            dim=int(os.environ.get("QDRANT_EMBED_DIM", "1536")),
        )

    def available(self) -> bool:
        return bool(self.api_key)

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """Embed a batch; returns None on any failure (caller degrades)."""
        if not self.api_key or not texts:
            return None
        try:
            resp = requests.post(
                f"{self.base}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "input": texts,
                      "dimensions": self.dim},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            out = [d.get("embedding") for d in
                   sorted(data, key=lambda d: d.get("index", 0))]
            if len(out) != len(texts) or any(v is None for v in out):
                return None
            return out
        except Exception as e:
            logger.debug("embedding call failed: %s", e)
            return None
