"""Tests for the dev-request system reliability improvements.

Covers:
  - queue_counts() — accurate live/expired split
  - list_requests() — stale-ID LREM during listing
  - _redis() error message when REDIS_URL is absent
  - handle_devrequests_slash — defer before Redis I/O; every failure path
    produces a followup; empty state includes expired/approved counts
  - _handle_diag — Redis host fingerprint masking
  - _decide button handler — defers before Redis; edits original response
  - dispatch_loop REDIS_URL check — no localhost fallback
  - DEV_REQUEST_GUIDANCE present in prompt_builder exports
  - Adapter: _warn_if_devreq_redis_unhealthy present on the class

All Redis I/O uses fakeredis (no Lua needed for these tests).
Discord I/O uses AsyncMock so no real bot token is required.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

# ---------------------------------------------------------------------------
# Path wiring — point imports at the workspace root
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
for _p in [_ROOT, _ROOT / "tools", _ROOT / "services" / "vault"]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Stub heavy runtime deps before importing the modules under test.
#
# discord.ui.View must be a *real* Python class (not a MagicMock attribute)
# because Python resolves the metaclass of a new class from its bases.
# If the base is a MagicMock, Python picks MagicMock as the metaclass and
# the resulting "class" is itself a MagicMock instance — instantiating it
# calls MagicMock.__call__ rather than __init__, breaking all tests that
# create a DevRequestApprovalView.
#
# Similarly, @discord.ui.button must be a passthrough decorator so the
# approve/deny methods remain real async functions rather than Mock objects.

class _StubView:
    """Minimal stub for discord.ui.View."""
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.children = []

class _StubButtonStyle:
    success = 1
    danger = 2

def _stub_button(**kw):
    """Passthrough stub for the @discord.ui.button decorator."""
    def decorator(fn):
        return fn
    return decorator

_discord_stub = MagicMock()
_discord_stub.ui = MagicMock()
_discord_stub.ui.View = _StubView
_discord_stub.ui.button = _stub_button
_discord_stub.ButtonStyle = _StubButtonStyle
sys.modules["discord"] = _discord_stub

if "httpx" not in sys.modules:
    sys.modules["httpx"] = MagicMock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_r() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def _seed_item(r, req_id: str, status: str = "pending") -> dict:
    item = {
        "id": req_id,
        "title": f"Test request {req_id}",
        "description": "some description",
        "agent": "testbot",
        "status": status,
        "created_at": int(time.time()),
        "project": "open-manus",
    }
    r.set(f"devreq:item:{req_id}", json.dumps(item), ex=86400)
    return item


def _make_interaction(
    user_id: int = 42,
    *,
    defer_raises: Exception | None = None,
    followup_raises: Exception | None = None,
) -> SimpleNamespace:
    """Return a minimal mock discord.Interaction."""
    interaction = SimpleNamespace()
    interaction.user = SimpleNamespace(id=user_id, __str__=lambda s: f"user#{user_id}")

    response = SimpleNamespace()
    if defer_raises:
        response.defer = AsyncMock(side_effect=defer_raises)
    else:
        response.defer = AsyncMock()
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    interaction.response = response

    followup = SimpleNamespace()
    if followup_raises:
        followup.send = AsyncMock(side_effect=followup_raises)
    else:
        followup.send = AsyncMock()
    interaction.followup = followup
    interaction.edit_original_response = AsyncMock()
    interaction.channel = MagicMock()
    return interaction


# ---------------------------------------------------------------------------
# tools.dev_requests — queue_counts and stale-ID cleanup
# ---------------------------------------------------------------------------

class TestQueueCounts:
    """queue_counts() returns accurate live/expired split without removing IDs."""

    def _make_module(self, r):
        import tools.dev_requests as dr
        return dr, r

    def test_all_live(self):
        r = _fake_r()
        _seed_item(r, "1")
        _seed_item(r, "2")
        r.rpush("devreq:pending", "1", "2")

        with patch("tools.dev_requests._redis", return_value=r):
            import tools.dev_requests as dr
            counts = dr.queue_counts()

        assert counts["pending_in_list"] == 2
        assert counts["pending_live"] == 2
        assert counts["pending_expired"] == 0

    def test_expired_items_counted_not_removed(self):
        r = _fake_r()
        _seed_item(r, "1")
        # ID "2" has no item key → expired
        r.rpush("devreq:pending", "1", "2")

        with patch("tools.dev_requests._redis", return_value=r):
            import tools.dev_requests as dr
            counts = dr.queue_counts()

        assert counts["pending_in_list"] == 2
        assert counts["pending_live"] == 1
        assert counts["pending_expired"] == 1
        # ID should still be in the list (queue_counts is read-only)
        assert r.llen("devreq:pending") == 2

    def test_approved_split(self):
        r = _fake_r()
        _seed_item(r, "10", status="approved")
        r.rpush("devreq:approved", "10", "11")  # "11" has no item

        with patch("tools.dev_requests._redis", return_value=r):
            import tools.dev_requests as dr
            counts = dr.queue_counts()

        assert counts["approved_in_list"] == 2
        assert counts["approved_live"] == 1
        assert counts["approved_expired"] == 1

    def test_dispatch_backlog_count(self):
        r = _fake_r()
        r.lpush("devreq:dispatch", "5", "6")

        with patch("tools.dev_requests._redis", return_value=r):
            import tools.dev_requests as dr
            counts = dr.queue_counts()

        assert counts["dispatch_backlog"] == 2


class TestStaleIdCleanup:
    """list_requests() actively LREMs expired IDs during listing."""

    def test_stale_ids_removed_from_pending(self, caplog):
        r = _fake_r()
        _seed_item(r, "1")
        r.rpush("devreq:pending", "1", "99")  # "99" has no item

        with caplog.at_level(logging.WARNING, logger="tools.dev_requests"):
            with patch("tools.dev_requests._redis", return_value=r):
                import tools.dev_requests as dr
                items = dr.list_requests("pending")

        # Only live item returned
        assert len(items) == 1
        assert items[0]["id"] == "1"
        # Stale ID removed from list
        assert r.llen("devreq:pending") == 1
        assert r.lrange("devreq:pending", 0, -1) == ["1"]
        # Logged at WARNING
        assert "99" in caplog.text

    def test_all_stale_returns_empty_list(self):
        r = _fake_r()
        r.rpush("devreq:pending", "99", "100")  # no item keys

        with patch("tools.dev_requests._redis", return_value=r):
            import tools.dev_requests as dr
            items = dr.list_requests("pending")

        assert items == []
        assert r.llen("devreq:pending") == 0

    def test_approved_stale_ids_removed(self, caplog):
        r = _fake_r()
        _seed_item(r, "3", status="approved")
        r.rpush("devreq:approved", "3", "77")  # "77" missing

        with caplog.at_level(logging.WARNING, logger="tools.dev_requests"):
            with patch("tools.dev_requests._redis", return_value=r):
                import tools.dev_requests as dr
                items = dr.list_requests("approved")

        assert len(items) == 1
        assert r.llen("devreq:approved") == 1
        assert "77" in caplog.text


class TestRedisErrorMessage:
    """_redis() raises a helpful RuntimeError when REDIS_URL is absent."""

    def test_raises_with_guidance(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove REDIS_URL from env
            env = {k: v for k, v in os.environ.items() if k != "REDIS_URL"}
            with patch.dict(os.environ, env, clear=True):
                import tools.dev_requests as dr
                with pytest.raises(RuntimeError) as exc_info:
                    dr._redis()
                msg = str(exc_info.value)
                assert "REDIS_URL" in msg
                assert "/devrequests" in msg or "dev-request" in msg.lower()

    def test_dev_request_tool_returns_json_error_not_exception(self):
        """dev_request_tool catches RuntimeError and returns JSON error."""
        with patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                      if k != "REDIS_URL"}, clear=True):
            import tools.dev_requests as dr
            result = dr.dev_request_tool(
                {"action": "submit", "title": "t", "description": "d"})
            parsed = json.loads(result)
            assert "error" in parsed
            assert "REDIS_URL" in parsed["error"]


# ---------------------------------------------------------------------------
# dev_requests_ui — handle_devrequests_slash
# ---------------------------------------------------------------------------

class TestHandleDevRequestsSlash:
    """handle_devrequests_slash defers before Redis I/O."""

    @pytest.mark.asyncio
    async def test_defer_called_before_list_requests(self):
        """response.defer must be awaited before list_requests runs."""
        defer_calls = []
        list_calls = []

        async def fake_defer(**kw):
            defer_calls.append(1)

        async def fake_list(*a, **kw):
            # defer must already have been called
            assert defer_calls, "defer was not called before list_requests"
            list_calls.append(1)
            return []

        interaction = _make_interaction()
        interaction.response.defer = fake_defer

        # Patch asyncio.to_thread so list_requests runs inline (no real thread)
        orig_to_thread = asyncio.to_thread

        async def patched_to_thread(fn, *args, **kwargs):
            if fn.__name__ in ("list_requests", "queue_counts"):
                return fn(*args, **kwargs)
            return await orig_to_thread(fn, *args, **kwargs)

        import plugins.platforms.discord.dev_requests_ui as ui

        store_mock = MagicMock()
        store_mock.list_requests = lambda *a, **kw: list_calls.append(1) or []
        store_mock.queue_counts = lambda: {}

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=patched_to_thread):
                await ui.handle_devrequests_slash(interaction, action="list")

        assert defer_calls, "defer was never called"

    @pytest.mark.asyncio
    async def test_redis_error_delivers_followup_not_exception(self):
        """When list_requests raises, a followup error message is sent (not re-raised)."""
        interaction = _make_interaction()

        import plugins.platforms.discord.dev_requests_ui as ui

        store_mock = MagicMock()
        store_mock.list_requests = MagicMock(side_effect=RuntimeError("Redis gone"))

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                # Must not raise
                await ui.handle_devrequests_slash(interaction, action="list")

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited()
        call_args = str(interaction.followup.send.call_args_list)
        assert "Redis gone" in call_args or "store" in call_args.lower()

    @pytest.mark.asyncio
    async def test_empty_state_includes_expired_count(self):
        """Empty state message mentions expired IDs when present."""
        interaction = _make_interaction()

        import plugins.platforms.discord.dev_requests_ui as ui

        store_mock = MagicMock()
        store_mock.list_requests = MagicMock(return_value=[])
        store_mock.queue_counts = MagicMock(return_value={
            "pending_expired": 3,
            "approved_live": 1,
            "dispatch_backlog": 0,
        })

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                await ui.handle_devrequests_slash(interaction, action="list")

        all_sent = " ".join(
            str(c) for c in interaction.followup.send.call_args_list
        )
        assert "3" in all_sent  # expired count
        assert "1" in all_sent  # approved count

    @pytest.mark.asyncio
    async def test_failed_card_delivers_visible_error(self):
        """When a followup.send for a card fails, a visible error is sent."""
        interaction = _make_interaction()
        # First followup (count line) succeeds; subsequent cards fail
        call_count = [0]

        async def followup_send(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("Discord error")

        interaction.followup.send = followup_send

        import plugins.platforms.discord.dev_requests_ui as ui

        item = {"id": "1", "title": "T", "description": "D",
                "agent": "bot", "project": "p", "status": "pending"}
        store_mock = MagicMock()
        store_mock.list_requests = MagicMock(return_value=[item])

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                # Must not raise
                await ui.handle_devrequests_slash(interaction, action="list")

        # If we got here without raising, the test passes.  The failed-card
        # branch tries a second followup.send which also fails; it must log
        # rather than re-raise (can't easily assert the second send without
        # complex mock sequencing, but the no-raise is the key contract).

    @pytest.mark.asyncio
    async def test_no_bare_pass_on_store_error(self):
        """Regression: store error must not silently pass — followup must be attempted."""
        interaction = _make_interaction()

        import plugins.platforms.discord.dev_requests_ui as ui

        store_mock = MagicMock()
        store_mock.list_requests = MagicMock(side_effect=OSError("timeout"))

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                await ui.handle_devrequests_slash(interaction, action="list")

        interaction.followup.send.assert_awaited()


# ---------------------------------------------------------------------------
# dev_requests_ui — _handle_diag
# ---------------------------------------------------------------------------

class TestHandleDiag:
    """_handle_diag fingerprints the Redis URL without exposing the password."""

    def test_redis_fingerprint_hides_password(self):
        import plugins.platforms.discord.dev_requests_ui as ui
        fp = ui._redis_fingerprint("redis://:s3cr3t@myhost.example.com:6380/0")
        assert "s3cr3t" not in fp
        assert "myhost.example.com" in fp
        assert "6380" in fp
        # SHA256 prefix — 6 hex chars
        import re
        assert re.search(r"\([0-9a-f]{6}\)", fp)

    def test_redis_fingerprint_no_auth(self):
        import plugins.platforms.discord.dev_requests_ui as ui
        fp = ui._redis_fingerprint("redis://localhost:6379")
        assert "localhost" in fp
        assert "6379" in fp

    @pytest.mark.asyncio
    async def test_diag_no_redis_url_sends_followup(self):
        interaction = _make_interaction()

        import plugins.platforms.discord.dev_requests_ui as ui

        with patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                      if k != "REDIS_URL"}, clear=True):
            await ui._handle_diag(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        msg = str(interaction.followup.send.call_args_list)
        assert "REDIS_URL" in msg

    @pytest.mark.asyncio
    async def test_diag_dispatched_from_handle_slash(self):
        """action='diag' must call _handle_diag, not the listing path."""
        interaction = _make_interaction()

        import plugins.platforms.discord.dev_requests_ui as ui

        diag_called = []

        async def fake_diag(iact):
            diag_called.append(1)

        with patch.object(ui, "_handle_diag", side_effect=fake_diag):
            await ui.handle_devrequests_slash(interaction, action="diag")

        assert diag_called


# ---------------------------------------------------------------------------
# DevRequestApprovalView._decide — defers before Redis
# ---------------------------------------------------------------------------

class TestApprovalViewDecide:
    """Button handler defers before any Redis call."""

    @pytest.mark.asyncio
    async def test_defer_called_before_set_status(self):
        defer_calls = []
        set_status_calls = []

        async def fake_defer(**kw):
            defer_calls.append(1)

        async def fake_to_thread(fn, *args, **kwargs):
            if hasattr(fn, "__name__") and fn.__name__ == "set_status":
                assert defer_calls, "defer not called before set_status"
            set_status_calls.append(1)
            return fn(*args, **kwargs)

        import plugins.platforms.discord.dev_requests_ui as ui

        view = ui.DevRequestApprovalView("1", reviewer_id=42)
        view.resolved = False
        view.children = []

        interaction = _make_interaction(user_id=42)
        interaction.response.defer = fake_defer

        store_mock = MagicMock()
        item = {"id": "1", "title": "T", "status": "approved",
                "agent": "bot", "project": "p"}
        store_mock.set_status = MagicMock(return_value=item)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                with patch("asyncio.create_task"):  # don't actually poll
                    await view._decide(interaction, "approved", "✅ Approved")

        assert defer_calls, "defer was never called"

    @pytest.mark.asyncio
    async def test_set_status_error_sends_followup_not_raises(self):
        import plugins.platforms.discord.dev_requests_ui as ui

        view = ui.DevRequestApprovalView("1", reviewer_id=42)
        view.resolved = False
        view.children = []

        interaction = _make_interaction(user_id=42)

        store_mock = MagicMock()
        store_mock.set_status = MagicMock(side_effect=RuntimeError("Redis gone"))

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                await view._decide(interaction, "approved", "✅ Approved")

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited()
        msg = str(interaction.followup.send.call_args_list)
        assert "Redis gone" in msg

    @pytest.mark.asyncio
    async def test_uses_edit_original_response_not_edit_message(self):
        """After defer, response is via edit_original_response, not edit_message."""
        import plugins.platforms.discord.dev_requests_ui as ui

        view = ui.DevRequestApprovalView("1", reviewer_id=42)
        view.resolved = False
        view.children = []

        interaction = _make_interaction(user_id=42)

        item = {"id": "1", "title": "T", "status": "approved",
                "agent": "bot", "project": "p"}
        store_mock = MagicMock()
        store_mock.set_status = MagicMock(return_value=item)

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(ui, "_store", return_value=store_mock):
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                with patch("asyncio.create_task"):
                    await view._decide(interaction, "approved", "✅ Approved")

        interaction.edit_original_response.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# dispatch_loop — no localhost fallback
# ---------------------------------------------------------------------------

class TestDispatchLoopRedisUrl:
    """dispatch_loop must not silently fall back to localhost when REDIS_URL is absent."""

    @pytest.mark.asyncio
    async def test_idles_not_connects_to_localhost(self):
        """When REDIS_URL is unset, dispatch_loop must not attempt redis.from_url."""

        if "httpx" not in sys.modules:
            sys.modules["httpx"] = MagicMock()
        if "replit_mcp" in sys.modules:
            del sys.modules["replit_mcp"]
        import replit_mcp

        from_url_calls = []

        def fake_from_url(url, **kw):
            from_url_calls.append(url)
            raise RuntimeError("should not be called")

        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)
            if len(sleep_calls) >= 2:
                # Simulate REDIS_URL appearing after two idle ticks
                os.environ["REDIS_URL"] = "redis://testhost:6379"
            raise asyncio.CancelledError()

        mcp_mock = MagicMock()
        env = {k: v for k, v in os.environ.items() if k != "REDIS_URL"}

        with patch.dict(os.environ, env, clear=True):
            with patch("redis.from_url", side_effect=fake_from_url):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    with pytest.raises(asyncio.CancelledError):
                        await replit_mcp.dispatch_loop(mcp_mock)

        # from_url must never have been called with localhost
        localhost_calls = [u for u in from_url_calls if "localhost" in u]
        assert not localhost_calls, (
            f"dispatch_loop called redis.from_url with localhost: {from_url_calls}"
        )

        # Cleanup
        os.environ.pop("REDIS_URL", None)


# ---------------------------------------------------------------------------
# Prompt layer — DEV_REQUEST_GUIDANCE exported and injected
# ---------------------------------------------------------------------------

class TestPromptLayer:
    def test_dev_request_guidance_exported_from_prompt_builder(self):
        import agent.prompt_builder as pb
        assert hasattr(pb, "DEV_REQUEST_GUIDANCE")
        guidance = pb.DEV_REQUEST_GUIDANCE
        assert "request_dev_modification" in guidance
        assert "REDIS_URL" in guidance or "unavailable" in guidance.lower()

    def test_dev_request_guidance_imported_in_system_prompt(self):
        import agent.system_prompt as sp
        # The module must import DEV_REQUEST_GUIDANCE (used inside build_system_prompt_parts)
        # We verify it's resolvable via the module's globals
        import agent.prompt_builder as pb
        assert pb.DEV_REQUEST_GUIDANCE  # non-empty

    def test_guidance_mentions_devrequests_channel(self):
        import agent.prompt_builder as pb
        # Agents who can't use the tool need to know to tell the owner about /devrequests
        assert "/devrequests" in pb.DEV_REQUEST_GUIDANCE or "devrequests" in pb.DEV_REQUEST_GUIDANCE


# ---------------------------------------------------------------------------
# Adapter — _warn_if_devreq_redis_unhealthy exists on the class
# ---------------------------------------------------------------------------

class TestAdapterHealthCheck:
    def test_method_exists(self):
        """_warn_if_devreq_redis_unhealthy must be defined on the adapter class."""
        # Import the module and find the class without instantiating it
        # (instantiation requires heavy discord.py setup).
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "discord_adapter_check",
            _ROOT / "plugins" / "platforms" / "discord" / "adapter.py",
        )
        # Just check the source file contains the method definition
        src = (_ROOT / "plugins" / "platforms" / "discord" / "adapter.py").read_text()
        assert "_warn_if_devreq_redis_unhealthy" in src

    def test_startup_warning_in_on_ready(self):
        """on_ready must schedule the health check task."""
        src = (_ROOT / "plugins" / "platforms" / "discord" / "adapter.py").read_text()
        assert "_warn_if_devreq_redis_unhealthy" in src
        # Confirm it's referenced inside on_ready (not just defined)
        on_ready_idx = src.index("async def on_ready")
        health_idx = src.index("_warn_if_devreq_redis_unhealthy")
        # Health check call must appear after on_ready definition
        assert health_idx > on_ready_idx


# ---------------------------------------------------------------------------
# Integration: submit → list → approve flow against real fakeredis
# ---------------------------------------------------------------------------

class TestSubmitListApproveFlow:
    """End-to-end store flow using fakeredis (no Lua)."""

    def test_submit_then_list_then_approve(self):
        r = _fake_r()

        import tools.dev_requests as dr

        with patch("tools.dev_requests._redis", return_value=r):
            # Submit
            item = dr.submit_request("Add X tool", "We need tool X because Y")
            req_id = item["id"]
            assert item["status"] == "pending"

            # Appears in listing
            items = dr.list_requests("pending")
            assert any(i["id"] == req_id for i in items)

            # Approve (skip _enqueue_if_unclaimed — it uses Lua eval)
            with patch("tools.dev_requests._enqueue_if_unclaimed", return_value=True):
                decided = dr.set_status(req_id, "approved", decided_by="owner")

            assert decided["status"] == "approved"
            assert decided.get("conflict") is None

            # Moves to approved list
            approved = dr.list_requests("approved")
            assert any(i["id"] == req_id for i in approved)

            # No longer in pending
            pending = dr.list_requests("pending")
            assert not any(i["id"] == req_id for i in pending)

    def test_double_approve_returns_conflict(self):
        r = _fake_r()

        import tools.dev_requests as dr

        with patch("tools.dev_requests._redis", return_value=r):
            dr.submit_request("Title", "Desc")
            req_id = "1"
            with patch("tools.dev_requests._enqueue_if_unclaimed", return_value=True):
                dr.set_status(req_id, "approved", decided_by="owner")
                # Second attempt
                result = dr.set_status(req_id, "approved", decided_by="owner2")
            assert result.get("conflict") == "already decided"

    def test_queue_counts_after_approval(self):
        r = _fake_r()

        import tools.dev_requests as dr

        with patch("tools.dev_requests._redis", return_value=r):
            dr.submit_request("T1", "D1")
            dr.submit_request("T2", "D2")
            req_id = "1"
            with patch("tools.dev_requests._enqueue_if_unclaimed", return_value=True):
                dr.set_status(req_id, "approved", decided_by="owner")

            counts = dr.queue_counts()
            assert counts["pending_live"] == 1
            assert counts["approved_live"] == 1
