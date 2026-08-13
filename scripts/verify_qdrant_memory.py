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

With --provider it additionally exercises the full QdrantMemoryProvider
runtime path — registration/availability, real embeddings, memory_bank
(store + dedup), memory_recall, shared-write authorization gating, and
memory_forget — against the live backend. Requires an embedding credential
(OPENAI_API_KEY or QDRANT_EMBED_API_KEY) in the environment.

Run from a machine that can reach Qdrant (an agent container, or a temporary
Railway TCP proxy):

  QDRANT_URL=http://qdrant.railway.internal:6333 QDRANT_API_KEY=... \
      python3 scripts/verify_qdrant_memory.py [--provider]
"""
import json
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

    if "--provider" in sys.argv:
        verify_provider(url, key)
    print("ALL CHECKS PASSED")


def verify_provider(url: str, key: str):
    """Exercise the actual provider runtime against the live backend."""
    from plugins.memory import load_memory_provider

    probe = f"verify-prov-{uuid.uuid4().hex[:8]}"
    os.environ["QDRANT_AGENT_ID"] = probe
    os.environ.setdefault("QDRANT_ALLOW_SHARED_WRITE", "false")

    print(f"\nProvider runtime checks (agent identity: {probe})")
    provider = load_memory_provider("qdrant")
    check("provider loads via plugin loader", provider is not None)
    check("provider reports available", provider.is_available())
    provider.initialize("verify-session", platform="verify",
                        agent_context="primary", agent_identity="default")
    check("identity from env (not profile 'default')",
          provider._agent_id == probe)
    check("3 tools exposed", len(provider.get_tool_schemas()) == 3)

    marker = uuid.uuid4().hex[:10]
    fact = f"Verification probe fact {marker}: the owner's favorite verification color is teal."
    out = json.loads(provider.handle_tool_call(
        "memory_bank", {"content": fact, "importance": 4, "scope": "shared"}))
    check("memory_bank stores via real embeddings", "result" in out, str(out))
    check("unauthorized shared write downgraded to private",
          "private" in out.get("result", "") or "(agent scope)" in out.get("result", ""),
          out.get("result", ""))

    out = json.loads(provider.handle_tool_call(
        "memory_recall", {"query": f"favorite verification color {marker}"}))
    hits = out.get("results", [])
    check("memory_recall finds the banked fact",
          any(marker in h.get("memory", "") for h in hits),
          f"{out.get('count')} hits")
    mem_id = next(h["id"] for h in hits if marker in h.get("memory", ""))

    dup = json.loads(provider.handle_tool_call(
        "memory_bank", {"content": fact, "importance": 4}))
    check("near-duplicate rejected by dedup", "near-identical" in dup.get("result", ""),
          str(dup))

    out = json.loads(provider.handle_tool_call("memory_forget", {"memory_id": mem_id}))
    check("memory_forget deletes own memory", out.get("result") == "Memory deleted.")

    # Shared-delete authorization: plant a shared point directly, then verify
    # an UNauthorized provider cannot delete it while an authorized one can.
    shared_id = str(uuid.uuid4())
    vec = provider._embedder.embed([f"shared probe {marker}"])[0]
    provider._client.upsert(COLLECTION, [{
        "id": shared_id, "vector": vec,
        "payload": {"text": f"shared probe {marker}", "agent_id": "someone-else",
                    "scope": "shared", "importance": 3, "ts": time.time(),
                    "session_id": "verify", "platform": "verify",
                    "source": "verify", "superseded": False}}])
    out = json.loads(provider.handle_tool_call("memory_forget", {"memory_id": shared_id}))
    check("UNauthorized shared delete rejected",
          "not authorized" in out.get("error", ""), str(out))

    os.environ["QDRANT_ALLOW_SHARED_WRITE"] = "true"
    authorized = load_memory_provider("qdrant")
    authorized.initialize("verify-session-2", platform="verify",
                          agent_context="primary")
    out = json.loads(authorized.handle_tool_call("memory_forget", {"memory_id": shared_id}))
    check("authorized shared delete succeeds", out.get("result") == "Memory deleted.")
    authorized._stop.set()
    provider._stop.set()


if __name__ == "__main__":
    main()
