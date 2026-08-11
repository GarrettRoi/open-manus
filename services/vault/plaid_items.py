"""Plaid Item (bank connection) storage for the Open Manus Key Vault.

A Plaid *Item* is one institution login (Discover, Capital One, ...) obtained
through the Plaid Link flow.  Multiple Items can live under one Plaid
connection; each Item's access token is Fernet-encrypted with the vault
master key and never leaves the vault — the proxy injects it server-side.

Redis layout:
  vault:plaid_item:{CONN_ID}:{ITEM_KEY}  hash: institution_name,
        institution_id, item_id, access_enc (encrypted token), status,
        created_at, updated_at
  vault:plaid_items:{CONN_ID}            zset index of item keys
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from connections import normalize_id

logger = logging.getLogger("vault.plaid_items")

PFX_ITEM = "vault:plaid_item:"
PFX_ITEM_INDEX = "vault:plaid_items:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlaidItemStore:
    def __init__(self, redis_client, encrypt, decrypt):
        self.r = redis_client
        self.encrypt = encrypt
        self.decrypt = decrypt

    def _key(self, conn_id: str, item_key: str) -> str:
        return f"{PFX_ITEM}{normalize_id(conn_id)}:{item_key}"

    def list_keys(self, conn_id: str) -> List[str]:
        return self.r.zrange(f"{PFX_ITEM_INDEX}{normalize_id(conn_id)}", 0, -1)

    def get(self, conn_id: str, item_key: str) -> Optional[Dict[str, Any]]:
        """Item metadata WITHOUT the access token."""
        data = self.r.hgetall(self._key(conn_id, item_key))
        if not data:
            return None
        data.pop("access_enc", None)
        data["key"] = item_key
        return data

    def list_items(self, conn_id: str) -> List[Dict[str, Any]]:
        out = []
        for k in self.list_keys(conn_id):
            item = self.get(conn_id, k)
            if item:
                out.append(item)
            else:
                self.r.zrem(f"{PFX_ITEM_INDEX}{normalize_id(conn_id)}", k)
        return out

    def get_access_token(self, conn_id: str, item_key: str) -> Optional[str]:
        enc = self.r.hget(self._key(conn_id, item_key), "access_enc")
        if not enc:
            return None
        try:
            return self.decrypt(enc)
        except Exception:
            logger.exception("Failed to decrypt Plaid item token %s/%s",
                             conn_id, item_key)
            return None

    def all_access_tokens(self, conn_id: str) -> List[str]:
        """All decryptable tokens for a connection (for response scrubbing)."""
        out = []
        for k in self.list_keys(conn_id):
            tok = self.get_access_token(conn_id, k)
            if tok:
                out.append(tok)
        return out

    def save(self, conn_id: str, *, institution_name: str = "",
             institution_id: str = "", item_id: str, access_token: str,
             status: str = "active") -> str:
        """Persist an Item; returns its key. Same item_id updates in place."""
        # Reuse the existing key when this item_id is already stored
        # (e.g. update-mode re-auth or a duplicate Link of the same login).
        item_key = None
        for k in self.list_keys(conn_id):
            if self.r.hget(self._key(conn_id, k), "item_id") == item_id:
                item_key = k
                break
        if not item_key:
            slug = normalize_id(institution_name or institution_id or "BANK")[:32] or "BANK"
            item_key = slug
            n = 2
            while self.r.exists(self._key(conn_id, item_key)):
                item_key = f"{slug}_{n}"
                n += 1
        existing = self.r.exists(self._key(conn_id, item_key))
        mapping = {
            "institution_name": institution_name or institution_id or item_key,
            "institution_id": institution_id,
            "item_id": item_id,
            "access_enc": self.encrypt(access_token),
            "status": status,
            "updated_at": _now_iso(),
        }
        if not existing:
            mapping["created_at"] = _now_iso()
        self.r.hset(self._key(conn_id, item_key), mapping=mapping)
        self.r.zadd(f"{PFX_ITEM_INDEX}{normalize_id(conn_id)}", {item_key: time.time()})
        return item_key

    def set_status(self, conn_id: str, item_key: str, status: str) -> None:
        if self.r.exists(self._key(conn_id, item_key)):
            self.r.hset(self._key(conn_id, item_key), mapping={
                "status": status, "updated_at": _now_iso()})

    def delete(self, conn_id: str, item_key: str) -> None:
        self.r.delete(self._key(conn_id, item_key))
        self.r.zrem(f"{PFX_ITEM_INDEX}{normalize_id(conn_id)}", item_key)

    def delete_all(self, conn_id: str) -> None:
        for k in self.list_keys(conn_id):
            self.r.delete(self._key(conn_id, k))
        self.r.delete(f"{PFX_ITEM_INDEX}{normalize_id(conn_id)}")

    def find(self, conn_id: str, ref: str) -> Optional[Dict[str, Any]]:
        """Resolve an Item by key, item_id, institution_id, or name."""
        q = (ref or "").strip()
        if not q:
            return None
        qkey = normalize_id(q)
        if qkey:
            item = self.get(conn_id, qkey)
            if item:
                return item
        ql = q.lower()
        for k in self.list_keys(conn_id):
            item = self.get(conn_id, k)
            if not item:
                continue
            if q == item.get("item_id") or ql == (item.get("institution_id") or "").lower():
                return item
            if ql in (item.get("institution_name") or "").lower():
                return item
        return None
