"""Tests for Discord-native inter-agent dispatch.

Covers the Redis store (tools/agent_dispatch.py) — chain lifecycle, loop
rails (depth / fan-out / self-dispatch / unknown agent), question-answer
flow, completion with open sub-tasks — and the adapter-side gate logic in
plugins/platforms/discord/dispatch.py.

All Redis I/O uses fakeredis.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import agent_dispatch as store  # noqa: E402


def _r() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def _seed_roster(r, *agents: str) -> None:
    for a in agents:
        r.set(f"dispatch:roster:{a}", json.dumps({
            "agent": a, "discord_user_id": f"9{hash(a) % 10**6}",
            "role": "", "tools": ["vault_x"],
        }))


# ---------------------------------------------------------------------------
# Chain creation + rails
# ---------------------------------------------------------------------------

def test_create_chain_basic():
    r = _r()
    _seed_roster(r, "aria", "lexi")
    chain = store.create_chain(r, "aria", "lexi", "do the thing")
    assert chain["status"] == "pending"
    assert chain["from"] == "aria" and chain["to"] == "lexi"
    assert chain["root_id"] == chain["id"]
    assert r.sismember("dispatch:active", chain["id"])
    # dispatcher's outbox got the open_chain action
    out = json.loads(r.lrange("dispatch:outbox:aria", 0, -1)[0])
    assert out == {"kind": "open_chain", "chain_id": chain["id"]}


def test_self_dispatch_rejected():
    r = _r()
    _seed_roster(r, "aria")
    with pytest.raises(RuntimeError, match="yourself"):
        store.create_chain(r, "aria", "aria", "loop")


def test_unknown_agent_rejected():
    r = _r()
    _seed_roster(r, "aria")
    with pytest.raises(RuntimeError, match="Unknown agent"):
        store.create_chain(r, "aria", "ghost", "task")


def test_depth_rail():
    r = _r()
    _seed_roster(r, "a", "b", "c", "d")
    c1 = store.create_chain(r, "a", "b", "t1")
    c2 = store.create_chain(r, "b", "c", "t2", parent_id=c1["id"])
    assert c2["depth"] == 1 and c2["root_id"] == c1["id"]
    with patch.dict("os.environ", {"DISPATCH_MAX_DEPTH": "2"}):
        with pytest.raises(RuntimeError, match="depth limit"):
            store.create_chain(r, "c", "d", "t3", parent_id=c2["id"])


def test_fanout_rail():
    r = _r()
    _seed_roster(r, "a", "b", "c")
    parent = store.create_chain(r, "a", "b", "root")
    with patch.dict("os.environ", {"DISPATCH_MAX_FANOUT": "2"}):
        store.create_chain(r, "b", "c", "s1", parent_id=parent["id"])
        store.create_chain(r, "b", "c", "s2", parent_id=parent["id"])
        with pytest.raises(RuntimeError, match="Fan-out limit"):
            store.create_chain(r, "b", "c", "s3", parent_id=parent["id"])


def test_auto_parent_inference_while_working_an_order():
    """An agent working a dispatched order can't escape the rails by omitting
    parent_chain_id — its dispatch auto-attaches as a sub-task."""
    r = _r()
    _seed_roster(r, "a", "b", "c")
    order = store.create_chain(r, "a", "b", "root order")
    store.mark_working(r, order["id"], "b")
    sub = store.create_chain(r, "b", "c", "sub work")  # no parent given
    assert sub["parent_id"] == order["id"]
    assert sub["depth"] == 1


def test_parent_requires_participation():
    r = _r()
    _seed_roster(r, "a", "b", "c", "d")
    chain = store.create_chain(r, "a", "b", "t")
    with pytest.raises(RuntimeError, match="not a participant"):
        store.create_chain(r, "c", "d", "hijack", parent_id=chain["id"])


def test_cancel_requires_participant_or_owner():
    r = _r()
    _seed_roster(r, "a", "b", "c")
    chain = store.create_chain(r, "a", "b", "t")
    with pytest.raises(RuntimeError, match="may cancel"):
        store.cancel_chain(r, chain["id"], "c")
    assert store.cancel_chain(r, chain["id"], "owner")["status"] == "cancelled"


def test_guarded_save_rejects_terminal_overwrite():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    store.complete_chain(r, chain["id"], "b", "done")
    stale = dict(chain)
    stale["status"] = "acked"
    with pytest.raises(RuntimeError, match="already done"):
        store.save_chain_guarded(r, stale, {"pending"})


def test_multi_agent_root_tracks_participants():
    r = _r()
    _seed_roster(r, "a", "b", "c")
    root = store.create_chain(r, "a", "b", "root")
    store.create_chain(r, "b", "c", "sub", parent_id=root["id"])
    root2 = store.get_chain(r, root["id"])
    assert set(root2["agents"]) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Lifecycle: working / question / answer / complete
# ---------------------------------------------------------------------------

def test_working_only_assignee():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    with pytest.raises(RuntimeError, match="assigned to b"):
        store.mark_working(r, chain["id"], "a")
    chain = store.mark_working(r, chain["id"], "b")
    assert chain["status"] == "working"
    reacts = [json.loads(x) for x in r.lrange("dispatch:outbox:b", 0, -1)]
    assert any(a.get("kind") == "react" and a.get("add") == "🔧" for a in reacts)


def test_question_answer_flow_agent():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    store.mark_working(r, chain["id"], "b")
    chain = store.ask_question(r, chain["id"], "b", "a", "which env?")
    assert chain["status"] == "waiting" and chain["waiting_on"] == "a"
    # question event delivered to a's inbox
    events = [json.loads(x) for x in r.lrange("dispatch:inbox:a", 0, -1)]
    assert any(e["kind"] == "question" for e in events)
    # wrong agent can't answer
    with pytest.raises(RuntimeError, match="addressed to"):
        store.answer_question(r, chain["id"], "b", "nope")
    chain = store.answer_question(r, chain["id"], "a", "prod")
    assert chain["status"] == "working" and chain["waiting_on"] == ""
    events = [json.loads(x) for x in r.lrange("dispatch:inbox:b", 0, -1)]
    assert any(e["kind"] == "answer" and e["answer"] == "prod" for e in events)


def test_question_to_owner():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    chain = store.ask_question(r, chain["id"], "b", "garrett", "ok to delete?")
    assert chain["waiting_on"] == "owner"


def test_complete_blocked_by_open_children():
    r = _r()
    _seed_roster(r, "a", "b", "c")
    parent = store.create_chain(r, "a", "b", "root")
    child = store.create_chain(r, "b", "c", "sub", parent_id=parent["id"])
    with pytest.raises(RuntimeError, match="open sub-tasks"):
        store.complete_chain(r, parent["id"], "b", "done")
    store.complete_chain(r, child["id"], "c", "sub done")
    chain = store.complete_chain(r, parent["id"], "b", "done")
    assert chain["status"] == "done"
    assert not r.sismember("dispatch:active", parent["id"])
    done_events = [json.loads(x) for x in r.lrange("dispatch:inbox:a", 0, -1)]
    assert any(e["kind"] == "completed" and e["success"] for e in done_events)


def test_complete_failure_flag():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    chain = store.complete_chain(r, chain["id"], "b", "could not", success=False)
    assert chain["status"] == "failed"


def test_cancel_notifies_assignee():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    chain = store.cancel_chain(r, chain["id"], "a", "obsolete")
    assert chain["status"] == "cancelled"
    events = [json.loads(x) for x in r.lrange("dispatch:inbox:b", 0, -1)]
    assert any(e["kind"] == "cancelled" for e in events)


def test_double_complete_rejected():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    store.complete_chain(r, chain["id"], "b", "done")
    with pytest.raises(RuntimeError, match="already done"):
        store.complete_chain(r, chain["id"], "b", "again")


def test_list_chains_for_prunes_stale_active_ids():
    r = _r()
    _seed_roster(r, "a", "b")
    chain = store.create_chain(r, "a", "b", "t")
    r.delete(f"dispatch:chain:{chain['id']}")  # simulate TTL expiry
    assert store.list_chains_for(r, "a") == []
    assert not r.sismember("dispatch:active", chain["id"])


# ---------------------------------------------------------------------------
# Tool entrypoint smoke tests
# ---------------------------------------------------------------------------

def test_tool_dispatch_and_roster():
    r = _r()
    _seed_roster(r, "aria", "lexi")
    with patch.object(store, "_redis", return_value=r), \
         patch.dict("os.environ", {"AGENT_NAME": "aria"}):
        out = json.loads(store.agent_dispatch_tool({"action": "roster"}))
        assert {e["agent"] for e in out["roster"]} == {"aria", "lexi"}
        out = json.loads(store.agent_dispatch_tool(
            {"action": "dispatch", "to": "lexi", "task": "audit the vault"}))
        assert out["dispatched"] and out["chain_id"]
        out = json.loads(store.agent_dispatch_tool(
            {"action": "status", "chain_id": out["chain_id"]}))
        assert out["chain"]["to"] == "lexi"


def test_tool_error_paths():
    r = _r()
    with patch.object(store, "_redis", return_value=r), \
         patch.dict("os.environ", {"AGENT_NAME": "aria"}):
        out = json.loads(store.agent_dispatch_tool({"action": "dispatch"}))
        assert "error" in out
        out = json.loads(store.agent_dispatch_tool({"action": "bogus"}))
        assert "error" in out


# ---------------------------------------------------------------------------
# Adapter-side gate
# ---------------------------------------------------------------------------

def _mk_manager(agent="lexi", channel="777", owner="700339484507766826"):
    import importlib
    disp = importlib.import_module("plugins.platforms.discord.dispatch")
    with patch.dict("os.environ", {"AGENT_NAME": agent,
                                   "DISPATCH_CHANNEL_ID": channel,
                                   "REDIS_URL": "redis://x"}):
        mgr = disp.DispatchManager(MagicMock())
    mgr.agent = agent
    mgr.channel_id = channel
    return disp, mgr


def _msg(channel_id, author_id, bot=False):
    m = MagicMock()
    m.channel.id = int(channel_id)
    m.author.id = int(author_id)
    m.author.bot = bot
    return m


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_gate_ignores_non_dispatch_channels():
    disp, mgr = _mk_manager()
    with patch.object(type(mgr), "enabled", property(lambda s: True)):
        assert _run(mgr.gate(_msg("123", "1"), None, False)) is None


def test_gate_drops_non_owner_in_dispatch_thread():
    disp, mgr = _mk_manager()
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"):
        assert _run(mgr.gate(_msg("555", "99"), "777", True)) == "drop"


def test_gate_owner_root_channel_normal():
    disp, mgr = _mk_manager()
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"):
        assert _run(mgr.gate(_msg("777", "42"), None, False)) is None


def test_gate_owner_steer_routes_to_assignee_only():
    r = _r()
    chain = {"id": "5", "root_id": "5", "parent_id": "", "depth": 0,
             "from": "aria", "to": "lexi", "task": "t", "status": "working",
             "thread_id": "555", "order_message_id": "1", "agents": ["aria", "lexi"],
             "waiting_on": "", "question": "", "result": "", "created_at": 1}
    store.save_chain(r, chain)
    r.set("dispatch:thread:555", "5")

    fake_store = MagicMock()
    fake_store._redis.return_value = r
    fake_store.get_chain = store.get_chain
    fake_store.save_chain = store.save_chain
    fake_store.save_chain_guarded = store.save_chain_guarded

    disp, mgr = _mk_manager(agent="lexi")
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"), \
         patch.object(disp, "_store", return_value=fake_store):
        assert _run(mgr.gate(_msg("555", "42"), "777", True)) == "steer"
    # non-assignee agent drops the same steering message
    disp2, mgr2 = _mk_manager(agent="aria")
    with patch.object(type(mgr2), "enabled", property(lambda s: True)), \
         patch.object(disp2, "_owner_id", return_value="42"), \
         patch.object(disp2, "_store", return_value=fake_store):
        assert _run(mgr2.gate(_msg("555", "42"), "777", True)) == "drop"


def test_owner_steering_bypass_fail_closed():
    """The on_message allowlist exception admits ONLY the configured owner,
    ONLY in dispatch territory (channel or its threads)."""
    disp, mgr = _mk_manager(channel="777")
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"):
        # owner in dispatch channel root / in a thread under it (parent id in set)
        assert mgr.owner_steering_bypass("42", {"777"}) is True
        assert mgr.owner_steering_bypass("42", {"555", "777"}) is True
        # non-owner never passes
        assert mgr.owner_steering_bypass("99", {"777"}) is False
        # owner outside dispatch territory never passes
        assert mgr.owner_steering_bypass("42", {"123"}) is False
        # DMs (no channel-id set) never pass
        assert mgr.owner_steering_bypass("42", None) is False
        assert mgr.owner_steering_bypass("", {"777"}) is False
    # no owner configured → fail closed
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value=""):
        assert mgr.owner_steering_bypass("42", {"777"}) is False
    # dispatch disabled → fail closed
    with patch.object(type(mgr), "enabled", property(lambda s: False)), \
         patch.object(disp, "_owner_id", return_value="42"):
        assert mgr.owner_steering_bypass("42", {"777"}) is False


def test_adapter_dispatch_owner_bypass_helper():
    """Adapter-side wrapper: no manager / manager error → fail closed."""
    import importlib
    _ensure_pkg = importlib.import_module("plugins.platforms.discord.dispatch")
    from plugins.platforms.discord.adapter import DiscordAdapter

    fake_adapter = MagicMock(spec=[])  # bare object
    fake_adapter.name = "test"
    fake_adapter._dispatch_manager = None
    assert DiscordAdapter._dispatch_owner_bypass(
        fake_adapter, _msg("777", "42"), {"777"}) is False

    mgr = MagicMock()
    mgr.owner_steering_bypass.return_value = True
    fake_adapter._dispatch_manager = mgr
    assert DiscordAdapter._dispatch_owner_bypass(
        fake_adapter, _msg("777", "42"), {"777"}) is True
    mgr.owner_steering_bypass.assert_called_once_with("42", {"777"})

    mgr.owner_steering_bypass.side_effect = RuntimeError("redis down")
    assert DiscordAdapter._dispatch_owner_bypass(
        fake_adapter, _msg("777", "42"), {"777"}) is False


def test_gate_drops_bot_authored_protocol_posts():
    """Bot-authored posts in dispatch threads (order posts, results) must
    never trigger a turn even if bot filtering is loosened: the gate drops
    every non-owner author, bots included."""
    disp, mgr = _mk_manager()
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"):
        assert _run(mgr.gate(_msg("555", "888", bot=True), "777", True)) == "drop"


def test_gate_owner_answer_resumes_asker():
    r = _r()
    chain = {"id": "6", "root_id": "6", "parent_id": "", "depth": 0,
             "from": "aria", "to": "lexi", "task": "t", "status": "waiting",
             "thread_id": "556", "order_message_id": "1", "agents": ["aria", "lexi"],
             "waiting_on": "owner", "asked_by": "lexi", "question": "q",
             "result": "", "created_at": 1}
    store.save_chain(r, chain)
    r.set("dispatch:thread:556", "6")

    fake_store = MagicMock()
    fake_store._redis.return_value = r
    fake_store.get_chain = store.get_chain
    fake_store.save_chain = store.save_chain
    fake_store.save_chain_guarded = store.save_chain_guarded

    disp, mgr = _mk_manager(agent="lexi")
    with patch.object(type(mgr), "enabled", property(lambda s: True)), \
         patch.object(disp, "_owner_id", return_value="42"), \
         patch.object(disp, "_store", return_value=fake_store):
        assert _run(mgr.gate(_msg("556", "42"), "777", True)) == "steer"
    updated = store.get_chain(r, "6")
    assert updated["status"] == "working" and updated["waiting_on"] == ""


def test_roster_tools_filters_registry(monkeypatch):
    """Roster publishes only granted vault_* tools + core dispatch tools,
    never the full registry dump."""
    from unittest.mock import MagicMock
    from plugins.platforms.discord.dispatch import DispatchManager

    mgr = DispatchManager(adapter=MagicMock())

    class _E:
        def __init__(self, name): self.name = name

    fake_names = [
        "terminal", "read_file", "web_search", "browser",  # local noise
        "vault_openai", "vault_gmail_main",                # granted vault tools
        "agent_dispatch", "vault", "ask_owner", "request_dev_modification",
    ]

    class _Reg:
        def snapshot(self):
            return [_E(n) for n in fake_names], None

    import plugins.platforms.discord.dispatch as dmod
    import tools.registry as regmod
    monkeypatch.setattr(regmod, "registry", _Reg())

    tools = mgr._roster_tools()
    assert tools == [
        "vault_gmail_main", "vault_openai",
        "agent_dispatch", "vault", "ask_owner", "request_dev_modification",
    ]
    assert "terminal" not in tools and "web_search" not in tools
