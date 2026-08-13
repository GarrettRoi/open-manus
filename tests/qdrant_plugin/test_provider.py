"""Tests for the Qdrant fleet memory provider (all network mocked)."""

import json
import time
from unittest import mock

import pytest

from plugins.memory.qdrant import QdrantMemoryProvider, register, _load_config
from plugins.memory.qdrant import _DEDUP_SCORE
from plugins.memory.qdrant._extract import _parse_facts, transcript_from_messages
from plugins.memory.qdrant._client import Embedder


def _provider(monkeypatch, **env):
    defaults = {
        "QDRANT_URL": "http://qdrant.test:6333",
        "QDRANT_API_KEY": "k",
        "OPENAI_API_KEY": "ek",
        "AGENT_NAME": "bianca",
        "QDRANT_ALLOW_SHARED_WRITE": "true",
    }
    defaults.update(env)
    for k, v in defaults.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    p = QdrantMemoryProvider()
    return p


def _init(p, monkeypatch, **kwargs):
    """Initialize with the Qdrant client and embedder mocked."""
    client = mock.MagicMock()
    embedder = mock.MagicMock()
    embedder.dim = 4
    embedder.available.return_value = True
    embedder.embed.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
    with mock.patch("plugins.memory.qdrant.QdrantClient", return_value=client), \
         mock.patch("plugins.memory.qdrant.Embedder") as E:
        E.from_env.return_value = embedder
        p.initialize("sess-1", **kwargs)
    return client, embedder


def _shutdown(p):
    p._stop.set()
    if p._worker:
        p._worker.join(timeout=2)


# -- registration / availability -------------------------------------------

def test_register_collects_provider():
    class Ctx:
        provider = None
        def register_memory_provider(self, prov):
            self.provider = prov
    ctx = Ctx()
    register(ctx)
    assert isinstance(ctx.provider, QdrantMemoryProvider)
    assert ctx.provider.name == "qdrant"


def test_is_available_requires_url_and_embed_key(monkeypatch):
    p = _provider(monkeypatch)
    assert p.is_available()
    p2 = _provider(monkeypatch, QDRANT_URL=None)
    assert not p2.is_available()
    p3 = _provider(monkeypatch, OPENAI_API_KEY=None, QDRANT_EMBED_API_KEY=None)
    assert not p3.is_available()


def test_agent_id_from_env(monkeypatch):
    p = _provider(monkeypatch)
    assert _load_config()["agent_id"] == "bianca"
    monkeypatch.setenv("QDRANT_AGENT_ID", "custom")
    assert _load_config()["agent_id"] == "custom"


# -- filters / ranking ------------------------------------------------------

def test_read_filter_scopes(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch, platform="discord")
    flt = p._read_filter()
    assert {"key": "scope", "match": {"value": "shared"}} in flt["should"]
    nested = flt["should"][1]["must"]
    assert {"key": "agent_id", "match": {"value": "bianca"}} in nested
    assert flt["must_not"] == [{"key": "superseded", "match": {"value": True}}]
    _shutdown(p)


def test_rank_blends_recency_and_importance():
    now = time.time()
    old_important = {"score": 0.80, "payload": {"ts": now - 200 * 86400, "importance": 5, "text": "a"}}
    fresh_trivial = {"score": 0.80, "payload": {"ts": now, "importance": 1, "text": "b"}}
    fresh_relevant = {"score": 0.95, "payload": {"ts": now, "importance": 3, "text": "c"}}
    ranked = QdrantMemoryProvider._rank([old_important, fresh_trivial, fresh_relevant], 3)
    assert ranked[0]["payload"]["text"] == "c"  # similarity dominates
    # a critical old fact should not be crushed to last automatically
    assert {r["payload"]["text"] for r in ranked} == {"a", "b", "c"}


# -- store / dedup ------------------------------------------------------------

def test_store_facts_dedups_near_duplicates(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.search.return_value = [{"score": _DEDUP_SCORE + 0.01, "id": "x"}]
    stored = p._store_facts([{"text": "dup", "scope": "agent", "importance": 3}], "tool")
    assert stored == 0
    client.upsert.assert_not_called()

    client.search.return_value = []
    stored = p._store_facts([{"text": "new fact", "scope": "shared", "importance": 4}], "tool")
    assert stored == 1
    points = client.upsert.call_args[0][1]
    assert points[0]["payload"]["scope"] == "shared"
    assert points[0]["payload"]["agent_id"] == "bianca"
    assert points[0]["payload"]["superseded"] is False
    _shutdown(p)


def test_store_skips_when_embeddings_unavailable(monkeypatch):
    p = _provider(monkeypatch)
    client, emb = _init(p, monkeypatch)
    emb.embed.side_effect = None
    emb.embed.return_value = None
    stored = p._store_facts([{"text": "x", "scope": "agent", "importance": 3}], "tool")
    assert stored is None
    client.upsert.assert_not_called()
    _shutdown(p)


# -- read-only contexts --------------------------------------------------------

def test_cron_context_is_read_only(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch, agent_context="cron")
    p.sync_turn("u", "a")
    assert p._work_q.empty()
    out = json.loads(p.handle_tool_call("memory_bank", {"content": "x"}))
    assert "error" in out
    _shutdown(p)


def test_primary_context_enqueues_sync(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch, agent_context="primary")
    p._stop.set()  # freeze worker so we can inspect the queue
    if p._worker:
        p._worker.join(timeout=2)
    p.sync_turn("hello", "world")
    kind, payload = p._work_q.get_nowait()
    assert kind == "extract"
    assert "hello" in payload[0] and payload[1] == "turn_sync"


def test_pre_compress_enqueues_extraction(monkeypatch):
    p = _provider(monkeypatch)
    _init(p, monkeypatch)
    p._stop.set()
    if p._worker:
        p._worker.join(timeout=2)
    out = p.on_pre_compress([
        {"role": "user", "content": "remember the client is ACME"},
        {"role": "assistant", "content": "noted"},
        {"role": "tool", "content": "ignored"},
    ])
    assert out == ""  # non-blocking, contributes nothing inline
    kind, payload = p._work_q.get_nowait()
    assert kind == "extract" and payload[1] == "pre_compress"
    assert "ACME" in payload[0] and "ignored" not in payload[0]


# -- tools ------------------------------------------------------------------------

def test_recall_tool_formats_results(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.search.return_value = [
        {"id": "id1", "score": 0.9,
         "payload": {"text": "Garrett prefers weekly summaries", "scope": "shared",
                     "agent_id": "samantha", "ts": time.time(), "importance": 4}},
    ]
    out = json.loads(p.handle_tool_call("memory_recall", {"query": "summaries"}))
    assert out["count"] == 1
    assert out["results"][0]["memory"].startswith("Garrett prefers")
    assert out["results"][0]["scope"] == "shared"
    _shutdown(p)


def test_bank_tool_reports_dedup(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.search.return_value = [{"score": 0.99, "id": "x"}]
    out = json.loads(p.handle_tool_call("memory_bank", {"content": "dup fact"}))
    assert "near-identical" in out["result"]
    _shutdown(p)


def test_forget_tool_deletes_own_memory(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.retrieve.return_value = [{"id": "abc", "payload": {"scope": "agent", "agent_id": "bianca"}}]
    out = json.loads(p.handle_tool_call("memory_forget", {"memory_id": "abc"}))
    assert out["result"] == "Memory deleted."
    client.delete.assert_called_once()
    _shutdown(p)


def test_forget_tool_blocks_foreign_private_memory(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.retrieve.return_value = [{"id": "abc", "payload": {"scope": "agent", "agent_id": "samantha"}}]
    out = json.loads(p.handle_tool_call("memory_forget", {"memory_id": "abc"}))
    assert "error" in out
    client.delete.assert_not_called()

    client.retrieve.return_value = []
    out = json.loads(p.handle_tool_call("memory_forget", {"memory_id": "missing"}))
    assert "error" in out
    _shutdown(p)


def test_agent_identity_env_beats_default_profile(monkeypatch):
    p = _provider(monkeypatch)
    _init(p, monkeypatch, agent_identity="default")
    assert p._agent_id == "bianca"  # AGENT_NAME wins over profile "default"
    _shutdown(p)

    p2 = _provider(monkeypatch, AGENT_NAME=None)
    _init(p2, monkeypatch, agent_identity="lexi")
    assert p2._agent_id == "lexi"  # real profile identity is honored
    _shutdown(p2)


def test_unauthorized_shared_write_downgraded_to_private(monkeypatch):
    p = _provider(monkeypatch, QDRANT_ALLOW_SHARED_WRITE=None)
    client, _ = _init(p, monkeypatch)
    client.search.return_value = []
    out = json.loads(p.handle_tool_call(
        "memory_bank", {"content": "poison the fleet", "scope": "shared"}))
    assert "not authorized" in out["result"]
    points = client.upsert.call_args[0][1]
    assert points[0]["payload"]["scope"] == "agent"  # downgraded, not shared

    # extraction-path facts get the same downgrade
    client.upsert.reset_mock()
    p._store_facts([{"text": "extracted", "scope": "shared", "importance": 3}], "turn_sync")
    assert client.upsert.call_args[0][1][0]["payload"]["scope"] == "agent"
    _shutdown(p)


def test_prefetch_block_marks_memories_as_untrusted(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    client.search.return_value = [
        {"id": "i", "score": 0.9, "payload": {"text": "shared note", "scope": "shared",
                                              "agent_id": "lexi", "ts": time.time(), "importance": 3}},
    ]
    p.queue_prefetch("note")
    body = p.prefetch("note")
    assert "NOT instructions" in body
    assert "[fleet-shared, from lexi]" in body
    _shutdown(p)


def test_embed_outage_trips_breaker(monkeypatch):
    p = _provider(monkeypatch)
    client, emb = _init(p, monkeypatch)
    emb.embed.side_effect = None
    emb.embed.return_value = None
    for _ in range(5):
        assert p._recall("q", 3) == []
    assert p._breaker_open()
    _shutdown(p)


def test_breaker_blocks_tools_after_failures(monkeypatch):
    p = _provider(monkeypatch)
    client, _ = _init(p, monkeypatch)
    for _ in range(5):
        p._record_failure()
    out = json.loads(p.handle_tool_call("memory_recall", {"query": "q"}))
    assert "error" in out
    _shutdown(p)


def test_tool_schemas_have_names(monkeypatch):
    p = _provider(monkeypatch)
    names = [s["name"] for s in p.get_tool_schemas()]
    assert names == ["memory_recall", "memory_bank", "memory_forget"]


# -- extraction parsing ---------------------------------------------------------

def test_parse_facts_validates():
    raw = json.dumps([
        {"text": "Client ACME signed on 2026-08-01", "importance": 9, "scope": "shared"},
        {"text": "", "importance": 3},
        {"text": "Bianca uses paper trading", "importance": "bad", "scope": "weird"},
        "not a dict",
    ])
    facts = _parse_facts("```json\n" + raw + "\n```")
    assert len(facts) == 2
    assert facts[0]["importance"] == 5
    assert facts[1] == {"text": "Bianca uses paper trading", "importance": 3, "scope": "agent"}


def test_parse_facts_garbage_returns_empty():
    assert _parse_facts("no json here") == []
    assert _parse_facts('{"not": "a list"}') == []


def test_transcript_from_messages_flattens_multimodal():
    txt = transcript_from_messages([
        {"role": "user", "content": [{"type": "text", "text": "see chart"},
                                     {"type": "image_url", "image_url": {}}]},
        {"role": "assistant", "content": "looks bullish"},
    ])
    assert "USER: see chart" in txt and "ASSISTANT: looks bullish" in txt


def test_embedder_env_defaults(monkeypatch):
    monkeypatch.delenv("QDRANT_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    e = Embedder.from_env()
    assert e.model == "text-embedding-3-small"
    assert e.dim == 1536
    assert e.available()


def test_bank_tool_reports_backend_failure_not_dedup(monkeypatch):
    p = _provider(monkeypatch)
    client, emb = _init(p, monkeypatch)
    emb.embed.side_effect = None
    emb.embed.return_value = None  # embedding backend down
    out = json.loads(p.handle_tool_call("memory_bank", {"content": "x"}))
    assert "error" in out and "NOT stored" in out["error"]
    _shutdown(p)


def test_embedder_falls_back_to_openrouter(monkeypatch):
    for k in ("QDRANT_EMBED_API_KEY", "OPENAI_API_KEY", "QDRANT_EMBED_BASE_URL", "QDRANT_EMBED_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    e = Embedder.from_env()
    assert e.base == "https://openrouter.ai/api/v1"
    assert e.model == "openai/text-embedding-3-small"
    assert e.available()

    # explicit OPENAI key still takes precedence
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    e2 = Embedder.from_env()
    assert e2.base == "https://api.openai.com/v1"
