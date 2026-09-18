"""Hermetic vault-test bootstrap.

This file is loaded before test modules import ``app``.  Redis construction is
therefore replaced unconditionally: a developer's or CI worker's ``REDIS_URL``
must never make a test run connect to a deployed vault.
"""
import os
import sys

import fakeredis
import pytest
import redis as _redis

# Do not leave live provider/public configuration visible while importing the
# vault application.  Tests that need a public URL set ``app.PUBLIC_URL``
# explicitly.  Clearing Railway credentials also makes startup's agent
# initialization incapable of pushing test tokens to deployed services.
for _name in (
    "VAULT_PUBLIC_URL",
    "RAILWAY_PUBLIC_DOMAIN",
    "RAILWAY_ACCOUNT_API",
    "VAULT_ADMIN_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
):
    os.environ.pop(_name, None)

# Never import production admin/encryption credentials into the app under
# test.  This is a valid Fernet key encoding 32 ASCII zero bytes.
os.environ["VAULT_ADMIN_PASSWORD"] = "vault-test-admin-password"
os.environ["VAULT_MASTER_KEY"] = (
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
)

# Some runtime code requires REDIS_URL to be present before it calls
# redis.from_url.  Use an intentionally non-resolvable test URL; the patched
# constructors below never open a socket.
os.environ["REDIS_URL"] = "redis://fakeredis.invalid/0"

_server = fakeredis.FakeServer()


def _fake_client(**kwargs):
    # Connection-only kwargs are meaningless (and may be unsupported) in
    # fakeredis.  Preserve behavioral options such as decode_responses.
    for name in (
        "socket_connect_timeout",
        "socket_timeout",
        "retry_on_timeout",
        "health_check_interval",
        "ssl",
    ):
        kwargs.pop(name, None)
    kwargs.pop("connection_pool", None)
    return fakeredis.FakeRedis(server=_server, **kwargs)


def _fake_from_url(url, **kwargs):
    return _fake_client(**kwargs)


def _fake_redis_class_from_url(cls, url, **kwargs):
    return _fake_client(**kwargs)


# Cover both construction styles used by redis-py callers.  This is deliberately
# process-wide for the test process and has no live-Redis opt-in.
_redis.from_url = _fake_from_url
_redis.Redis.from_url = classmethod(_fake_redis_class_from_url)


@pytest.fixture(scope="session", autouse=True)
def _disable_vault_background_runtime():
    """Prevent TestClient startup from dispatching, refreshing, or backing up."""
    patcher = pytest.MonkeyPatch()

    async def _disabled_loop(*args, **kwargs):
        return None

    # Test modules are imported during collection, before session fixtures run.
    # Account for either import spelling used by the suite.
    for module_name in ("app", "services.vault.app"):
        vault_app = sys.modules.get(module_name)
        if vault_app is None:
            continue
        patcher.setattr(
            vault_app.vault_backup, "backup_loop", _disabled_loop)
        patcher.setattr(
            vault_app.oauth_mod, "google_refresh_loop", _disabled_loop)
        patcher.setattr(
            vault_app.replit_mcp_mod, "dispatch_loop", _disabled_loop)
        patcher.setattr(
            vault_app.replit_mcp_mod, "sweep_dispatch_backlog",
            lambda *args, **kwargs: [])

    yield
    patcher.undo()