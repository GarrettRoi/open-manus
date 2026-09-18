"""Focused tests for synchronized proactive Google OAuth refresh."""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import oauth  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)

    def eval(self, script, count, key, owner):
        if self.values.get(key) == owner:
            self.delete(key)
            return 1
        return 0

    def hset(self, key, mapping=None, *args):
        target = self.hashes.setdefault(key, {})
        if mapping:
            target.update(mapping)
        elif len(args) == 2:
            target[args[0]] = args[1]


class FakeStore:
    def __init__(self, connections):
        self.r = FakeRedis()
        self.connections = {c["id"]: dict(c) for c in connections}
        self.secrets = {c["id"]: dict(c["secrets"]) for c in connections}

    def list_ids(self):
        return list(self.connections)

    def get(self, cid):
        if cid not in self.connections:
            return None
        conn = dict(self.connections[cid])
        conn.pop("secrets", None)
        conn.update(self.r.hashes.get(f"vault:conn:{cid}", {}))
        return conn

    def get_secrets(self, cid):
        return dict(self.secrets[cid])

    def set_secrets(self, cid, value):
        self.secrets[cid] = dict(value)


def connection(cid="GOOGLE", service="google", refresh="refresh-old"):
    secrets = {
        "access_token": "access-old",
        "client_id": "client",
        "client_secret": "client-secret",
        "expires_at": time.time() + 5,
    }
    if refresh is not None:
        secrets["refresh_token"] = refresh
    return {
        "id": cid, "service": service, "status": "ready",
        "auth": {"kind": "oauth2"}, "secrets": secrets,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updated,expected_refresh",
    [
        ({"access_token": "access-new", "expires_at": time.time() + 3600},
         "refresh-old"),
        ({"access_token": "access-new", "refresh_token": "refresh-rotated",
          "expires_at": time.time() + 3600}, "refresh-rotated"),
    ],
)
async def test_refresh_merge_preserves_or_rotates_refresh_token(
        monkeypatch, updated, expected_refresh):
    store = FakeStore([connection()])

    async def fake_refresh(*args, **kwargs):
        return dict(updated)

    monkeypatch.setattr(oauth, "refresh_tokens", fake_refresh)
    token, changed = await oauth.refresh_connection(
        "google", "GOOGLE", store, conn=store.get("GOOGLE"),
        within_seconds=600)
    assert (token, changed) == ("access-new", True)
    assert store.secrets["GOOGLE"]["refresh_token"] == expected_refresh
    assert store.secrets["GOOGLE"]["client_secret"] == "client-secret"
    metadata = store.r.hashes["vault:conn:GOOGLE"]
    assert metadata["oauth_refresh_status"] == "ok"
    assert metadata["oauth_next_retry_at"] == ""
    assert metadata["oauth_refresh_at"]


@pytest.mark.asyncio
async def test_refresh_without_expiry_drops_previous_token_expiry(monkeypatch):
    store = FakeStore([connection()])

    async def fake_refresh(*args, **kwargs):
        return {"access_token": "access-without-declared-expiry"}

    monkeypatch.setattr(oauth, "refresh_tokens", fake_refresh)
    await oauth.refresh_connection(
        "google", "GOOGLE", store, within_seconds=600)
    assert "expires_at" not in store.secrets["GOOGLE"]
    assert store.secrets["GOOGLE"]["refresh_token"] == "refresh-old"


@pytest.mark.asyncio
async def test_lazy_and_proactive_refresh_share_one_connection_lock(monkeypatch):
    store = FakeStore([connection()])
    calls = 0

    async def fake_refresh(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.05)
        return {
            "access_token": "winner-access",
            "refresh_token": "winner-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(oauth, "refresh_tokens", fake_refresh)
    first, second = await asyncio.gather(
        oauth.refresh_connection("google", "GOOGLE", store,
                                 within_seconds=600),
        oauth.get_valid_access_token("google", "GOOGLE", store),
    )
    assert calls == 1
    assert first[0] == second[0] == "winner-access"
    assert store.secrets["GOOGLE"]["refresh_token"] == "winner-refresh"


@pytest.mark.asyncio
async def test_explicit_reauth_supersedes_stale_inflight_refresh(monkeypatch):
    store = FakeStore([connection()])
    refresh_started = asyncio.Event()
    allow_refresh_response = asyncio.Event()

    async def slow_refresh(*args, **kwargs):
        refresh_started.set()
        await allow_refresh_response.wait()
        return {
            "access_token": "stale-access",
            "refresh_token": "stale-rotated-refresh",
            "expires_at": time.time() + 3600,
        }

    async def new_login(*args, **kwargs):
        return {
            "access_token": "new-account-access",
            "refresh_token": "new-account-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(oauth, "refresh_tokens", slow_refresh)
    refresh_task = asyncio.create_task(oauth.refresh_connection(
        "google", "GOOGLE", store, within_seconds=600))
    await refresh_started.wait()
    monkeypatch.setattr(oauth, "exchange_code", new_login)
    saved = await oauth.exchange_and_store_code(
        "google", "GOOGLE", "code", "https://vault.example", store)
    allow_refresh_response.set()
    token, changed = await refresh_task

    assert saved["refresh_token"] == "new-account-refresh"
    assert (token, changed) == ("new-account-access", False)
    assert store.secrets["GOOGLE"]["refresh_token"] == "new-account-refresh"


@pytest.mark.asyncio
async def test_explicit_reauth_drops_old_refresh_when_provider_omits_it(monkeypatch):
    store = FakeStore([connection(refresh="old-account-refresh")])

    async def new_login(*args, **kwargs):
        return {
            "access_token": "new-account-access",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(oauth, "exchange_code", new_login)
    saved = await oauth.exchange_and_store_code(
        "google", "GOOGLE", "code", "https://vault.example", store)
    assert "refresh_token" not in saved
    assert "refresh_token" not in store.secrets["GOOGLE"]


@pytest.mark.asyncio
async def test_deleted_connection_is_not_recreated_by_inflight_refresh(monkeypatch):
    store = FakeStore([connection()])
    refresh_started = asyncio.Event()
    allow_refresh_response = asyncio.Event()

    async def slow_refresh(*args, **kwargs):
        refresh_started.set()
        await allow_refresh_response.wait()
        return {
            "access_token": "stale-access",
            "refresh_token": "stale-refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(oauth, "refresh_tokens", slow_refresh)
    task = asyncio.create_task(oauth.refresh_connection(
        "google", "GOOGLE", store, within_seconds=600))
    await refresh_started.wait()
    store.connections.pop("GOOGLE")
    allow_refresh_response.set()
    with pytest.raises(oauth.OAuthRefreshError) as caught:
        await task
    assert caught.value.transient
    assert store.get("GOOGLE") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reauth", "delete"])
async def test_stale_sweep_failure_does_not_mutate_new_or_deleted_record(
        monkeypatch, change):
    store = FakeStore([connection()])

    async def stale_failure(*args, **kwargs):
        if change == "reauth":
            store.secrets["GOOGLE"] = {
                "access_token": "new-account-access",
                "refresh_token": "new-account-refresh",
                "expires_at": time.time() + 3600,
            }
            store.connections["GOOGLE"]["status"] = "ready"
        else:
            store.connections.pop("GOOGLE")
        raise oauth.OAuthRefreshError(
            "stale invalid grant", reason="invalid_grant")

    monkeypatch.setattr(oauth, "refresh_connection", stale_failure)
    result = await oauth.refresh_ready_google_connections(store)
    assert result["needs_reauth"] == 0
    if change == "reauth":
        assert store.get("GOOGLE")["status"] == "ready"
        assert "vault:conn:GOOGLE" not in store.r.hashes
    else:
        assert store.get("GOOGLE") is None
        assert "vault:conn:GOOGLE" not in store.r.hashes


@pytest.mark.asyncio
async def test_lock_release_failure_never_deletes_possible_successor():
    class FailingEvalRedis:
        def __init__(self):
            self.deleted = []

        def eval(self, *args):
            raise RuntimeError("eval unavailable")

        def get(self, key):
            return "successor-owner"

        def delete(self, key):
            self.deleted.append(key)

    redis = FailingEvalRedis()
    await oauth._release_refresh_lock(redis, "lock", "old-owner")
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_missing_refresh_marks_reauth_without_exposing_secret():
    store = FakeStore([connection(refresh=None)])
    result = await oauth.refresh_ready_google_connections(store, retries=0)
    state = store.r.hashes["vault:conn:GOOGLE"]
    assert result["needs_reauth"] == 1
    assert state["status"] == "needs_reauth"
    assert "access-old" not in str(state)
    assert "client-secret" not in str(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("transient,expected_status", [
    (False, "needs_reauth"),
    (True, "ready"),
])
async def test_invalid_grant_is_permanent_but_transient_is_retryable(
        monkeypatch, transient, expected_status):
    store = FakeStore([connection()])

    async def fail(*args, **kwargs):
        raise oauth.OAuthRefreshError(
            "sanitized", transient=transient,
            reason="provider_unavailable" if transient else "invalid_grant")

    monkeypatch.setattr(oauth, "refresh_connection", fail)
    result = await oauth.refresh_ready_google_connections(store)
    state = store.r.hashes["vault:conn:GOOGLE"]
    assert state.get("status", "ready") == expected_status
    assert result["transient" if transient else "needs_reauth"] == 1
    assert bool(state["oauth_next_retry_at"]) is transient
    assert state["oauth_last_attempt_at"]
    assert "access-old" not in str(state)
    assert "client-secret" not in str(state)


@pytest.mark.asyncio
async def test_scheduler_covers_combined_and_every_existing_dedicated_kind(monkeypatch):
    services = [
        "google", "google_gmail", "google_drive", "google_sheets",
        "google_docs", "google_slides", "google_forms", "google_calendar",
        "google_tasks", "google_people", "google_meet", "google_app_script",
    ]
    store = FakeStore([
        connection(f"C{i}", service=service) for i, service in enumerate(services)
    ])
    seen = []

    async def refresh(service, cid, *args, **kwargs):
        seen.append(service)
        return "new-access", True

    monkeypatch.setattr(oauth, "refresh_connection", refresh)
    result = await oauth.refresh_ready_google_connections(store)
    assert set(seen) == set(services)
    assert result["refreshed"] == len(services)


def test_token_expiry_handles_preexpiry_and_malformed_values():
    assert oauth.token_expiring({"expires_at": time.time() + 30}, 60)
    assert not oauth.token_expiring({"expires_at": time.time() + 120}, 60)
    assert oauth.token_expiring({"expires_at": "not-a-timestamp"}, 60)


def test_combined_google_oauth_includes_dedicated_hosts_and_scopes():
    from catalog import CATALOG

    combined_hosts = set(CATALOG["google"]["allowed_hosts"])
    combined_scopes = set(CATALOG["google"]["oauth"]["scopes"])
    for name, preset in CATALOG.items():
        if not name.startswith("google_"):
            continue
        assert set(preset["allowed_hosts"]) - {"oauth2.googleapis.com"} <= combined_hosts
        assert set(preset["oauth"]["scopes"]) <= combined_scopes


class FakeResponse:
    def __init__(self, status, payload, text):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,transient,reason",
    [
        (FakeResponse(400, {"error": "invalid_grant"},
                      "echo refresh-super-secret"), False, "invalid_grant"),
        (FakeResponse(503, {"error": "temporarily_unavailable"},
                      "echo refresh-super-secret"), True, "provider_unavailable"),
        (FakeResponse(200, {"error": "temporarily_unavailable"},
                      "echo refresh-super-secret"), True, "provider_unavailable"),
        (FakeResponse(200, {"error": "server_error"},
                      "echo refresh-super-secret"), True, "provider_unavailable"),
        (FakeResponse(200, {"error": "rate_limit"},
                      "echo refresh-super-secret"), True, "provider_unavailable"),
        (FakeResponse(200, {"error": "slow_down"},
                      "echo refresh-super-secret"), True, "provider_unavailable"),
    ],
)
async def test_provider_error_classification_never_exposes_body(
        monkeypatch, response, transient, reason):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return response

    monkeypatch.setattr(oauth.httpx, "AsyncClient", Client)
    with pytest.raises(oauth.OAuthRefreshError) as caught:
        await oauth.refresh_tokens("google", {
            "refresh_token": "refresh-super-secret",
            "client_id": "client",
            "client_secret": "client-secret",
        })
    assert caught.value.transient is transient
    assert caught.value.reason == reason
    assert "refresh-super-secret" not in str(caught.value)
    assert response.text not in str(caught.value)


@pytest.mark.asyncio
async def test_transient_200_error_payload_is_retried(monkeypatch):
    store = FakeStore([connection()])
    attempts = 0

    async def eventually_succeeds(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise oauth.OAuthRefreshError(
                "OAuth token refresh is temporarily unavailable",
                transient=True, reason="provider_unavailable")
        return {
            "access_token": "access-after-retry",
            "expires_at": time.time() + 3600,
        }

    async def no_delay(*args, **kwargs):
        return None

    monkeypatch.setattr(oauth, "refresh_tokens", eventually_succeeds)
    monkeypatch.setattr(oauth.asyncio, "sleep", no_delay)
    token, changed = await oauth.refresh_connection(
        "google", "GOOGLE", store, within_seconds=600, retries=2)
    assert attempts == 3
    assert (token, changed) == ("access-after-retry", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    ["not", "an", "object"],
    {"access_token": "new-access", "expires_in": "not-a-number"},
    {"access_token": "new-access", "expires_in": 0},
    {"access_token": "new-access", "expires_in": float("inf")},
])
async def test_invalid_refresh_shape_or_expiry_is_retryable(monkeypatch, payload):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return FakeResponse(200, payload, "secret provider body")

    monkeypatch.setattr(oauth.httpx, "AsyncClient", Client)
    with pytest.raises(oauth.OAuthRefreshError) as caught:
        await oauth.refresh_tokens("google", {
            "refresh_token": "refresh-super-secret",
            "client_id": "client",
            "client_secret": "client-secret",
        })
    assert caught.value.transient
    assert caught.value.reason == "invalid_response"
    assert "secret provider body" not in str(caught.value)