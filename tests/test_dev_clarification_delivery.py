"""Narrow clarification delivery, using fake Redis and mocked handlers only."""
import asyncio
import json
from pathlib import Path

import fakeredis
import pytest

from agent.prompt_builder import DEV_REQUEST_GUIDANCE
from gateway.ticket_consumer import TicketConsumer, configured_consumer
from services.vault.dev_clarifications import ClarificationStore
from tools import agent_dispatch, dev_requests
from tools.dispatch_tickets import TicketStore, ticket_execution_context


def stores():
    r = fakeredis.FakeRedis(decode_responses=True)
    r.set("devreq:item:168", json.dumps({
        "id": "168", "agent": "raven", "status": "approved",
        "dispatch_repl_id": "destination",
    }))
    clarifications = ClarificationStore(r)
    capability = clarifications.provision("168", "destination", "developer")
    question = clarifications.ask(
        capability, "168", "destination", "Which export format?", "question-1")
    return TicketStore(r), clarifications, question, capability


def binding(question, claimed):
    return {
        "kind": "dev_clarification", "question_id": question["id"],
        "ticket_id": question["id"], "token": claimed["delivery"]["token"],
    }


@pytest.mark.asyncio
async def test_delivery_answers_in_private_fenced_context(monkeypatch):
    tickets, store, question, capability = stores()
    monkeypatch.setenv("AGENT_NAME", " RAVEN ")
    monkeypatch.setattr(dev_requests, "_redis", lambda: store.r)
    observed = []

    async def handler(event):
        ctx = ticket_execution_context.get()
        assert ctx["kind"] == "dev_clarification"
        assert ctx["question_id"] == ctx["ticket_id"] == question["id"]
        assert store.get(question["id"])["status"] == "running"
        assert ctx["token"] not in event.text
        assert capability not in event.text
        assert "developer_id" not in event.text
        assert event.internal
        assert event.metadata["kind"] == "dev_clarification"
        assert "answer_clarification" in event.text
        assert "does not automatically resume" in event.text
        # Context survives the same copy-context executor boundary used by tools.
        result = json.loads(await asyncio.to_thread(dev_requests.dev_request_tool, {
            "action": "answer_clarification", "question_id": question["id"],
            "answer": "PNG", "idempotency_key": "answer-1", "agent": "impostor",
        }))
        observed.append(result)
        consumer.adapter.completed[event.message_id].set()

    consumer = TicketConsumer(lambda: tickets, "raven", None,
                              clarification_store_factory=lambda: store)
    consumer.adapter.handle_message = handler
    await consumer.execute(store, store.claim("raven", consumer.worker_id))
    assert observed[0]["answered"]
    assert store.get(question["id"])["answer"] == "PNG"
    assert tickets.r.get(tickets._key(question["id"])) is None
    assert tickets.r.llen("devreq:dispatch") == 0
    assert ticket_execution_context.get() is None


@pytest.mark.asyncio
async def test_conversational_output_does_not_answer_or_replay():
    tickets, store, question, _ = stores()
    consumer = TicketConsumer(lambda: tickets, "raven", None)

    async def handler(event):
        await consumer.adapter.send(event.source.chat_id, "PNG")
        consumer.adapter.completed[event.message_id].set()

    consumer.adapter.handle_message = handler
    await consumer.execute(store, store.claim("raven", consumer.worker_id))
    record = store.get(question["id"])
    assert record["status"] == "blocked"
    assert record["answer"] is None
    assert store.claim("raven", "replacement") is None


@pytest.mark.asyncio
async def test_poll_visits_separate_stores_and_preserves_ordinary_tickets():
    tickets, clarifications, question, _ = stores()
    tickets.r.zadd("vault:agents", {"raven": 1})
    ordinary = tickets.submit("harmony", "raven", "Ordinary work", {}, [], "Result")
    seen = []
    consumer = TicketConsumer(
        lambda: tickets, "raven", None, poll_seconds=.001,
        clarification_store_factory=lambda: clarifications)

    async def handler(event):
        ctx = ticket_execution_context.get()
        seen.append(event.message_id)
        if ctx.get("kind") == "dev_clarification":
            clarifications.answer(question["id"], "raven", "PNG", "answer-1", ctx["token"])
        else:
            assert "question_id" not in ctx
            assert "Use agent_dispatch" in event.text
            tickets.finish(ordinary["id"], ctx["token"], "succeeded", "Done")
        consumer.adapter.completed[event.message_id].set()

    consumer.adapter.handle_message = handler
    consumer.start()
    try:
        for _ in range(100):
            if len(seen) == 2:
                break
            await asyncio.sleep(.01)
        assert seen == [ordinary["id"], question["id"]]
    finally:
        await consumer.stop()
    assert tickets.get(ordinary["id"])["status"] == "succeeded"
    assert clarifications.get(question["id"])["status"] == "succeeded"


@pytest.mark.asyncio
async def test_failed_ordinary_transport_still_polls_clarifications():
    _, store, question, _ = stores()

    def unavailable():
        raise ConnectionError("simulated ordinary transport failure")

    consumer = TicketConsumer(
        unavailable, "raven", None, poll_seconds=.001,
        clarification_store_factory=lambda: store)
    seen = asyncio.Event()

    async def handler(event):
        ctx = ticket_execution_context.get()
        store.answer(question["id"], "raven", "PNG", "answer-1", ctx["token"])
        consumer.adapter.completed[event.message_id].set()
        seen.set()

    consumer.adapter.handle_message = handler
    consumer.start()
    try:
        await asyncio.wait_for(seen.wait(), 3)
    finally:
        await consumer.stop()


@pytest.mark.parametrize("bad_context", [
    None,
    {"ticket_id": "other", "token": "fake"},
    {"kind": "dev_clarification", "question_id": "other",
     "ticket_id": "other", "token": "fake"},
])
def test_answer_requires_matching_context(monkeypatch, bad_context):
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setattr(dev_requests, "_redis", lambda: pytest.fail("must not contact Redis"))
    handle = ticket_execution_context.set(bad_context)
    try:
        result = json.loads(dev_requests.dev_request_tool({
            "action": "answer_clarification", "question_id": "cq-current",
            "answer": "PNG", "idempotency_key": "answer-1",
        }))
        assert "matching fenced" in result["error"]
    finally:
        ticket_execution_context.reset(handle)


@pytest.mark.parametrize("failure", ["wrong_agent", "no_agent", "stale_token", "approval"])
def test_answer_rejects_identity_lease_and_approval_bypasses(monkeypatch, failure):
    _, store, question, _ = stores()
    claimed = store.claim("raven", "worker")
    ctx = binding(question, claimed)
    store.accept(question["id"], ctx["token"])
    monkeypatch.setenv("AGENT_NAME", {"wrong_agent": "cora", "no_agent": ""}.get(failure, "raven"))
    monkeypatch.setattr(dev_requests, "_redis", lambda: store.r)
    if failure == "stale_token":
        ctx["token"] = "stale"
    if failure == "approval":
        ctx["approval_required"] = True
    handle = ticket_execution_context.set(ctx)
    try:
        result = json.loads(dev_requests.dev_request_tool({
            "action": "answer_clarification", "question_id": question["id"],
            "answer": "PNG", "idempotency_key": "answer-1", "agent": "raven",
            "token": claimed["delivery"]["token"],
        }))
        assert "error" in result
        assert store.get(question["id"])["answer"] is None
    finally:
        ticket_execution_context.reset(handle)


def test_explicit_answer_retries_are_idempotent_and_conflicts_fail(monkeypatch):
    _, store, question, _ = stores()
    claimed = store.claim("raven", "worker")
    ctx = binding(question, claimed)
    store.accept(question["id"], ctx["token"])
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setattr(dev_requests, "_redis", lambda: store.r)
    handle = ticket_execution_context.set(ctx)
    args = {"action": "answer_clarification", "question_id": question["id"],
            "answer": "PNG", "idempotency_key": "answer-1"}
    try:
        assert json.loads(dev_requests.dev_request_tool(args))["answered"]
        assert json.loads(dev_requests.dev_request_tool(args))["answered"]
        assert "error" in json.loads(dev_requests.dev_request_tool(dict(args, answer="JPEG")))
        assert store.r.llen("devreq:dispatch") == 0
    finally:
        ticket_execution_context.reset(handle)


@pytest.mark.parametrize("action", ["complete", "blocked", "submit"])
def test_dispatch_mutations_cannot_misroute_clarification(monkeypatch, action):
    _, store, question, _ = stores()
    claimed = store.claim("raven", "worker")
    store.accept(question["id"], claimed["delivery"]["token"])
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setattr(agent_dispatch, "_redis", lambda: store.r)
    handle = ticket_execution_context.set(binding(question, claimed))
    try:
        result = json.loads(agent_dispatch.agent_dispatch_tool({
            "action": action, "chain_id": question["id"], "text": "Done",
            "required_inputs": ["missing"],
        }))
        assert "error" in result
        assert store.get(question["id"])["status"] == "running"
        assert store.r.get("dispatch:chain:" + question["id"]) is None
    finally:
        ticket_execution_context.reset(handle)


def test_configured_consumer_and_guidance_expose_only_narrow_exception(monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "raven")
    monkeypatch.setenv("REDIS_URL", "redis://unused.invalid")
    consumer = configured_consumer(None)
    assert consumer.clarification_store_factory is not None
    skill = Path("skills/harmony_communication/SKILL.md").read_text()
    for text in (skill, DEV_REQUEST_GUIDANCE):
        text = " ".join(text.replace("*", "").split())
        assert "answer_clarification" in text
        assert "idempotency_key" in text
        assert "not general agent" in text
        assert "automatically" in text