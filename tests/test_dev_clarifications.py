"""All storage is an isolated FakeRedis; exercise real admission Lua via lupa."""
import concurrent.futures
import json
from unittest.mock import patch

import fakeredis
import pytest
from redis.exceptions import ConnectionError

from services.vault.dev_clarifications import (
    BUDGET, PREFIX, ClarificationStore, RateLimited,
)


@pytest.fixture
def store():
    return ClarificationStore(fakeredis.FakeRedis(decode_responses=True))


def provision(store, rid="1", destination="app-one", developer="dev-one", agent="cora"):
    store.r.set(f"devreq:item:{rid}", json.dumps({
        "id": rid, "status": "approved", "dispatch_repl_id": destination,
        "agent": agent,
    }))
    return store.provision(rid, destination, developer)


def ask(store, token, key="intent", rid="1", destination="app-one"):
    return store.ask(token, rid, destination, "Which format?", key)


def test_roundtrip_separate_records_and_delivery_ack_not_answer(store):
    token = provision(store)
    raw = store.r.get("devreq:item:1")
    q = ask(store, token)
    assert q["answer"] is None and q["recipient_online"] is False
    claimed = store.claim("cora", "worker")
    fence = claimed["delivery"]["token"]
    assert store.claim("cora", "another") is None
    accepted = store.accept(q["id"], fence)
    assert accepted["answer"] is None
    with pytest.raises(ValueError):
        store.accept(q["id"], fence)
    result = store.answer(q["id"], "cora", "PDF please", "reply", fence)
    assert result["answer_state"] == "answered"
    assert "token" not in result["delivery"]
    assert store.answer(q["id"], "cora", "PDF please", "reply", fence)["answer"] == "PDF please"
    assert store.read(token, "1", "app-one", q["id"])["answer"] == "PDF please"
    assert store.r.get("devreq:item:1") == raw
    assert store.r.zcard(BUDGET) == 1
    assert not list(store.r.scan_iter("dispatch:inbox:*"))


def test_global_concurrent_limit_across_developers_projects_agents(store):
    scopes = [(str(n), f"app-{n}", provision(store, str(n), f"app-{n}",
              f"developer-{n}", f"agent-{n}")) for n in range(16)]
    def submit(scope):
        rid, destination, token = scope
        try:
            return ask(store, token, rid=rid, destination=destination)
        except RateLimited as exc:
            return exc
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(submit, scopes))
    assert sum(isinstance(x, dict) for x in results) == 4
    assert store.r.zcard(BUDGET) == 4
    assert all(1 <= x.retry_after <= 300 for x in results if isinstance(x, RateLimited))
    assert len(list(store.r.scan_iter(PREFIX + "item:*"))) == 4


def test_idempotency_before_budget_and_conflict(store):
    token = provision(store)
    original = ask(store, token)
    for n in range(3):
        ask(store, token, str(n))
    assert ask(store, token)["id"] == original["id"]
    with pytest.raises(ValueError, match="different"):
        store.ask(token, "1", "app-one", "Changed question", "intent")
    with pytest.raises(RateLimited):
        ask(store, token, "fifth")
    assert store.r.zcard(BUDGET) == 4
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert {x["id"] for x in pool.map(lambda _: ask(store, token), range(8))} == {original["id"]}


def test_exact_rolling_expiry_no_fixed_boundary_burst(store):
    token = provision(store)
    # fakeredis TIME calls time.time; frozen server time exercises Lua boundary.
    with patch("time.time", return_value=1000000.0):
        for n in range(4):
            ask(store, token, str(n))
    with patch("time.time", return_value=1000299.999):
        with pytest.raises(RateLimited) as exc:
            ask(store, token, "new")
        assert exc.value.retry_after == 1
    with patch("time.time", return_value=1000300.0):
        assert ask(store, token, "new")["created_at"] == 1000300.0


def test_denials_expiry_outage_and_bounds(store):
    token = provision(store)
    other = provision(store, "2", "app-two")
    q = ask(store, token)
    for operation in (
        lambda: ask(store, token, rid="2", destination="app-two"),
        lambda: store.read(other, "2", "app-two", q["id"]),
        lambda: store.answer(q["id"], "bianca", "x", "reply", "bogus"),
        lambda: store.provision("1", "wrong-project", "developer"),
    ):
        with pytest.raises(PermissionError):
            operation()
    with pytest.raises(ValueError):
        store.ask(token, "1", "app-one", "x" * 8001, "large")
    store.r.delete(store._cap(token))
    with pytest.raises(PermissionError):
        ask(store, token)
    with patch.object(store.r, "eval", side_effect=ConnectionError("unavailable")):
        with pytest.raises(ConnectionError):
            ask(store, other, rid="2", destination="app-two")
    assert store.r.zcard(BUDGET) == 1


def test_expired_capability_even_if_key_survives(store):
    token = provision(store)
    cap = json.loads(store.r.get(store._cap(token)))
    cap["expires_at"] = store.clock() - 1
    store.r.set(store._cap(token), json.dumps(cap))
    with pytest.raises(PermissionError):
        ask(store, token)


def test_restart_before_accept_retries_same_identity_after_accept_blocks(store):
    token = provision(store)
    q = ask(store, token)
    first = store.claim("cora", "old", lease_seconds=-1)
    restarted = ClarificationStore(store.r)
    restarted.recover("cora")
    current = restarted.get(q["id"])
    assert current["delivery"]["state"] == "retrying"
    current["delivery"]["next_attempt_at"] = 0
    store.r.set(store._key(q["id"]), json.dumps(current))
    store.r.zadd(PREFIX + "queue:cora", {q["id"]: 0})
    claim = restarted.claim("cora", "new")
    restarted.accept(q["id"], claim["delivery"]["token"])
    current = restarted.get(q["id"])
    current["delivery"]["lease_until"] = 0
    store.r.set(store._key(q["id"]), json.dumps(current))
    restarted.recover("cora")
    assert restarted.get(q["id"])["delivery"]["state"] == "reconciliation_required"
    assert restarted.claim("cora", "third") is None
    with pytest.raises(ValueError):
        restarted.answer(q["id"], "cora", "late", "reply", first["delivery"]["token"])
    assert restarted.read(token, "1", "app-one", q["id"])["answer"] is None


def test_offline_expiry_visible_on_read_without_consumer(store):
    token = provision(store)
    q = ask(store, token)
    raw = store.get(q["id"])
    raw["expires_at"] = 0
    store.r.set(store._key(q["id"]), json.dumps(raw))
    assert store.read(token, "1", "app-one", q["id"])["status"] == "expired"
    assert store.claim("cora", "late") is None


def test_preaccept_exhaustion_and_failed_delivery_never_answer(store):
    token = provision(store)
    q = ask(store, token)
    current = store.claim("cora", "worker", lease_seconds=-1)
    current["delivery"]["attempts"] = 5
    store.r.set(store._key(q["id"]), json.dumps(current))
    store.recover("cora")
    result = store.read(token, "1", "app-one", q["id"])
    assert result["delivery"]["state"] == "exhausted"
    assert result["answer"] is None