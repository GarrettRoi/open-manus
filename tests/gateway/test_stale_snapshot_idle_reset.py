"""Regression tests for #104 — false mid-conversation idle resets.

Root cause: on ephemeral hosts the on-disk gateway state (state.db /
sessions.json) is restored from a Redis snapshot at boot.  When the memory
sync silently stops pushing state.db (it exceeded the size cap, so the OLD
blob stayed in Redis), every redeploy restores a days-old routing table.
The idle reset policy compares ``entry.updated_at`` against
``idle_minutes`` — so the FIRST message of an actively used conversation
after a redeploy tripped the "inactive for 24h" auto-reset.

The gateway-side safety clamp: every entry rehydrated from disk at startup
gets a runtime-only ``rehydrated_at`` stamp, and the *idle* check uses
``max(updated_at, rehydrated_at)``.  A stale snapshot then merely restarts
the idle clock at boot instead of wiping an active conversation.  The daily
reset intentionally keeps using the raw ``updated_at``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore


def _make_store(tmp_path, mode="both", idle_minutes=1440, at_hour=4):
    config = GatewayConfig()
    policy = config.get_reset_policy(platform=Platform.DISCORD, session_type="dm")
    policy.mode = mode
    policy.idle_minutes = idle_minutes
    policy.at_hour = at_hour
    store = SessionStore.__new__(SessionStore)
    store.config = config
    store._entries = {}
    store._has_active_processes_fn = None
    return store


def _entry(updated_at, rehydrated_at=None):
    return SessionEntry(
        session_key="agent:main:discord:dm:user1",
        session_id="sess-1",
        created_at=updated_at,
        updated_at=updated_at,
        platform=Platform.DISCORD,
        chat_type="dm",
        rehydrated_at=rehydrated_at,
    )


def _source():
    return SessionSource(platform=Platform.DISCORD, user_id="user1", chat_id="c1")


class TestIdleClampOnRehydratedEntries:
    def test_stale_snapshot_rehydrated_this_boot_does_not_idle_reset(self, tmp_path):
        """A 3-day-old updated_at restored from a stale snapshot must NOT
        trigger the idle reset when the entry was rehydrated at boot."""
        store = _make_store(tmp_path, mode="idle")
        now = datetime.now()
        entry = _entry(
            updated_at=now - timedelta(days=3),
            rehydrated_at=now - timedelta(minutes=5),  # booted 5 min ago
        )
        assert store._should_reset(entry, _source()) is None
        assert store._is_session_expired(entry) is False

    def test_genuinely_idle_after_boot_still_resets(self, tmp_path):
        """Once idle_minutes pass after boot with no activity, idle reset
        fires normally — the clamp only forgives pre-boot staleness."""
        store = _make_store(tmp_path, mode="idle", idle_minutes=60)
        now = datetime.now()
        entry = _entry(
            updated_at=now - timedelta(days=3),
            rehydrated_at=now - timedelta(hours=2),  # 2h since boot, 1h policy
        )
        assert store._should_reset(entry, _source()) == "idle"
        assert store._is_session_expired(entry) is True

    def test_non_rehydrated_entry_unchanged(self, tmp_path):
        """Entries created during this process (no rehydrated_at) keep the
        original idle semantics."""
        store = _make_store(tmp_path, mode="idle", idle_minutes=60)
        now = datetime.now()
        assert store._should_reset(_entry(now - timedelta(hours=2)), _source()) == "idle"
        assert store._should_reset(_entry(now - timedelta(minutes=10)), _source()) is None

    def test_daily_reset_not_suppressed_by_clamp(self, tmp_path):
        """Daily reset still uses raw updated_at: last activity before today's
        boundary resets on the next message even if the entry was rehydrated
        after the boundary."""
        store = _make_store(tmp_path, mode="daily", at_hour=0)
        now = datetime.now()
        entry = _entry(
            updated_at=now - timedelta(days=1, hours=1),
            rehydrated_at=now - timedelta(minutes=1),
        )
        assert store._should_reset(entry, _source()) == "daily"


class TestRehydratedAtNotPersisted:
    def test_rehydrated_at_never_round_trips(self):
        """rehydrated_at must not serialize — persisting it would let it go
        stale inside the very snapshots the clamp defends against."""
        now = datetime.now()
        entry = _entry(now, rehydrated_at=now)
        data = entry.to_dict()
        assert "rehydrated_at" not in data
        assert SessionEntry.from_dict(json.loads(json.dumps(data))).rehydrated_at is None


class TestLoadStampsRehydratedAt:
    def test_ensure_loaded_stamps_all_loaded_entries(self, tmp_path):
        """Entries loaded from sessions.json at startup get rehydrated_at."""
        old = datetime.now() - timedelta(days=3)
        entry = _entry(old)
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "sessions.json").write_text(
            json.dumps({entry.session_key: entry.to_dict()})
        )

        store = SessionStore.__new__(SessionStore)
        store.config = GatewayConfig()
        store.sessions_dir = sessions_dir
        store._entries = {}
        store._loaded = False
        store._db = None

        store._ensure_loaded_locked()

        loaded = store._entries[entry.session_key]
        assert loaded.rehydrated_at is not None
        assert loaded.rehydrated_at > old
        # And the stale updated_at no longer trips the idle policy
        store._has_active_processes_fn = None
        policy = store.config.get_reset_policy(
            platform=Platform.DISCORD, session_type="dm"
        )
        policy.mode = "idle"
        assert store._should_reset(loaded, _source()) is None
