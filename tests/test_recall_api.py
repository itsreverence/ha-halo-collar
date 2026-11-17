from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from custom_components.halo_collar import api as halo_api
from custom_components.halo_collar.api import (
    HALO_RECALL_MAX_COMMAND_NUMBER,
    HaloApiClient,
    HaloApiError,
    HaloRecallAccepted,
    HaloRecallCommand,
    HaloRecallExpired,
    HaloRecallStaleNumber,
    HaloWriteOutcomeUnknown,
)

COMMAND = HaloRecallCommand(
    mobile_id=7,
    command_number=12,
    expires_at=datetime(2026, 11, 17, 12, 0, 30, 999_999, tzinfo=UTC),
)
INSTALLATION_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


class FakeResponse:
    def __init__(self, status: int, body=None) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return ""

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class RecordingTransport:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict | None]] = []

    @asynccontextmanager
    async def request(self, method, url, *, headers, payload=None, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.requests.append((method, url, payload))
        yield self.response


class ConfigurationSession:
    def __init__(self, status: int, body) -> None:
        self.status = status
        self.body = body
        self.gets: list[str] = []

    async def get(self, url, headers=None, allow_redirects=True):
        self.gets.append(url)
        return FakeResponse(self.status, self.body)


def _client(*, transport=None, session=None) -> HaloApiClient:
    return HaloApiClient(
        session=session,
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        expires_at=time.time() + 3600,
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        api_base="https://api.example",
        auth_base="https://auth.example",
        write_transport=transport,
    )


async def _send(body, status=200):
    transport = RecordingTransport(FakeResponse(status, body))
    reply = await _client(transport=transport).async_send_recall_whistle(
        "pet-1", COMMAND, pre_dispatch=lambda: None
    )
    return reply, transport


async def test_recall_posts_exactly_the_whistle_body_once():
    reply, transport = await _send({"result": "Success"})

    assert reply == HaloRecallAccepted()
    assert transport.requests == [
        (
            "POST",
            "https://api.example/pet/pet-1/run-instant-correction",
            {
                "mobileId": 7,
                "commandNumber": 12,
                "expirationDate": "2026-11-17T12:00:30Z",
                "correctionType": "returnwhistle",
            },
        )
    ]


def test_whistle_type_and_routes_appear_once_in_the_integration():
    root = Path(__file__).resolve().parents[1] / "custom_components"
    source = "\n".join(path.read_text() for path in sorted(root.rglob("*.py")))
    assert source.count('"returnwhistle"') == 1
    assert source.count("run-instant-correction") == 1
    assert source.count("/account/mobile-data") == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"result": "success"}, HaloRecallAccepted()),
        ({"result": "SUCCESS", "currentCommandNumber": 3}, HaloRecallAccepted()),
        ({"result": "CommandExpired"}, HaloRecallExpired()),
        ({"result": "oldcommandnumber", "currentCommandNumber": 40}, HaloRecallStaleNumber(40)),
        (
            {"result": "OldCommandNumber", "currentCommandNumber": HALO_RECALL_MAX_COMMAND_NUMBER},
            HaloRecallStaleNumber(HALO_RECALL_MAX_COMMAND_NUMBER),
        ),
        ({"result": "OldCommandNumber"}, HaloRecallStaleNumber(None)),
        ({"result": "OldCommandNumber", "currentCommandNumber": -1}, HaloRecallStaleNumber(None)),
        ({"result": "OldCommandNumber", "currentCommandNumber": True}, HaloRecallStaleNumber(None)),
        ({"result": "OldCommandNumber", "currentCommandNumber": "4"}, HaloRecallStaleNumber(None)),
        (
            {"result": "OldCommandNumber", "currentCommandNumber": 2**31},
            HaloRecallStaleNumber(None),
        ),
    ],
)
async def test_recall_reply_maps_definitive_results(body, expected):
    reply, transport = await _send(body)
    assert reply == expected
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"result": "Unknown"}),
        (200, {"result": "played"}),
        (200, {"result": 1}),
        (200, {}),
        (200, []),
        (200, ValueError("invalid JSON")),
        (401, {"result": "Success"}),
        (404, {"result": "Success"}),
        (500, {"result": "Success"}),
    ],
)
async def test_recall_reply_without_a_definitive_answer_is_unknown(status, body):
    with pytest.raises(HaloWriteOutcomeUnknown) as raised:
        await _send(body, status)
    assert "pet" not in str(raised.value).lower()


async def test_recall_veto_before_dispatch_sends_nothing():
    transport = RecordingTransport(FakeResponse(200, {"result": "Success"}))
    veto = Mock(side_effect=RuntimeError("vetoed"))

    with pytest.raises(RuntimeError, match="vetoed"):
        await _client(transport=transport).async_send_recall_whistle(
            "pet-1", COMMAND, pre_dispatch=veto
        )

    veto.assert_called_once_with()
    assert transport.requests == []


async def test_registration_describes_home_assistant_and_returns_the_mobile_id():
    transport = RecordingTransport(FakeResponse(200, {"mobileId": 7}))

    mobile_id = await _client(transport=transport).async_register_recall_device(
        INSTALLATION_ID, version_string="2026.11.1", pre_dispatch=lambda: None
    )

    assert mobile_id == 7
    assert transport.requests == [
        (
            "POST",
            "https://api.example/account/mobile-data",
            {
                "internalMobileId": "00000000-0000-4000-8000-000000000001",
                "model": "Home Assistant",
                "manufacturer": "Home Assistant",
                "versionString": "2026.11.1",
                "platform": "unknown",
                "idiom": "unknown",
            },
        )
    ]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {}),
        (200, {"mobileId": "7"}),
        (200, {"mobileId": True}),
        (200, {"mobileId": 2**15}),
        (200, {"mobileId": 7.0}),
        (500, {"mobileId": 7}),
    ],
)
async def test_registration_without_a_usable_mobile_id_is_unknown(status, body):
    transport = RecordingTransport(FakeResponse(status, body))
    with pytest.raises(HaloWriteOutcomeUnknown):
        await _client(transport=transport).async_register_recall_device(
            INSTALLATION_ID, version_string="2026.11.1", pre_dispatch=lambda: None
        )
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:00:30", timedelta(seconds=30)),
        ("00:01:00", timedelta(minutes=1)),
        ("1.02:03:04", timedelta(days=1, hours=2, minutes=3, seconds=4)),
        ("00:00:00.5", timedelta(milliseconds=500)),
        ("00:00:01.0000001", timedelta(seconds=1)),
        (45, timedelta(seconds=45)),
        (2.5, timedelta(seconds=2.5)),
    ],
)
async def test_recall_window_parses_timespan_or_seconds(value, expected):
    session = ConfigurationSession(200, {"correction": {"runPositiveCorrectionExpiration": value}})
    assert await _client(session=session).async_get_recall_window() == expected
    assert session.gets == ["https://api.example/configuration"]


@pytest.mark.parametrize(
    "body",
    [
        {"correction": {"runPositiveCorrectionExpiration": "00:00:00"}},
        {"correction": {"runPositiveCorrectionExpiration": "-00:00:30"}},
        {"correction": {"runPositiveCorrectionExpiration": "00:60:00"}},
        {"correction": {"runPositiveCorrectionExpiration": "24:00:00"}},
        {"correction": {"runPositiveCorrectionExpiration": "30 seconds"}},
        {"correction": {"runPositiveCorrectionExpiration": 0}},
        {"correction": {"runPositiveCorrectionExpiration": -5}},
        {"correction": {"runPositiveCorrectionExpiration": True}},
        {"correction": {"runPositiveCorrectionExpiration": float("nan")}},
        {"correction": {"runPositiveCorrectionExpiration": float("inf")}},
        {"correction": {"runNegativeCorrectionExpiration": "00:00:30"}},
        {"correction": "00:00:30"},
        {},
        [],
    ],
)
async def test_recall_window_fails_closed_on_missing_or_malformed_data(body):
    with pytest.raises(HaloApiError, match="Recall command window"):
        await _client(session=ConfigurationSession(200, body)).async_get_recall_window()


async def test_recall_window_read_has_a_bounded_budget(monkeypatch):
    class StalledSession:
        async def get(self, url, headers=None, allow_redirects=True):
            await asyncio.Event().wait()

    monkeypatch.setattr(halo_api, "RECALL_WINDOW_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(HaloApiError, match="timed out"):
        await _client(session=StalledSession()).async_get_recall_window()


class StateSession:
    def __init__(self, server_time) -> None:
        self.server_time = server_time

    async def get(self, url, headers=None, allow_redirects=True):
        if url.endswith("/system/server-date-time"):
            if self.server_time is None:
                return FakeResponse(503, None)
            return FakeResponse(200, self.server_time)
        if url.endswith("/subscription/my"):
            return FakeResponse(200, {})
        if "/walk/my" in url:
            return FakeResponse(200, {"results": []})
        return FakeResponse(200, [])


@pytest.mark.parametrize(
    ("server_time", "expected"),
    [("2026-11-17T12:00:00Z", 1234.5), (None, None), ({"time": "now"}, None)],
)
async def test_server_clock_sample_is_taken_before_the_time_request(
    monkeypatch, server_time, expected
):
    monkeypatch.setattr(halo_api, "monotonic", lambda: 1234.5)
    monkeypatch.setattr(halo_api, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))

    state = await _client(session=StateSession(server_time)).async_fetch_state()

    assert state.server_time_monotonic == expected
    assert state.server_time == (server_time if isinstance(server_time, str) else None)


def test_mobile_id_parser_accepts_only_int16():
    assert halo_api.halo_mobile_id(-(2**15)) == -(2**15)
    assert halo_api.halo_mobile_id(2**15 - 1) == 2**15 - 1
    for value in (2**15, -(2**15) - 1, True, "1", 1.0, None):
        assert halo_api.halo_mobile_id(value) is None
