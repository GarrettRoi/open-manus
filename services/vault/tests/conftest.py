"""Test bootstrap: use a real Redis when reachable, else fall back to an
in-process fakeredis so the suite can run in environments without Redis.

Loaded before the test modules import ``app``, so patching
``redis.from_url`` here affects the app's connection.
"""
import os

import redis as _redis

_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def _redis_reachable() -> bool:
    try:
        _redis.from_url(_REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


if not _redis_reachable():
    import fakeredis

    _server = fakeredis.FakeServer()
    _orig_from_url = _redis.from_url

    def _fake_from_url(url, **kwargs):
        kwargs.pop("socket_connect_timeout", None)
        return fakeredis.FakeRedis(server=_server, **kwargs)

    _redis.from_url = _fake_from_url
