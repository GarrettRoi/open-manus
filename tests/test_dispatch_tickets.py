"""Isolated ticket lifecycle tests. Never connect to workspace Redis."""
import concurrent.futures

import fakeredis
import pytest

from tools.dispatch_tickets import TicketStore


@pytest.fixture
def store():
    now = [1000.]
    s = TicketStore(fakeredis.FakeRedis(decode_responses=True), clock=lambda: now[0])
    s.r.zadd("vault:agents", {"raven": 1, "harmony": 1, "cora": 1})
    s.now = now
    return s


def submit(s, **kwargs):
    return s.submit("harmony", "raven", "Research", {}, [], "Report", **kwargs)


def test_offline_atomic_durable_and_dedup(store):
    t = submit(store, dedup_key="a")
    assert t["status"] == "queued"
    assert store.r.ttl(store._key(t["id"])) == -1
    assert submit(store, dedup_key="a")["id"] == t["id"]
    assert store.list_for("raven")[0]["id"] == t["id"]
    with pytest.raises(ValueError):
        store.submit("harmony", "raven", "Different", {}, [], "Report", dedup_key="a")


def test_concurrent_claim_single_winner(store):
    submit(store)
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        claims = list(ex.map(lambda n: store.claim("raven", str(n)), range(4)))
    assert sum(t is not None for t in claims) == 1


def test_preaccept_retry_then_ambiguous_blocks(store):
    t = submit(store)
    claim = store.claim("raven", "one", lease_seconds=10)
    token = claim["delivery"]["token"]
    store.now[0] += 11
    store.recover("raven")
    assert store.get(t["id"])["delivery"]["state"] == "retrying"
    assert store.claim("raven", "two") is None
    store.now[0] += 10
    claim = store.claim("raven", "two", lease_seconds=10)
    with pytest.raises(ValueError):
        store.accept(t["id"], token)
    store.accept(t["id"], claim["delivery"]["token"])
    with pytest.raises(ValueError):
        store.accept(t["id"], claim["delivery"]["token"])
    store.now[0] += 11
    store.recover("raven")
    assert store.get(t["id"])["status"] == "blocked"
    assert store.get(t["id"])["delivery"]["state"] == "reconciliation_required"
    assert store.claim("raven", "three") is None


def test_cancel_fences_and_permissions(store):
    t = submit(store)
    token = store.claim("raven", "one")["delivery"]["token"]
    store.accept(t["id"], token)
    with pytest.raises(PermissionError):
        store.cancel(t["id"], "cora")
    store.cancel(t["id"], "harmony")
    with pytest.raises(ValueError):
        store.finish(t["id"], token, "succeeded", "done")
    with pytest.raises(PermissionError):
        store.get(t["id"], "cora")


def test_exhaustion_not_dropped(store):
    t = submit(store)
    for _ in range(5):
        c = store.claim("raven", "one")
        store.retry(t["id"], c["delivery"]["token"], "Unavailable")
        store.now[0] += 400
    assert store.get(t["id"])["delivery"]["state"] == "exhausted"
    assert store.get(t["id"])["status"] == "failed"


def test_result_no_completion_queue_and_explicit_continuation(store):
    p = submit(store)
    pc = store.claim("raven", "one")
    store.accept(p["id"], pc["delivery"]["token"])
    c = store.submit("raven", "cora", "Child work", {}, [], "Child report", parent_id=p["id"])
    cc = store.claim("cora", "two")
    store.accept(c["id"], cc["delivery"]["token"])
    store.finish(c["id"], cc["delivery"]["token"], "succeeded", "Result")
    assert store.r.llen("dispatch:inbox:raven") == 0
    store.finish(p["id"], pc["delivery"]["token"], "blocked", "Needs synthesis",
                 required_inputs=["Requester continuation decision"])
    follow = store.continue_parent(p["id"], "harmony")
    assert follow["parent_id"] == p["id"]
    assert store.continue_parent(p["id"], "harmony")["id"] == follow["id"]


def test_native_context_and_legacy_migration(store, monkeypatch):
    from tools import agent_dispatch
    from tools.dispatch_tickets import ticket_execution_context
    import json

    monkeypatch.setattr(agent_dispatch, "_redis", lambda: store.r)
    monkeypatch.setenv("AGENT_NAME", "harmony")
    old = json.loads(agent_dispatch.agent_dispatch_tool(
        {"action": "dispatch", "to": "raven", "task": "Old task"}))
    assert "retired" in old["error"]
    t = submit(store)
    claim = store.claim("raven", "worker")
    store.accept(t["id"], claim["delivery"]["token"])
    monkeypatch.setenv("AGENT_NAME", "raven")
    args = {"action": "complete", "chain_id": t["id"], "text": "Done"}
    assert "error" in json.loads(agent_dispatch.agent_dispatch_tool(args))
    ctx = ticket_execution_context.set({"ticket_id": t["id"], "token": claim["delivery"]["token"]})
    try:
        # Native tool constructs its own real-clock store; use the fixture clock
        # to ensure the test lease remains valid without connecting anywhere.
        monkeypatch.setattr("tools.dispatch_tickets.TicketStore", lambda r: store)
        result = json.loads(agent_dispatch.agent_dispatch_tool(args))
        assert result["ticket"]["status"] == "succeeded"
    finally:
        ticket_execution_context.reset(ctx)


def test_recovery_noop_preserves_timestamp_and_audit(store):
    t = submit(store)
    events = store.r.lrange(f"dispatch:tickets:events:{t['id']}", 0, -1)
    store.now[0] += 20
    store.recover("raven")
    assert store.get(t["id"])["updated_at"] == t["updated_at"]
    assert store.r.lrange(f"dispatch:tickets:events:{t['id']}", 0, -1) == events
    c = store.claim("raven", "worker")
    store.accept(t["id"], c["delivery"]["token"])
    done = store.finish(t["id"], c["delivery"]["token"], "succeeded", "Private report")
    store.now[0] += 20
    store.recover("raven")
    assert store.get(t["id"])["updated_at"] == done["updated_at"]
    audit = "".join(store.r.lrange(f"dispatch:tickets:events:{t['id']}", 0, -1))
    assert "Private report" not in audit and c["delivery"]["token"] not in audit


def test_unknown_recipient_and_result_validation(store):
    with pytest.raises(ValueError, match="Unknown"):
        store.submit("harmony", "ghost", "Do work", {}, [], "Report")
    with pytest.raises(ValueError, match="artifacts"):
        submit(store, artifacts=[{"secret": "not a reference"}])
    t = submit(store)
    token = store.claim("raven", "worker")["delivery"]["token"]
    store.accept(t["id"], token)
    with pytest.raises(ValueError, match="required_inputs"):
        store.finish(t["id"], token, "blocked", "Missing data")
    assert store.get(t["id"])["status"] == "running"


def test_context_parent_cannot_be_overridden_or_cancelled(store):
    from tools.dispatch_tickets import ticket_execution_context
    p = submit(store)
    token = store.claim("raven", "worker")["delivery"]["token"]
    store.accept(p["id"], token)
    ctx = ticket_execution_context.set({"ticket_id": p["id"], "token": token})
    try:
        with pytest.raises(ValueError, match="override"):
            store.submit("raven", "cora", "Child", {}, [], "Report", parent_id="999")
        child = store.submit("raven", "cora", "Child", {}, [], "Report")
        assert child["parent_id"] == p["id"]
        with pytest.raises(ValueError, match="child"):
            store.finish(p["id"], token, "succeeded", "Premature")
        store.cancel(p["id"], "harmony")
        with pytest.raises(ValueError, match="lease"):
            store.submit("raven", "cora", "Child", {}, [], "Report")
    finally:
        ticket_execution_context.reset(ctx)


def test_legacy_crm_work_completes_but_questions_retired(store, monkeypatch):
    import json
    from tools import agent_dispatch
    monkeypatch.setattr(agent_dispatch, "_redis", lambda: store.r)
    monkeypatch.setenv("AGENT_NAME", "raven")
    chain = agent_dispatch.notification_chain("crm-1", "raven", "CRM event", "e1")
    agent_dispatch.save_chain(store.r, chain)
    def call(action, **extra):
        return json.loads(agent_dispatch.agent_dispatch_tool(
            {"action": action, "chain_id": "crm-1", **extra}))
    assert call("working")["legacy_chain"]["status"] == "working"
    assert "error" in call("question", text="More?")
    assert call("complete", text="CRM processed")["legacy_chain"]["status"] == "done"
    assert store.r.ttl("dispatch:chain:crm-1") == -1