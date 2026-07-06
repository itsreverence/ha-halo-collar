from __future__ import annotations

import time

import pytest

from custom_components.halo_collar.api import HaloApiClient


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data, headers))
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
