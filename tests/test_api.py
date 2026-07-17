from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest

from custom_components.halo_collar import api as halo_api
from custom_components.halo_collar.api import (
    HaloApiClient,
    HaloApiError,
    HaloAuthError,
    HaloCollarNotFound,
    HaloWriteOutcomeUnknown,
)


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload
        self.released = False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return str(self._payload)

    def release(self):
        self.released = True


class FakeSession:
    def __init__(self, token_status=200):
        self.posts = []
        self.gets = []
        self.puts = []
        self.put_redirects = []
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
        if url.endswith("/walk/my?page=1&pageSize=10"):
            return FakeResponse(200, {"results": []})
        return FakeResponse(404, {})

    async def put(self, url, json=None, headers=None, allow_redirects=True):
        assert json is not None
        self.put_redirects.append(allow_redirects)
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
    assert state.walks == []
    assert session.posts[0][1]["grant_type"] == "refresh_token"
    assert session.gets[0][1]["Authorization"] == "Bearer new-access"
    assert [url for url, _headers in session.gets].count(
        "https://api.example/walk/my?page=1&pageSize=10"
    ) == 1
    assert client.token_snapshot["refresh_token"] == "new-refresh"
    assert client.token_snapshot["expires_at"] > time.time()


class ConcurrentRefreshSession(FakeSession):
    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data, headers))
        await asyncio.sleep(0)
        return FakeResponse(
            200,
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            },
        )


@pytest.mark.asyncio
async def test_concurrent_expired_reads_share_one_rotating_token_refresh():
    session = ConcurrentRefreshSession()
    client = _new_client(session)
    client._refresh_token = "original-refresh"

    pets, collars = await asyncio.gather(
        client.async_get("/pet/my"),
        client.async_get("/collar/my"),
    )

    assert pets == [{"id": "pet1"}]
    assert collars[0]["id"] == "collar1"
    assert len(session.posts) == 1
    assert client.token_snapshot["refresh_token"] == "rotated-refresh"
    assert all(headers["Authorization"] == "Bearer new-access" for _, headers in session.gets)


class ConcurrentUnauthorizedSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.old_token_gets = 0
        self.old_token_barrier = asyncio.Event()

    async def get(self, url, headers=None):
        assert headers is not None
        self.gets.append((url, headers))
        if headers["Authorization"] == "Bearer old-access":
            self.old_token_gets += 1
            if self.old_token_gets == 2:
                self.old_token_barrier.set()
            await self.old_token_barrier.wait()
            return FakeResponse(401, {"error": "unauthorized"})
        return await super().get(url, headers=headers)


@pytest.mark.asyncio
async def test_concurrent_401_recovery_refreshes_rejected_token_once():
    session = ConcurrentUnauthorizedSession()
    client = _new_client(session)
    client._access_token = "old-access"
    client._refresh_token = "original-refresh"
    client._expires_at = time.time() + 3600

    pets, collars = await asyncio.gather(
        client.async_get("/pet/my"),
        client.async_get("/collar/my"),
    )

    assert pets == [{"id": "pet1"}]
    assert collars[0]["id"] == "collar1"
    assert len(session.posts) == 1
    assert client.token_snapshot["refresh_token"] == "new-refresh"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "message"),
    [
        (
            [
                FakeResponse(200, {"id": "not-a-list"}),
                FakeResponse(200, []),
                FakeResponse(200, {}),
                FakeResponse(200, "2026-07-06T00:32:03Z"),
            ],
            "pet state response was not a list",
        ),
        (
            [
                FakeResponse(200, []),
                FakeResponse(200, {"id": "not-a-list"}),
                FakeResponse(200, {}),
                FakeResponse(200, "2026-07-06T00:32:03Z"),
            ],
            "collar state response was not a list",
        ),
        (
            [
                FakeResponse(200, []),
                FakeResponse(200, []),
                FakeResponse(200, ["not-an-object"]),
                FakeResponse(200, "2026-07-06T00:32:03Z"),
            ],
            "subscription state response was not an object",
        ),
    ],
)
async def test_fetch_state_rejects_malformed_provider_shapes(responses, message):
    session = SequencedSession(responses)
    client = _new_client(session)

    with pytest.raises(HaloApiError, match=message):
        await client.async_fetch_state()


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


class StalledGetBodyResponse(FakeResponse):
    async def json(self, content_type=None):
        await asyncio.sleep(60)


@pytest.mark.asyncio
async def test_get_timeout_covers_response_body_and_releases_each_attempt(monkeypatch):
    monkeypatch.setattr(halo_api, "REQUEST_TIMEOUT_SECONDS", 0.01)
    response = StalledGetBodyResponse(200, {})
    session = SequencedSession([response])
    client = _new_client(session)

    with pytest.raises(HaloApiError):
        await client.async_get("/pet/my")

    assert len(session.gets) == 3
    assert response.released is True


@pytest.mark.asyncio
async def test_get_releases_transient_responses_before_retrying():
    first = FakeResponse(503, {"error": "unavailable"})
    second = FakeResponse(429, {"error": "rate limited"})
    third = FakeResponse(200, {"ok": True})
    session = SequencedSession([first, second, third])
    client = _new_client(session)

    assert await client.async_get("/pet/my") == {"ok": True}
    assert first.released is True
    assert second.released is True
    assert third.released is True


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


class StalledTokenBodyResponse(FakeResponse):
    async def text(self):
        await asyncio.sleep(60)
        return ""


class StalledTokenBodySession(FakeSession):
    def __init__(self):
        super().__init__()
        self.response = StalledTokenBodyResponse(200, {})

    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data, headers))
        return self.response


@pytest.mark.asyncio
async def test_token_timeout_covers_response_body_and_releases_response(monkeypatch):
    monkeypatch.setattr(halo_api, "REQUEST_TIMEOUT_SECONDS", 0.01)
    session = StalledTokenBodySession()
    client = _new_client(session)

    with pytest.raises(HaloApiError):
        await client.async_login("user@example.com", "hunter2", scope="openid")

    assert session.response.released is True


class InvalidJsonTokenResponse(FakeResponse):
    async def json(self, content_type=None):
        raise ValueError("invalid json")


class InvalidJsonTokenSession(FakeSession):
    def __init__(self, status=200):
        super().__init__()
        self.response = InvalidJsonTokenResponse(status, "not-json")

    async def post(self, url, data=None, headers=None):
        self.posts.append((url, data, headers))
        return self.response


@pytest.mark.asyncio
async def test_invalid_token_success_body_is_api_error_and_releases_response():
    session = InvalidJsonTokenSession()
    client = _new_client(session)

    with pytest.raises(HaloApiError, match="invalid JSON") as excinfo:
        await client.async_login("user@example.com", "hunter2", scope="openid")

    assert not isinstance(excinfo.value, HaloAuthError)
    assert session.response.released is True


@pytest.mark.asyncio
async def test_invalid_token_rejection_body_is_auth_error_and_releases_response():
    session = InvalidJsonTokenSession(status=400)
    client = _new_client(session)

    with pytest.raises(HaloAuthError, match="rejected with invalid JSON"):
        await client.async_login("user@example.com", "wrong", scope="openid")

    assert session.response.released is True


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
    assert session.put_redirects == [False]
    assert response["desiredMode"]["fencesOn"] is True


class FindCollarSession(FakeSession):
    def __init__(self, status=204, error=None):
        super().__init__()
        self.status = status
        self.error = error
        self.response = FakeResponse(status, None)

    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.put_redirects.append(allow_redirects)
        self.puts.append((url, json, headers))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_find_collar_uses_bodyless_single_attempt_contract():
    session = FindCollarSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    await client.async_find_collar("collar/id")

    assert len(session.puts) == 1
    url, payload, headers = session.puts[0]
    assert url == "https://api.example/collar/collar%2Fid/find"
    assert payload is None
    assert headers["Authorization"] == "Bearer access"
    assert session.put_redirects == [False]
    assert session.response.released is True


@pytest.mark.asyncio
async def test_find_collar_pre_dispatch_runs_after_refresh_and_can_veto():
    session = FindCollarSession()
    client = _new_client(session)
    client._refresh_token = "refresh"

    def veto():
        assert client.token_snapshot["access_token"] == "new-access"
        raise RuntimeError("find option revoked")

    with pytest.raises(RuntimeError, match="revoked"):
        await client.async_find_collar("collar-1", pre_dispatch=veto)

    assert len(session.posts) == 1
    assert session.puts == []


@pytest.mark.asyncio
async def test_find_collar_404_is_known_not_found_without_retry():
    session = FindCollarSession(status=404)
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloCollarNotFound, match="not found"):
        await client.async_find_collar("collar-1")

    assert len(session.puts) == 1
    assert session.response.released is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 307, 429, 500, 503])
async def test_find_collar_non_success_is_unknown_and_never_retried(status):
    session = FindCollarSession(status=status)
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match=f"HTTP {status}"):
        await client.async_find_collar("collar-1")

    assert len(session.puts) == 1
    assert session.put_redirects == [False]
    assert session.response.released is True


@pytest.mark.asyncio
async def test_find_collar_transport_failure_is_unknown_and_never_retried():
    session = FindCollarSession(error=aiohttp.ClientConnectionError("disconnected"))
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="outcome is unknown"):
        await client.async_find_collar("collar-1")

    assert len(session.puts) == 1
    assert session.put_redirects == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("collar_id", ["", 123, [], {}])
async def test_find_collar_rejects_invalid_target_before_transport(collar_id):
    session = FindCollarSession()
    client = _new_client(session)

    with pytest.raises(HaloApiError, match="non-empty string"):
        await client.async_find_collar(collar_id)

    assert session.posts == []
    assert session.puts == []


@pytest.mark.asyncio
async def test_pre_dispatch_check_runs_after_token_refresh_and_can_veto_put():
    session = FakeSession()
    client = _new_client(session)
    client._refresh_token = "refresh"

    def veto_after_refresh():
        assert session.posts[0][1]["grant_type"] == "refresh_token"
        assert client.token_snapshot["access_token"] == "new-access"
        raise RuntimeError("options revoked")

    with pytest.raises(RuntimeError, match="options revoked"):
        await client.async_set_fences_enabled(
            "pet-1",
            enabled=False,
            pre_dispatch=veto_after_refresh,
        )

    assert session.puts == []


class RedirectWriteSession(FakeSession):
    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.put_redirects.append(allow_redirects)
        self.puts.append((url, json, headers))
        return FakeResponse(307, {"location": "https://other.example"})


@pytest.mark.asyncio
async def test_fence_writes_do_not_follow_redirects_or_replay():
    session = RedirectWriteSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="HTTP 307"):
        await client.async_set_fences_enabled("pet-1", enabled=False)

    assert len(session.puts) == 1
    assert session.put_redirects == [False]


class BrokenBodyResponse(FakeResponse):
    async def text(self):
        raise aiohttp.ClientPayloadError("truncated body")


class BrokenBodyWriteSession(FakeSession):
    def __init__(self, status):
        super().__init__()
        self.response = BrokenBodyResponse(status, {})

    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.put_redirects.append(allow_redirects)
        self.puts.append((url, json, headers))
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 503])
async def test_write_status_body_failures_have_unknown_outcome_without_replay(status):
    session = BrokenBodyWriteSession(status)
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="response processing failed"):
        await client.async_set_fences_enabled("pet-1", enabled=False)

    assert len(session.puts) == 1
    assert session.put_redirects == [False]
    assert session.response.released is True


class StalledBodyResponse(FakeResponse):
    async def json(self, content_type=None):
        await asyncio.sleep(60)


class StalledBodyWriteSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.response = StalledBodyResponse(200, {})

    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.put_redirects.append(allow_redirects)
        self.puts.append((url, json, headers))
        return self.response


@pytest.mark.asyncio
async def test_write_timeout_covers_response_body_and_releases_response(monkeypatch):
    monkeypatch.setattr(halo_api, "REQUEST_TIMEOUT_SECONDS", 0.01)
    session = StalledBodyWriteSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="response processing failed"):
        await client.async_set_fences_enabled("pet-1", enabled=True)

    assert len(session.puts) == 1
    assert session.response.released is True


class InvalidWriteResponseSession(FakeSession):
    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.put_redirects.append(allow_redirects)
        self.puts.append((url, json, headers))
        return FakeResponse(200, ["unexpected"])


@pytest.mark.asyncio
async def test_invalid_write_success_body_has_unknown_outcome_without_replay():
    session = InvalidWriteResponseSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="invalid success response"):
        await client.async_set_fences_enabled("pet-1", enabled=True)

    assert len(session.puts) == 1
    assert session.put_redirects == [False]


class UnauthorizedWriteSession(FakeSession):
    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.puts.append((url, json, headers))
        return FakeResponse(401, {"error": "unauthorized"})


@pytest.mark.asyncio
async def test_fence_writes_are_not_retried_or_refreshed_after_401():
    session = UnauthorizedWriteSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="not retried"):
        await client.async_set_fences_enabled("pet-1", enabled=False)

    assert len(session.puts) == 1
    assert session.posts == []


class FailedWriteSession(FakeSession):
    async def put(self, url, json=None, headers=None, allow_redirects=True):
        self.puts.append((url, json, headers))
        return FakeResponse(503, {"error": "unavailable"})


@pytest.mark.asyncio
async def test_fence_writes_are_not_retried_on_server_failure():
    session = FailedWriteSession()
    client = _new_client(session)
    client._access_token = "access"
    client._expires_at = time.time() + 3600

    with pytest.raises(HaloWriteOutcomeUnknown, match="HTTP 503"):
        await client.async_set_fences_enabled("pet-1", enabled=False)

    assert len(session.puts) == 1


@pytest.mark.asyncio
async def test_fetch_state_stores_only_walk_results_after_core_state_validation():
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "endedAt": "2026-07-06T10:30:00Z",
                            "id": "PRIVATE_WALK_ID",
                            "name": "PRIVATE_NAME_SENTINEL",
                            "locationName": "PRIVATE_LOCATION_SENTINEL",
                            "routeImageUrl": "https://private.invalid/route.png",
                            "user": {"id": "PRIVATE_USER_ID"},
                            "feedback": {"value": "PRIVATE_FEEDBACK_SENTINEL"},
                            "correction": {"value": "PRIVATE_CORRECTION_SENTINEL"},
                            "pets": [
                                {
                                    "id": "pet-1",
                                    "walkedDurationInSeconds": 300,
                                    "walkedDistanceInMeters": "125.5",
                                    "nestedUnknown": {"route": "PRIVATE_NESTED_SENTINEL"},
                                },
                                {
                                    "id": "pet-2",
                                    "walkedDurationInSeconds": True,
                                    "walkedDistanceInMeters": ["not", "a", "scalar"],
                                },
                            ],
                        }
                    ],
                    "page": 1,
                },
            ),
        ]
    )
    client = _new_client(session)

    state = await client.async_fetch_state()

    assert state.walks == [
        {
            "endedAt": "2026-07-06T10:30:00Z",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 300,
                    "walkedDistanceInMeters": "125.5",
                },
                {"id": "pet-2"},
            ],
        }
    ]
    assert [url for url, _headers in session.gets][-1] == (
        "https://api.example/walk/my?page=1&pageSize=10"
    )
    serialized_walks = str(state.walks).lower()
    for sentinel in (
        "private_walk_id",
        "private_name_sentinel",
        "private_location_sentinel",
        "private.invalid/route.png",
        "private_user_id",
        "private_feedback_sentinel",
        "private_correction_sentinel",
        "private_nested_sentinel",
    ):
        assert sentinel not in serialized_walks

    def assert_allowlisted_walk(value):
        if isinstance(value, list):
            for item in value:
                assert_allowlisted_walk(item)
        elif isinstance(value, dict):
            assert set(value) <= {
                "endedAt",
                "startedAt",
                "startTrigger",
                "pets",
                "id",
                "walkedDurationInSeconds",
                "walkedDistanceInMeters",
            }
            for item in value.values():
                assert_allowlisted_walk(item)
        elif isinstance(value, str):
            assert "private_" not in value.lower()
            assert "private.invalid" not in value.lower()

    assert_allowlisted_walk(state.walks)


@pytest.mark.asyncio
async def test_fetch_state_keeps_only_first_ten_raw_walk_results():
    first_ten_results = [
        {"id": "walk-0", "endedAt": "2026-07-06T10:00:00Z"},
        {"id": "walk-1", "endedAt": "2026-07-06T10:01:00Z"},
        {"id": "walk-2", "endedAt": "2026-07-06T10:02:00Z"},
        {"id": "walk-3", "endedAt": "2026-07-06T10:03:00Z"},
        "not-an-object",
        {"id": "walk-5", "endedAt": "2026-07-06T10:05:00Z"},
        {"id": "walk-6", "endedAt": "2026-07-06T10:06:00Z"},
        {"id": "walk-7", "endedAt": "2026-07-06T10:07:00Z"},
        {"id": "walk-8", "endedAt": "2026-07-06T10:08:00Z"},
        {"id": "walk-9", "endedAt": "2026-07-06T10:09:00Z"},
    ]
    beyond_boundary = {
        "id": "PRIVATE_NEWER_WALK_SENTINEL",
        "endedAt": "2026-07-06T12:00:00Z",
        "pets": [{"id": "pet-1", "walkedDurationInSeconds": 999}],
    }
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            FakeResponse(200, {"results": [*first_ten_results, beyond_boundary]}),
        ]
    )
    client = _new_client(session)

    state = await client.async_fetch_state()

    assert [walk.get("endedAt") for walk in state.walks] == [
        walk["endedAt"] for walk in first_ten_results if isinstance(walk, dict)
    ]
    assert len(state.walks) == 9
    assert beyond_boundary not in state.walks
    assert "2026-07-06T12:00:00Z" not in [walk.get("endedAt") for walk in state.walks]
    assert "PRIVATE_NEWER_WALK_SENTINEL" not in str(state.walks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("walk_payload", "expected_walks"),
    [
        ("not-an-object", []),
        ({"results": "not-a-list"}, []),
        (
            {
                "results": [
                    {"endedAt": "2026-07-06T10:30:00Z"},
                    "not-an-object",
                    42,
                    {"endedAt": "2026-07-06T11:30:00Z"},
                ]
            },
            [
                {"endedAt": "2026-07-06T10:30:00Z"},
                {"endedAt": "2026-07-06T11:30:00Z"},
            ],
        ),
    ],
)
async def test_malformed_walk_history_payload_preserves_core_state(walk_payload, expected_walks):
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {"accessLevel": "basic"}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            FakeResponse(200, walk_payload),
        ]
    )
    client = _new_client(session)

    state = await client.async_fetch_state()

    assert state.pets == [{"id": "pet-1"}]
    assert state.collars == [{"id": "collar-1"}]
    assert state.subscription == {"accessLevel": "basic"}
    assert state.server_time == "2026-07-06T00:32:03Z"
    assert state.walks == expected_walks


@pytest.mark.asyncio
async def test_walk_history_transient_failure_does_not_hide_core_state(caplog):
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            FakeResponse(503, {"unavailable": True}),
        ]
    )
    client = _new_client(session)

    state = await client.async_fetch_state()

    assert state.pets == [{"id": "pet-1"}]
    assert state.walks == []
    assert len(session.gets) == 7
    assert "walk history was unavailable" in caplog.text


@pytest.mark.asyncio
async def test_walk_history_total_budget_preserves_core_state_and_releases_response(
    monkeypatch, caplog
):
    monkeypatch.setattr(halo_api, "OPTIONAL_WALK_HISTORY_TIMEOUT_SECONDS", 0.01)
    stalled_response = StalledGetBodyResponse(200, {"private": "PRIVATE_WALK_SENTINEL"})
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {"accessLevel": "basic"}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            stalled_response,
        ]
    )
    client = _new_client(session)

    state = await client.async_fetch_state()

    assert state.pets == [{"id": "pet-1"}]
    assert state.walks == []
    assert stalled_response.released is True
    assert "walk history was unavailable" in caplog.text
    assert "PRIVATE_WALK_SENTINEL" not in caplog.text


@pytest.mark.asyncio
async def test_walk_history_auth_rejection_preserves_auth_failure_semantics():
    session = SequencedSession(
        [
            FakeResponse(200, [{"id": "pet-1"}]),
            FakeResponse(200, [{"id": "collar-1"}]),
            FakeResponse(200, {}),
            FakeResponse(200, "2026-07-06T00:32:03Z"),
            FakeResponse(401, {"unauthorized": True}),
        ]
    )
    client = _new_client(session)

    with pytest.raises(HaloAuthError):
        await client.async_fetch_state()
