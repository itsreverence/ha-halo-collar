from __future__ import annotations

import time

import pytest

from custom_components.halo_collar.api import HaloApiClient, HaloApiError, HaloAuthError


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self, token_status=200):
        self.posts = []
        self.gets = []
        self.puts = []
        self._token_status = token_status

    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data, headers))
        if self._token_status >= 400:
            return FakeResponse(self._token_status, {"error": "invalid_grant"})
        return FakeResponse(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    async def get(self, url, headers=None):
        self.gets.append((url, headers))
        if url.endswith("/pet/my"):
            return FakeResponse(200, [{"id": "pet1"}])
        if url.endswith("/collar/my"):
            return FakeResponse(200, [{"id": "collar1", "telemetry": {}}])
        if url.endswith("/subscription/my"):
            return FakeResponse(200, {"accessLevel": "basic"})
        if url.endswith("/system/server-date-time"):
            return FakeResponse(200, "2026-07-06T00:32:03Z")
        return FakeResponse(404, {})

    async def put(self, url, json=None, headers=None):
        assert json is not None
        self.puts.append((url, json, headers))
        enabled = json["modePatch"]["fencesOn"]
        return FakeResponse(
            200,
            {
                "desiredMode": {"fencesOn": enabled},
                "telemetry": {"mode": {"fencesOn": enabled}},
            },
        )


@pytest.mark.asyncio
async def test_refreshes_expired_token_and_fetches_state():
    session = FakeSession()
    client = HaloApiClient(
        session=session,
        access_token="old",
        refresh_token="refresh",
        expires_at=0,
        client_id="halo.app.android",
        client_secret="secret",
        api_base="https://api.example",
        auth_base="https://auth.example",
    )

    state = await client.async_fetch_state()

    assert state.pets == [{"id": "pet1"}]
    assert state.collars[0]["id"] == "collar1"
    assert state.subscription["accessLevel"] == "basic"
    assert session.posts[0][1]["grant_type"] == "refresh_token"
    assert session.gets[0][1]["Authorization"] == "Bearer new-access"
    assert client.token_snapshot["refresh_token"] == "new-refresh"
    assert client.token_snapshot["expires_at"] > time.time()


def _new_client(session):
    return HaloApiClient(
        session=session,
        access_token="",
        refresh_token="",
        expires_at=0,
        client_id="halo.app.android",
        client_secret="secret",
        api_base="https://api.example",
        auth_base="https://auth.example",
    )


@pytest.mark.asyncio
async def test_login_exchanges_credentials_for_tokens():
    session = FakeSession()
    client = _new_client(session)

    await client.async_login("user@example.com", "hunter2", scope="openid offline_access")

    url, data, headers = session.posts[0]
    assert url.endswith("/connect/token")
    assert data["grant_type"] == "password"
    assert data["username"] == "user@example.com"
    assert data["password"] == "hunter2"
    assert headers["Accept"] == "application/json"
    assert "clientId=halo.app.android" in headers["Halo-Client"]
    assert client.token_snapshot["access_token"] == "new-access"
    assert client.token_snapshot["refresh_token"] == "new-refresh"


@pytest.mark.asyncio
async def test_login_raises_auth_error_on_bad_credentials():
    session = FakeSession(token_status=400)
    client = _new_client(session)

    with pytest.raises(HaloAuthError):
        await client.async_login("user@example.com", "wrong", scope="openid")


class SequencedSession(FakeSession):
    """Session whose GETs walk through a scripted list; the last item repeats."""

    def __init__(self, get_results, token_status=200):
        super().__init__(token_status=token_status)
        self._get_results = list(get_results)

    async def get(self, url, headers=None):
        self.gets.append((url, headers))
        result = self._get_results.pop(0) if len(self._get_results) > 1 else self._get_results[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr("custom_components.halo_collar.api.RETRY_BACKOFF_SECONDS", (0.0, 0.0))


@pytest.mark.asyncio
async def test_retries_transient_server_errors_then_succeeds():
    session = SequencedSession(
        [
            FakeResponse(503, {"error": "unavailable"}),
            FakeResponse(429, {"error": "rate limited"}),
            FakeResponse(200, {"ok": True}),
        ]
    )
    client = _new_client(session)

    assert await client.async_get("/pet/my") == {"ok": True}
    assert len(session.gets) == 3


@pytest.mark.asyncio
async def test_raises_api_error_after_exhausting_retries():
    session = SequencedSession([FakeResponse(503, {"error": "unavailable"})])
    client = _new_client(session)

    with pytest.raises(HaloApiError, match="HTTP 503"):
        await client.async_get("/pet/my")
    assert len(session.gets) == 3


@pytest.mark.asyncio
async def test_timeouts_are_wrapped_as_api_errors():
    session = SequencedSession([TimeoutError("timed out")])
    client = _new_client(session)

    with pytest.raises(HaloApiError):
        await client.async_get("/pet/my")
    assert len(session.gets) == 3


@pytest.mark.asyncio
async def test_persistent_401_after_refresh_raises_auth_error():
    session = SequencedSession([FakeResponse(401, {"error": "unauthorized"})])
    client = _new_client(session)

    with pytest.raises(HaloAuthError):
        await client.async_get("/pet/my")


@pytest.mark.asyncio
async def test_token_endpoint_outage_is_not_an_auth_error():
    session = FakeSession(token_status=503)
    client = _new_client(session)

    with pytest.raises(HaloApiError) as excinfo:
        await client.async_login("user@example.com", "hunter2", scope="openid")
    assert not isinstance(excinfo.value, HaloAuthError)


@pytest.mark.asyncio
async def test_set_fences_enabled_uses_recovered_instant_mode_contract():
    session = FakeSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    response = await client.async_set_fences_enabled("pet-1", enabled=True)

    url, payload, headers = session.puts[0]
    assert url == "https://api.example/pet/pet-1/instant-mode"
    assert payload == {"modePatch": {"fencesOn": True}}
    assert headers["Authorization"] == "Bearer access"
    assert response["desiredMode"]["fencesOn"] is True


class FailedWriteSession(FakeSession):
    async def put(self, url, json=None, headers=None):
        self.puts.append((url, json, headers))
        return FakeResponse(503, {"error": "unavailable"})


@pytest.mark.asyncio
async def test_fence_writes_are_not_retried_on_server_failure():
    session = FailedWriteSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloApiError, match="HTTP 503"):
        await client.async_set_fences_enabled("pet-1", enabled=False)

    assert len(session.puts) == 1
