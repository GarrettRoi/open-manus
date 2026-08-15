"""Tests for the fleet-wide lifecycle notice cooldown (shutdown/online spam)."""
import time
from unittest import mock

import fakeredis
import pytest

from gateway import status_notify


@pytest.fixture()
def fake_redis(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(status_notify, "_get_redis", lambda: client)
    monkeypatch.setenv("AGENT_NAME", "harmony")
    return client


def test_first_notice_allowed_then_suppressed(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    assert status_notify.lifecycle_notice_allowed("shutdown") is True
    # Second attempt within the window (e.g. crash loop) is suppressed.
    assert status_notify.lifecycle_notice_allowed("shutdown") is False


def test_shutdown_and_online_have_separate_windows(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    assert status_notify.lifecycle_notice_allowed("shutdown") is True
    # The same restart may still announce its "online" half.
    assert status_notify.lifecycle_notice_allowed("online") is True
    assert status_notify.lifecycle_notice_allowed("online") is False


def test_cooldown_is_per_agent(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    monkeypatch.setenv("AGENT_NAME", "harmony")
    assert status_notify.lifecycle_notice_allowed("online") is True
    monkeypatch.setenv("AGENT_NAME", "lexi")
    assert status_notify.lifecycle_notice_allowed("online") is True


def test_zero_cooldown_disables_throttle(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "0")
    assert status_notify.lifecycle_notice_allowed("shutdown") is True
    assert status_notify.lifecycle_notice_allowed("shutdown") is True


def test_fails_open_without_redis(monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    monkeypatch.setattr(status_notify, "_get_redis", lambda: None)
    assert status_notify.lifecycle_notice_allowed("shutdown") is True
    assert status_notify.lifecycle_notice_allowed("shutdown") is True


def test_fails_open_on_redis_error(monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")

    def _boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(status_notify, "_get_redis", _boom)
    assert status_notify.lifecycle_notice_allowed("online") is True


def test_key_expires_after_window(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    assert status_notify.lifecycle_notice_allowed("shutdown") is True
    key = "hermes:gateway:lifecycle_notice:shutdown:harmony"
    assert 0 < fake_redis.ttl(key) <= 300


def test_release_reopens_window_when_nothing_delivered(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    token = status_notify.reserve_lifecycle_notice("shutdown")
    assert token
    # Nothing was delivered → release; the next restart may announce again.
    status_notify.release_lifecycle_notice("shutdown", token)
    assert status_notify.reserve_lifecycle_notice("shutdown") is not None


def test_release_never_clobbers_another_workers_reservation(fake_redis, monkeypatch):
    monkeypatch.setenv("HERMES_LIFECYCLE_NOTICE_COOLDOWN", "300")
    stale_token = status_notify.reserve_lifecycle_notice("online")
    key = "hermes:gateway:lifecycle_notice:online:harmony"
    fake_redis.delete(key)
    fresh_token = status_notify.reserve_lifecycle_notice("online")
    assert fresh_token and fresh_token != stale_token
    # Releasing with the stale token must not delete the fresh reservation.
    status_notify.release_lifecycle_notice("online", stale_token)
    assert fake_redis.get(key) == fresh_token


def test_release_is_noop_for_open_sentinel(monkeypatch):
    calls = []
    monkeypatch.setattr(status_notify, "_get_redis",
                        lambda: calls.append(1))
    status_notify.release_lifecycle_notice("shutdown", None)
    status_notify.release_lifecycle_notice("shutdown", status_notify._OPEN_TOKEN)
    assert calls == []  # never touched Redis
