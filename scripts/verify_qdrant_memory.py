#!/usr/bin/env python3
"""
Live end-to-end verification of the Qdrant fleet memory backend.

Authenticates with the real Qdrant service, ensures the collection, writes
one agent-scoped and one shared memory for a probe agent, then verifies:
  * the probe agent's read filter recalls both,
  * a *different* agent's read filter sees the shared memory but NOT the
    probe agent's private one (scope isolation),
  * an unauthenticated request is rejected (when an API key is set),
then deletes the probe points.

Run from a machine that can reach Qdrant (an agent container, or a temporary
Railway TCP proxy):

  QDRANT_URL=http://qdrant.railway.internal:6333 QDRANT_API_KEY=... \
      python3 scripts/verify_qdrant_memory.py
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from plugins.memory.qdrant._client import QdrantClient  # noqa: E402

COLLECTION = os.environ.get("QDRANT_COLLECTION", "fleet_memory")
DIM = int(os.environ.get("QDRANT_EMBED_DIM", "1536"))


def read_filter(agent_id: str) -> dict:
    return {
        "should": [
            {"key": "scope", "match": {"value": "shared"}},
            {"must": [
                {"key": "scope", "match": {"value": "agent"}},
                {"key": "agent_id", "match": {"value": agent_id}},
            ]},
        ],
        "must_not": [{"key": "superseded", "match": {"value": True}}],
    }


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(f"FAILED: {name}")


def main():
    url = os.environ.get("QDRANT_URL")
    key = os.environ.get("QDRANT_API_KEY", "")
    if not url:
        raise SystemExit("QDRANT_URL is required")
    client = QdrantClient(url, key)
    probe = f"verify-{uuid.uuid4().hex[:8]}"
    other = f"{probe}-other"
    vec = [0.017] * DIM  # deterministic probe vector

    print(f"Verifying {url} collection={COLLECTION} probe_agent={probe}")

    client.ensure_collection(COLLECTION, DIM)
    check("authenticated collection ensure", True)

    if key:
        try:
            QdrantClient(url, "wrong-key").search(COLLECTION, vec, limit=1)
            check("unauthenticated request rejected", False, "wrong key accepted!")
        except Exception:
            check("unauthenticated request rejected", True)

    ids = {"private": str(uuid.uuid4()), "shared": str(uuid.uuid4())}
    now = time.time()
    client.upsert(COLLECTION, [
        {"id": ids["private"], "vector": vec,
         "payload": {"text": "probe private fact", "agent_id": probe,
                     "scope": "agent", "importance": 3, "ts": now,
                     "session_id": "verify", "platform": "verify",
                     "source": "verify", "superseded": False}},
        {"id": ids["shared"], "vector": vec,
         "payload": {"text": "probe shared fact", "agent_id": probe,
                     "scope": "shared", "importance": 3, "ts": now,
                     "session_id": "verify", "platform": "verify",
                     "source": "verify", "superseded": False}},
    ])
    check("write (agent + shared points)", True)

    own = client.search(COLLECTION, vec, limit=50, flt=read_filter(probe))
    own_ids = {h["id"] for h in own}
    check("own recall sees private fact", ids["private"] in own_ids)
    check("own recall sees shared fact", ids["shared"] in own_ids)

    foreign = client.search(COLLECTION, vec, limit=50, flt=read_filter(other))
    foreign_ids = {h["id"] for h in foreign}
    check("foreign agent sees shared fact", ids["shared"] in foreign_ids)
    check("foreign agent CANNOT see private fact",
          ids["private"] not in foreign_ids)

    client.delete(COLLECTION, list(ids.values()))
    check("cleanup (probe points deleted)", True)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
