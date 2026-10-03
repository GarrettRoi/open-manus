"""Focused tests for multi-project dev-request routing.

These tests use fakeredis plus mocked MCP/Lua calls only.  No provider or
vault service is contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import fakeredis
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "services" / "vault"
for path in (ROOT, VAULT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from services.vault import dev_projects
from services.vault import dev_clarifications
from services.vault import replit_mcp
from services.vault.dev_routing import content_digest


@pytest.fixture(autouse=True)
def owner_identity(monkeypatch):
    monkeypatch.setenv("DISCORD_OWNER_ID", "owner")


def redis():
    return fakeredis.FakeRedis(decode_responses=True)


def put_projects(r, mapping):
    r.set(dev_projects.K_PROJECTS, json.dumps(mapping))


class _OneIterationQueue:
    """Return one queue item, then stop a dispatch-loop test cleanly."""

    def __init__(self, req_id):
        self.req_id = req_id
        self.calls = 0

    def brpop(self, queue, timeout):
        if self.calls == 0:
            self.calls += 1
            return queue, self.req_id
        raise asyncio.CancelledError


async def run_dispatch_iteration(mcp, req_id, renew_result=True,
                                 pin_result=None, public_url="https://vault.invalid"):
    """Run one real dispatcher iteration with only Redis Lua mocked.

    fakeredis does not provide a Lua interpreter in the lightweight test
    environment.  The three Lua-backed helpers are therefore patched with
    small persistence-preserving stand-ins; all queue, item, routing, prompt,
    and error handling remains in ``dispatch_loop`` itself.
    """
    # Routing regressions exercise the real mandatory capability provisioner
    # against fake Redis, with an explicit non-live HTTPS vault URL.
    mcp.public_url = public_url
    key = f"devreq:item:{req_id}"
    raw = mcp.r.get(key)
    if raw:
        request = json.loads(raw)
        request.setdefault("agent", "tester")
        if req_id != "corrected-route" and not request.get("work_scope"):
            project = dev_projects.normalize_project(request.get("project"))
            request.update(project=project,
                           work_scope="fleet_platform" if project == "open-manus" else "project_app",
                           decided_by="owner", decided_at=1)
            try:
                _, target = dev_projects.resolve_project(mcp.r, project)
            except ValueError:
                target = "missing-id"
            request.update(submitted_repl_id=target, dispatch_project=project,
                           dispatch_repl_id=target)
            request["owner_confirmation"] = {"version": 1, "owner_id": "owner"}
            request["approval_digest"] = content_digest(request)
        mcp.r.set(key, json.dumps(request), ex=86400)
    queue = _OneIterationQueue(req_id)
    redis_module = ModuleType("redis")
    redis_module.from_url = lambda *args, **kwargs: queue

    def pin_route(r, request_id, token, item, project, repl_id):
        pinned = dict(item)
        pinned["dispatch_project"] = project
        pinned["dispatch_repl_id"] = repl_id
        r.set(f"devreq:item:{request_id}", json.dumps(pinned), ex=86400)
        return True

    def finalize(r, request_id, token, item, item_ttl):
        key = f"devreq:item:{request_id}"
        if item_ttl and item_ttl > 0:
            r.set(key, json.dumps(item), ex=item_ttl)
        else:
            r.set(key, json.dumps(item))
        r.delete(replit_mcp.K_LEASE + request_id)
        return True

    pin_side_effect = pin_result or pin_route
    renew_kwargs = (
        {"return_value": renew_result}
        if isinstance(renew_result, bool)
        else {"side_effect": renew_result}
    )
    with patch.dict(sys.modules, {"redis": redis_module}), \
            patch.dict("os.environ", {"REDIS_URL": "redis://dispatch-test"}), \
            patch.object(replit_mcp, "renew_lease", **renew_kwargs), \
            patch.object(replit_mcp, "pin_dispatch_route",
                         side_effect=pin_side_effect), \
            patch.object(replit_mcp, "finalize_lease",
                         side_effect=finalize):
        with pytest.raises(asyncio.CancelledError):
            await replit_mcp.dispatch_loop(mcp)


def test_project_names_are_canonical_and_collisions_are_rejected():
    assert dev_projects.normalize_project(" SECOND   App ") == "second-app"
    assert dev_projects.normalize_project(" \t ") == "open-manus"
    assert dev_projects.validate_projects(
        {" SECOND   App ": "second_repl"}) == {"second-app": "second_repl"}

    with pytest.raises(ValueError, match="collide"):
        dev_projects.validate_projects({"Second App": "one", "second-app": "two"})
    with pytest.raises(ValueError, match="more than one project"):
        dev_projects.validate_projects({"one": "same", "two": "same"})
    with pytest.raises(ValueError, match="invalid project name"):
        dev_projects.validate_projects({"second/app": "one"})


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-url", "redis-unavailable"])
async def test_dispatch_refuses_to_start_without_clarification_channel(monkeypatch, failure):
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    r.set("devreq:item:channel-failure", json.dumps({
        "id": "channel-failure", "project": "open-manus",
        "agent": "tester", "status": "approved",
    }), ex=86400)
    mcp = replit_mcp.ReplitMCP(r, str, str, "")
    mcp.start_agent_run = AsyncMock()
    if failure == "redis-unavailable":
        def unavailable(*args, **kwargs):
            raise RuntimeError("secret internal diagnostics")
        monkeypatch.setattr(dev_clarifications.ClarificationStore, "provision", unavailable)
    await run_dispatch_iteration(
        mcp, "channel-failure",
        public_url="" if failure == "missing-url" else "https://vault.invalid")
    mcp.start_agent_run.assert_not_awaited()
    stored = json.loads(r.get("devreq:item:channel-failure"))
    assert stored["dispatch_status"] == "failed"
    assert stored["dispatch_repl_id"] == "repl-one"
    assert "secret internal diagnostics" not in stored["dispatch_error"]
    assert "clarification channel" in stored["dispatch_error"]


def test_legacy_target_is_default_only_and_explicit_registry_wins():
    r = redis()
    r.set(dev_projects.K_TARGET, "legacy-default")
    put_projects(r, {"Second   App": "second-repl"})

    assert dev_projects.list_projects(r) == {"second-app": "second-repl"}
    assert dev_projects.configured_names(r) == ["open-manus", "second-app"]
    assert dev_projects.resolve_project(r, "") == ("open-manus", "legacy-default")
    assert dev_projects.resolve_project(r, "SECOND app") == ("second-app", "second-repl")
    with pytest.raises(ValueError, match="unknown project"):
        dev_projects.resolve_project(r, "not-configured")


def test_resolve_project_reads_registry_and_legacy_default_with_one_mget():
    class MGetOnlyRedis:
        def __init__(self):
            self.calls = []

        def mget(self, keys):
            self.calls.append(list(keys))
            return [json.dumps({"open-manus": "explicit-default"}), "legacy-default"]

        def get(self, _key):
            raise AssertionError("destination resolution must use one MGET snapshot")

    r = MGetOnlyRedis()
    assert dev_projects.resolve_project(r, "open-manus") == (
        "open-manus", "explicit-default")
    assert r.calls == [[dev_projects.K_PROJECTS, dev_projects.K_TARGET]]


def test_explicit_open_manus_does_not_read_legacy_target():
    r = redis()
    r.set(dev_projects.K_TARGET, "legacy-default")
    put_projects(r, {"open-manus": "explicit-default"})
    assert dev_projects.resolve_project(r, "open-manus") == (
        "open-manus", "explicit-default")


def test_missing_default_has_actionable_error_and_no_phantom_name():
    r = redis()
    put_projects(r, {"Second App": "second-repl"})
    assert dev_projects.configured_names(r) == ["second-app"]
    with pytest.raises(ValueError, match="unknown project"):
        dev_projects.resolve_project(r, "")


@pytest.mark.asyncio
async def test_route_is_pinned_before_retry_and_survives_registry_change():
    r = redis()
    put_projects(r, {"open-manus": "repl-one", "second-app": "repl-two"})
    req_id = "route-1"
    item = {
        "id": req_id,
        "project": "second app",
        "status": "approved",
        "work_scope": "project_app", "submitted_repl_id": "repl-two",
        "dispatch_project": "second-app", "dispatch_repl_id": "repl-two",
        "decided_by": "owner", "decided_at": 1,
        "owner_confirmation": {"version": 1, "owner_id": "owner"},
    }
    item["approval_digest"] = content_digest(item)
    r.set(f"devreq:item:{req_id}", json.dumps(item), ex=86400)
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    token = "lease-token"
    # fakeredis in the lightweight test environment has no Lua engine; the
    # CAS helper itself is covered by the existing Redis-backed suite.  Mock
    # this one atomic write here to focus on routing/retry behavior.
    with patch.object(replit_mcp, "pin_dispatch_route", return_value=True):
        resolved = await replit_mcp._resolve_and_pin_route(
            mcp, req_id, token, item)
    assert resolved == ("second-app", "repl-two")
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    stored.update({
        "dispatch_project": item["dispatch_project"],
        "dispatch_repl_id": item["dispatch_repl_id"],
    })
    r.set(f"devreq:item:{req_id}", json.dumps(stored), ex=86400)
    assert stored["dispatch_project"] == "second-app"
    assert stored["dispatch_repl_id"] == "repl-two"

    # The target is deleted/reassigned after the first attempt.  The durable
    # route, not current registry state, controls the retry.
    put_projects(r, {"open-manus": "repl-one"})
    with pytest.raises(replit_mcp.ReplitMCPRoutingError, match="unknown project"):
        await replit_mcp._resolve_and_pin_route(mcp, req_id, token, stored)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project", "repl_id"),
    (("open-manus", "repl-one"), ("SECOND   App", "repl-two")),
)
async def test_dispatch_loop_sends_each_approved_project_to_its_explicit_id(
        project, repl_id):
    r = redis()
    put_projects(r, {"open-manus": "repl-one", "second-app": "repl-two"})
    req_id = f"approved-{repl_id}"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": project,
            "status": "approved",
            "title": "Implement the change",
            "description": "A project-local change.",
            "agent": "tester",
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})

    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_awaited_once()
    assert mcp.start_agent_run.call_args.kwargs["repl_id"] == repl_id
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_status"] == "started"
    assert stored["dispatch_repl_id"] == repl_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_mapping", "mapping", "project", "reason"),
    (
        (
            {"open-manus": "repl-one"},
            {"open-manus": "repl-one"},
            "not-configured",
            "unknown project",
        ),
        (
            {},
            {},
            "open-manus",
            "not configured",
        ),
        (
            {"open-manus": "repl-one", "deleted-app": "repl-two"},
            {"open-manus": "repl-one"},
            "deleted-app",
            "unknown project",
        ),
    ),
)
async def test_dispatch_loop_does_not_call_mcp_for_unresolvable_routes(
        initial_mapping, mapping, project, reason):
    r = redis()
    put_projects(r, initial_mapping)
    # A request can remain queued while an administrator removes its
    # destination.  The dispatcher must fail closed rather than fall back to
    # the default project.
    put_projects(r, mapping)
    req_id = f"unresolvable-{project}"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({"id": req_id, "project": project, "status": "approved"}),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock()

    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_not_awaited()
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_status"] == "failed"
    assert reason in stored["dispatch_error"]


@pytest.mark.asyncio
async def test_unknown_project_retries_after_registry_is_corrected():
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    req_id = "corrected-route"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": "new-app",
            "status": "approved",
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})

    await run_dispatch_iteration(mcp, req_id)
    mcp.start_agent_run.assert_not_awaited()
    assert json.loads(r.get(f"devreq:item:{req_id}"))["dispatch_status"] == "failed"

    put_projects(r, {"open-manus": "repl-one", "new-app": "repl-fixed"})
    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_not_awaited()
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_status"] == "failed"
    assert "needs-routing-review" in stored["dispatch_error"]


@pytest.mark.asyncio
async def test_permission_failure_is_stored_without_escaping_dispatch_loop():
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    req_id = "permission-failure"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({"id": req_id, "project": "open-manus", "status": "approved"}),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(
        side_effect=PermissionError("permission denied by provider"),
    )

    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_awaited_once()
    assert mcp.start_agent_run.call_args.kwargs["repl_id"] == "repl-one"
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_status"] == "failed"
    assert stored["dispatch_error"] == replit_mcp.PROVIDER_ACCESS_FAILURE
    assert "permission denied by provider" not in stored["dispatch_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("pending", "denied"))
async def test_dispatch_loop_does_not_start_non_approved_requests(status):
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    req_id = f"not-approved-{status}"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({"id": req_id, "project": "open-manus", "status": status}),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})

    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_not_awaited()
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["status"] == status
    assert "dispatch_status" not in stored


@pytest.mark.asyncio
async def test_dispatch_loop_releases_lease_when_current_record_disappears():
    req_id = "disappearing-record"

    class VanishingRecordRedis(fakeredis.FakeRedis):
        def __init__(self):
            super().__init__(decode_responses=True)
            self.item_reads = 0

        def get(self, key):
            value = super().get(key)
            if key == f"devreq:item:{req_id}":
                self.item_reads += 1
                if self.item_reads == 2:
                    super().delete(key)
                    return None
            return value

    r = VanishingRecordRedis()
    put_projects(r, {"open-manus": "repl-one"})
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({"id": req_id, "project": "open-manus", "status": "approved"}),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})

    await run_dispatch_iteration(mcp, req_id)

    mcp.start_agent_run.assert_not_awaited()
    assert r.get(f"devreq:item:{req_id}") is None


@pytest.mark.asyncio
async def test_pinned_route_survives_changed_registry_on_retry():
    r = redis()
    put_projects(r, {"second-app": "repl-original"})
    req_id = "pinned-retry"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": "second app",
            "status": "approved",
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(
        side_effect=[RuntimeError("temporary provider failure"),
                     {"accepted": True}],
    )

    await run_dispatch_iteration(mcp, req_id)
    first = json.loads(r.get(f"devreq:item:{req_id}"))
    assert first["dispatch_status"] == "failed"
    assert first["dispatch_project"] == "second-app"
    assert first["dispatch_repl_id"] == "repl-original"

    put_projects(r, {"second-app": "repl-reassigned"})
    await run_dispatch_iteration(mcp, req_id)

    assert [call.kwargs["repl_id"]
            for call in mcp.start_agent_run.await_args_list] == [
                "repl-original",
            ]
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_status"] == "failed"
    assert stored["dispatch_repl_id"] == "repl-original"


@pytest.mark.asyncio
async def test_pinned_retry_checks_lease_before_provider_call():
    r = redis()
    put_projects(r, {"second-app": "repl-original"})
    req_id = "pinned-retry-lease-lost"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": "second app",
            "status": "approved",
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(
        side_effect=RuntimeError("temporary provider failure"),
    )

    # The first attempt pins and fails.  The retry still has the durable
    # destination but loses the token-CAS ownership check immediately before
    # the provider call.
    await run_dispatch_iteration(mcp, req_id, renew_result=True)
    await run_dispatch_iteration(mcp, req_id, renew_result=False)

    mcp.start_agent_run.assert_awaited_once()
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_repl_id"] == "repl-original"


@pytest.mark.asyncio
async def test_failed_pin_recovery_checks_lease_before_provider_call():
    r = redis()
    put_projects(r, {"second-app": "repl-original"})
    req_id = "failed-pin-lease-lost"
    item = {
        "id": req_id,
        "project": "second app",
        "status": "approved",
    }
    r.set(f"devreq:item:{req_id}", json.dumps(item), ex=86400)

    def failed_pin_with_concurrent_recovery(
            redis_client, request_id, token, original, project, repl_id):
        recovered = dict(original)
        recovered["dispatch_project"] = project
        recovered["dispatch_repl_id"] = repl_id
        redis_client.set(
            f"devreq:item:{request_id}", json.dumps(recovered), ex=86400)
        return False

    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})

    await run_dispatch_iteration(
        mcp, req_id, renew_result=False,
        pin_result=failed_pin_with_concurrent_recovery)

    mcp.start_agent_run.assert_not_awaited()
    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_repl_id"] == "repl-original"


def test_unknown_deleted_project_without_a_pin_is_retryable_configuration_error():
    r = redis()
    put_projects(r, {"second-app": "repl-two"})
    with pytest.raises(ValueError, match="unknown project"):
        dev_projects.resolve_project(r, "deleted-project")


@pytest.mark.asyncio
async def test_start_agent_run_uses_explicit_repl_id_and_sanitizes_provider_error():
    r = redis()
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp._mcp_call_tool = AsyncMock(
        side_effect=replit_mcp.ReplitMCPError(
            "provider echoed Bearer secret-token access_token=secret-token"))
    with pytest.raises(replit_mcp.ReplitMCPError):
        await mcp.start_agent_run("change", repl_id="repl-two")
    call = mcp._mcp_call_tool.call_args
    assert call.args[1]["replId"] == "repl-two"
    assert "secret-token" not in mcp.sanitize_text(
        "provider echoed Bearer secret-token access_token=secret-token")
    assert "[redacted]" in mcp.sanitize_text(
        "provider echoed Bearer secret-token access_token=secret-token")


@pytest.mark.asyncio
async def test_mcp_call_tool_never_reflects_raw_provider_error_body():
    r = redis()
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    mcp._access_token = AsyncMock(return_value="oauth-token")
    raw_provider_body = (
        "raw arbitrary provider body Bearer oauth-token access_token=oauth-token")

    responses = [
        httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {}},
            headers={"content-type": "application/json"},
        ),
        httpx.Response(202),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32000, "message": raw_provider_body},
            },
            headers={"content-type": "application/json"},
        ),
    ]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.responses = iter(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return next(self.responses)

    with patch.object(replit_mcp.httpx, "AsyncClient", FakeClient):
        with pytest.raises(replit_mcp.ReplitMCPProviderError) as exc_info:
            await mcp._mcp_call_tool(
                "update_app_using_prompt", {"replId": "repl-two"})

    assert str(exc_info.value) == replit_mcp.PROVIDER_RETRY_FAILURE
    assert raw_provider_body not in str(exc_info.value)
    assert "oauth-token" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_dispatch_does_not_persist_arbitrary_provider_exception_body():
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    req_id = "provider-body-private"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": "open-manus",
            "status": "approved",
            "dispatch_error": "stale error from an earlier attempt",
            "routing_blocked": True,
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    raw_provider_body = (
        "raw arbitrary provider body: Bearer top-secret "
        "and project metadata that must not escape")
    mcp.start_agent_run = AsyncMock(
        side_effect=RuntimeError(raw_provider_body),
    )

    await run_dispatch_iteration(mcp, req_id)

    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_error"] == replit_mcp.PROVIDER_RETRY_FAILURE
    assert raw_provider_body not in json.dumps(stored)
    assert "top-secret" not in json.dumps(stored)


@pytest.mark.asyncio
async def test_success_stores_minimal_result_and_clears_stale_provider_error():
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    req_id = "minimal-provider-result"
    r.set(
        f"devreq:item:{req_id}",
        json.dumps({
            "id": req_id,
            "project": "open-manus",
            "status": "approved",
            "dispatch_error": "old provider failure",
            "routing_blocked": True,
        }),
        ex=86400,
    )
    mcp = replit_mcp.ReplitMCP(r, lambda x: x, lambda x: x, "")
    raw_provider_result = {
        "accepted": True,
        "body": "raw arbitrary provider result",
        "access_token": "result-secret",
    }
    mcp.start_agent_run = AsyncMock(return_value=raw_provider_result)

    await run_dispatch_iteration(mcp, req_id)

    stored = json.loads(r.get(f"devreq:item:{req_id}"))
    assert stored["dispatch_result"] == {
        "accepted": True,
        "summary": "Replit Agent accepted the request",
    }
    assert "dispatch_error" not in stored
    assert "raw arbitrary provider result" not in json.dumps(stored)
    assert "result-secret" not in json.dumps(stored)


def test_destination_prompt_does_not_assume_fleet_or_shared_redis():
    prompt = replit_mcp._build_prompt({
        "id": "9",
        "project": "SECOND   App",
        "title": "Add endpoint",
        "description": "Implement the endpoint.",
        "agent": "tester",
    })
    assert "second-app" in prompt
    assert "Open Manus fleet" not in prompt
    assert "Redis" not in prompt
    assert "unrelated projects" in prompt


def test_release_lease_is_token_compare_and_delete():
    class EvalRedis:
        def __init__(self):
            self.args = None

        def eval(self, *args):
            self.args = args
            return 1

    r = EvalRedis()
    assert replit_mcp.release_lease(r, "req-1", "lease-token") is True
    assert r.args == (
        replit_mcp._RELEASE_LEASE_SCRIPT,
        1,
        f"{replit_mcp.K_LEASE}req-1",
        "lease-token",
    )


@pytest.mark.asyncio
async def test_missing_oauth_then_reconnect_is_safe_to_retry():
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    r.set("devreq:item:oauth-retry", json.dumps({
        "id": "oauth-retry", "project": "open-manus", "status": "approved"}))
    mcp = replit_mcp.ReplitMCP(r, str, str, "")
    with patch.object(replit_mcp.httpx, "AsyncClient",
                      side_effect=AssertionError("Network forbidden without OAuth")) as http:
        await run_dispatch_iteration(mcp, "oauth-retry")
        http.assert_not_called()
    failed = json.loads(r.get("devreq:item:oauth-retry"))
    assert failed["provider_disposition"] == "not_issued"
    assert not failed.get("provider_attempt_at")
    assert failed["dispatch_error"] == replit_mcp.PROVIDER_AUTH_FAILURE
    assert replit_mcp.admin_enqueue(r, "oauth-retry", force=True) == 1

    # Reconnect is represented by a stored test-only token. Only the actual
    # network transport is mocked; auth, MCP initialization and call routing run.
    r.set(replit_mcp.K_TOKENS, json.dumps({"access_token": "test-only"}))
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            calls.append(kwargs["json"])
            return httpx.Response(200, json={"result": {}})
    with patch.object(replit_mcp.httpx, "AsyncClient", Client):
        await run_dispatch_iteration(mcp, "oauth-retry")
    tools = [c for c in calls if c["method"] == "tools/call"]
    assert len(tools) == 1
    assert tools[0]["params"]["arguments"]["replId"] == "repl-one"
    started = json.loads(r.get("devreq:item:oauth-retry"))
    assert started["dispatch_status"] == "started"
    assert started["provider_attempt_at"]
    assert started["provider_attempt_history"][0]["disposition"] == "not_issued"
    with pytest.raises(ValueError, match="already issued"):
        replit_mcp.admin_enqueue(r, "oauth-retry", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_method", ["initialize", "tools/call"])
async def test_transport_distinguishes_not_issued_from_ambiguous(fail_method):
    r = redis()
    mcp = replit_mcp.ReplitMCP(r, str, str, "")
    mcp._access_token = AsyncMock(return_value="test-only")
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            if kwargs["json"]["method"] == fail_method:
                raise TimeoutError("not safe to echo")
            return httpx.Response(200, json={"result": {}})
    with patch.object(replit_mcp.httpx, "AsyncClient", Client):
        with pytest.raises(replit_mcp.ReplitMCPProviderError) as err:
            await mcp.start_agent_run("change", repl_id="repl-one")
    assert isinstance(err.value, replit_mcp.ReplitMCPNotIssuedError) == (fail_method == "initialize")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["approval_digest", "owner_confirmation", "wrong_owner"])
async def test_historical_scoped_approval_requires_fresh_owner_confirmation(missing):
    from tools import dev_requests as dr
    from services.vault import dev_routing
    r = redis()
    put_projects(r, {"open-manus": "repl-one"})
    item = {
        "id": "historical", "agent": "tester", "title": "Historical",
        "description": "May not be the reviewed content", "status": "approved",
        "project": "open-manus", "work_scope": "fleet_platform",
        "submitted_repl_id": "repl-one", "dispatch_project": "open-manus",
        "dispatch_repl_id": "repl-one", "decided_by": "owner", "decided_at": 1,
        "owner_confirmation": {"version": 1, "owner_id": "owner"},
    }
    item["approval_digest"] = content_digest(item)
    if missing == "wrong_owner":
        item["decided_by"] = "historical-reviewer"
    else:
        item.pop(missing)
    r.set("devreq:item:historical", json.dumps(item))
    r.rpush("devreq:approved", "historical")
    assert replit_mcp.sweep_dispatch_backlog(r) == []
    for force in (False, True):
        with pytest.raises(ValueError, match="needs-routing-review"):
            replit_mcp.admin_enqueue(r, "historical", force)
    assert r.llen(replit_mcp.DISPATCH_QUEUE) == 0
    mcp = replit_mcp.ReplitMCP(r, str, str, "")
    mcp.start_agent_run = AsyncMock(return_value={"accepted": True})
    await run_dispatch_iteration(mcp, "historical")
    mcp.start_agent_run.assert_not_awaited()
    blocked = json.loads(r.get("devreq:item:historical"))
    assert blocked["routing_blocked"]
    corrected = dev_routing.correct_route(
        r, "historical", "fleet_platform", "open-manus",
        dev_routing.review_token(r, blocked), "owner")
    with patch.object(dr, "_redis", return_value=r):
        dr.set_status("historical", "approved", "owner",
                      dev_routing.review_token(r, corrected))
    await run_dispatch_iteration(mcp, "historical")
    mcp.start_agent_run.assert_awaited_once()
    assert mcp.start_agent_run.call_args.kwargs["repl_id"] == "repl-one"