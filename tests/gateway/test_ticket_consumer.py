"""All ticket runtime tests use isolated in-memory Redis and no model/API."""
import asyncio

import fakeredis
import pytest

from gateway.ticket_consumer import TicketConsumer, configured_consumer
from tools.dispatch_tickets import TicketStore, ticket_execution_context


def fixture_store():
    return TicketStore(fakeredis.FakeRedis(decode_responses=True))


def submit(store):
    store.r.zadd("vault:agents", {"raven": 1})
    return store.submit("harmony", "raven", "Research the request",
                        {}, [], "A sourced result")


@pytest.mark.asyncio
async def test_acceptance_precedes_scheduling_and_context_is_private():
    store = fixture_store()
    ticket = submit(store)
    consumer = TicketConsumer(lambda: store, "raven", None)
    claimed = store.claim("raven", consumer.worker_id)

    async def handle(event):
        assert store.get(ticket["id"])["status"] == "running"
        ctx = ticket_execution_context.get()
        assert ctx["ticket_id"] == ticket["id"]
        assert ctx["token"] not in event.text
        assert event.source.chat_id == f"ticket:{ticket['id']}"
        assert event.internal
        store.finish(ticket["id"], ctx["token"], "succeeded", "verified")
        consumer.adapter.completed[event.message_id].set()

    consumer.adapter.handle_message = handle
    await consumer.execute(store, claimed)
    assert store.get(ticket["id"])["status"] == "succeeded"
    assert ticket_execution_context.get() is None


@pytest.mark.asyncio
async def test_no_explicit_result_is_reconciliation_block_not_success():
    store = fixture_store()
    ticket = submit(store)
    consumer = TicketConsumer(lambda: store, "raven", None)

    async def handle(event):
        consumer.adapter.completed[event.message_id].set()

    consumer.adapter.handle_message = handle
    await consumer.execute(store, store.claim("raven", consumer.worker_id))
    assert store.get(ticket["id"])["status"] == "blocked"
    assert store.claim("raven", "next-worker") is None


@pytest.mark.asyncio
async def test_crash_after_acceptance_never_replays():
    now = [1000]
    store = TicketStore(fakeredis.FakeRedis(decode_responses=True),
                        clock=lambda: now[0])
    ticket = submit(store)
    consumer = TicketConsumer(lambda: store, "raven", None)

    async def crash(event):
        raise RuntimeError("model scheduling failed after acceptance")

    consumer.adapter.handle_message = crash
    with pytest.raises(RuntimeError):
        await consumer.execute(store, store.claim("raven", consumer.worker_id))
    now[0] += 121
    store.recover("raven")
    assert store.get(ticket["id"])["status"] == "blocked"
    assert store.claim("raven", "replacement") is None


def test_configuration_requires_no_discord_or_harmony(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setenv("REDIS_URL", "redis://unused.invalid")
    monkeypatch.delenv("DISPATCH_CHANNEL_ID", raising=False)
    consumer = configured_consumer(None)
    assert consumer.agent == "raven"


@pytest.mark.asyncio
async def test_supervisor_recovers_transport_failure_without_dying():
    store = fixture_store()
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("isolated simulated outage")
        return store

    consumer = TicketConsumer(factory, "raven", None, poll_seconds=.001)
    consumer.start()
    for _ in range(100):
        if store.r.exists("dispatch:availability:raven"):
            break
        await asyncio.sleep(.005)
    await consumer.stop()
    assert len(calls) >= 2
    assert store.r.ttl("dispatch:availability:raven") > 0


@pytest.mark.asyncio
async def test_real_adapter_and_gateway_executor_reach_fenced_native_tool(monkeypatch):
    from gateway.run import GatewayRunner
    from tools import agent_dispatch
    import json

    store = fixture_store()
    ticket = submit(store)
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setattr(agent_dispatch, "_redis", lambda: store.r)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}  # no platform config or Discord adapter

    def native_tool():
        assert ticket_execution_context.get()["ticket_id"] == ticket["id"]
        return agent_dispatch.agent_dispatch_tool({
            "action": "complete", "chain_id": ticket["id"], "text": "Verified result"})

    async def handler(event):
        assert runner._adapter_for_source(event.source) is consumer.adapter
        result = await runner._run_in_executor_with_context(native_tool)
        assert json.loads(result)["ticket"]["status"] == "succeeded"
        return None

    consumer = TicketConsumer(lambda: store, "raven", handler)
    runner._ticket_consumer = consumer
    try:
        await asyncio.wait_for(
            consumer.execute(store, store.claim("raven", consumer.worker_id)), 5)
    finally:
        runner._executor.shutdown(wait=True)
    assert store.get(ticket["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_gateway_rejects_forged_ticket_user_before_authorization():
    from gateway.run import GatewayRunner
    from gateway.platforms.base import MessageEvent

    runner = object.__new__(GatewayRunner)
    consumer = TicketConsumer(None, "raven", None)
    source = consumer.adapter.build_source(chat_id="ticket:1", user_id="ticket-system")
    with pytest.raises(PermissionError, match="fenced"):
        await runner._handle_message(MessageEvent(text="run work", source=source, internal=True))


@pytest.mark.parametrize("mode", ["off", "smart", "manual"])
def test_ticket_cannot_inherit_owner_autoapproval(monkeypatch, mode):
    from tools import approval
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: mode)
    binding = ticket_execution_context.set({"ticket_id": "123", "token": "fence"})
    try:
        assert not approval.check_all_command_guards("rm -rf /tmp/customer-data", "local")["approved"]
        assert not approval.check_execute_code_guard("print('test')", "local")["approved"]
        assert ticket_execution_context.get()["approval_required"]
        notified = []
        decision = approval._await_gateway_decision(
            "ticket:123", lambda data: notified.append(data), {})
        assert decision["choice"] == "deny"
        assert notified == []
    finally:
        ticket_execution_context.reset(binding)


def test_native_completion_cannot_turn_approval_block_into_success(monkeypatch):
    from tools import agent_dispatch, approval
    import json
    store = fixture_store()
    ticket = submit(store)
    claimed = store.claim("raven", "worker")
    store.accept(ticket["id"], claimed["delivery"]["token"])
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setattr(agent_dispatch, "_redis", lambda: store.r)
    binding = ticket_execution_context.set({
        "ticket_id": ticket["id"], "token": claimed["delivery"]["token"]})
    try:
        approval._block_ticket_approval()
        result = json.loads(agent_dispatch.agent_dispatch_tool({
            "action": "complete", "chain_id": ticket["id"], "text": "Done"}))
        assert result["ticket"]["status"] == "blocked"
        assert result["ticket"]["required_inputs"]
    finally:
        ticket_execution_context.reset(binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["order", "question", "answer", "completed"])
async def test_legacy_conversation_requires_explicit_migration(monkeypatch, kind):
    from plugins.platforms.discord import dispatch
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    chain = {"id": "legacy", "from": "harmony", "to": "raven",
             "status": "waiting", "question": "What next?"}
    monkeypatch.setattr(dispatch, "_store", lambda: SimpleNamespace(
        get_chain=lambda r, cid: chain))
    manager = dispatch.DispatchManager(SimpleNamespace())
    manager._inject_turn = AsyncMock()
    await manager._handle_inbox_event(None, {"kind": kind, "chain_id": "legacy"})
    manager._inject_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_crm_intake_is_not_migrated_away(monkeypatch):
    from plugins.platforms.discord import dispatch
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    chain = {"id": "crm", "origin": "crm", "from": "crm-system", "to": "raven"}
    monkeypatch.setattr(dispatch, "_store", lambda: SimpleNamespace(
        get_chain=lambda r, cid: chain))
    manager = dispatch.DispatchManager(SimpleNamespace())
    manager._inject_turn = AsyncMock()
    await manager._handle_inbox_event(
        None, {"kind": "cancelled", "chain_id": "crm", "by": "owner"})
    manager._inject_turn.assert_awaited_once()