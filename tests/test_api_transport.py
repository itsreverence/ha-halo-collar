from __future__ import annotations

import asyncio
import json
import socket
import ssl
import struct
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from custom_components.halo_collar import api as halo_api
from custom_components.halo_collar import transport as halo_transport
from custom_components.halo_collar.api import (
    HaloApiClient,
    HaloApiError,
    HaloWriteOutcomeUnknown,
)
from custom_components.halo_collar.const import CONF_ENABLE_FIND_COLLAR
from custom_components.halo_collar.controls import (
    HaloControlError,
    async_find_collar,
    find_collar_cooldown_remaining,
)
from custom_components.halo_collar.transport import HaloWriteTransport

_TLS_CONTEXT = ssl.create_default_context()


def _new_client(session, base_url):
    return HaloApiClient(
        session=session,
        write_transport=HaloWriteTransport(
            ssl_context=_TLS_CONTEXT,
            default_headers=session.headers,
            trust_env=session.trust_env,
        ),
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        expires_at=time.time() + 3600,
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        api_base=base_url,
        auth_base=base_url,
    )


@pytest.fixture(autouse=True)
def _allow_local_sockets(request):
    if request.config.pluginmanager.hasplugin("socket"):
        request.getfixturevalue("socket_enabled")


def _success_response(operation):
    if operation == "find":
        return b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
    )


@asynccontextmanager
async def _write_server(operation, *, first_response, after_request=None):
    received = []
    handlers = []

    async def serve(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            async with asyncio.timeout(5):
                head = await reader.readuntil(b"\r\n\r\n")
                request_line, *fields = head.decode("ascii").split("\r\n")
                headers = {}
                for field in fields:
                    if ":" in field:
                        name, value = field.split(":", 1)
                        headers[name.lower()] = value
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                received.append((request_line, json.loads(body) if body else None))
                if after_request is not None:
                    await after_request(reader, writer, headers)
                    return
                response = first_response if len(received) == 1 else _success_response(operation)
                if response is not None:
                    writer.write(response)
                    await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with server:
            yield f"http://127.0.0.1:{port}", received
    finally:
        if handlers:
            await asyncio.gather(*handlers)


async def _dispatch(client, operation):
    if operation == "find":
        return await client.async_find_collar("synthetic-collar")
    return await client.async_set_fences_enabled("synthetic-pet", enabled=True)


def _expected_request(operation):
    if operation == "find":
        return ("PUT /collar/synthetic-collar/find HTTP/1.1", None)
    return ("PUT /pet/synthetic-pet/instant-mode HTTP/1.1", {"modePatch": {"fencesOn": True}})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["find", "fence"])
async def test_complete_write_disconnect_is_unknown_without_transport_replay(operation):
    async with (
        _write_server(operation, first_response=None) as (base_url, received),
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
    ):
        client = _new_client(session, base_url)
        outcome = None
        try:
            await _dispatch(client, operation)
        except HaloWriteOutcomeUnknown as error:
            outcome = error

    assert (received, type(outcome)) == ([_expected_request(operation)], HaloWriteOutcomeUnknown)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["find", "fence"])
async def test_write_success_over_real_transport(operation):
    async with (
        _write_server(operation, first_response=_success_response(operation)) as (
            base_url,
            received,
        ),
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
    ):
        result = await _dispatch(_new_client(session, base_url), operation)

    assert received == [_expected_request(operation)]
    assert result == (None if operation == "find" else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["find", "fence"])
async def test_write_redirect_is_unknown_without_following_over_real_transport(operation):
    redirect = (
        b"HTTP/1.1 307 Temporary Redirect\r\nLocation: /redirected\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    async with (
        _write_server(operation, first_response=redirect) as (base_url, received),
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
    ):
        with pytest.raises(HaloWriteOutcomeUnknown):
            await _dispatch(_new_client(session, base_url), operation)

    assert received == [_expected_request(operation)]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["find", "fence"])
async def test_complete_write_reset_is_unknown_without_transport_replay(operation):
    async def reset(reader, writer, headers):
        writer.get_extra_info("socket").setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        writer.transport.abort()

    async with (
        _write_server(operation, first_response=None, after_request=reset) as (base, received),
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(HaloWriteOutcomeUnknown):
            await _dispatch(_new_client(session, base), operation)

    assert received == [_expected_request(operation)]


@pytest.fixture
def write_resources(monkeypatch):
    resources = SimpleNamespace(sessions=[], connectors=[], resolvers=[])
    session_type = aiohttp.ClientSession
    connector_type = halo_transport._SingleLeaseConnector
    resolver_type = aiohttp.ThreadedResolver

    def session(**kwargs):
        result = session_type(**kwargs)
        result.put = Mock(wraps=result.put)
        resources.sessions.append(result)
        return result

    def connector(**kwargs):
        result = connector_type(**kwargs)
        resources.connectors.append(result)
        return result

    def resolver():
        result = resolver_type()
        result.close = AsyncMock(wraps=result.close)
        resources.resolvers.append(result)
        return result

    monkeypatch.setattr(halo_transport, "aiohttp", SimpleNamespace(**vars(aiohttp)))
    monkeypatch.setattr(halo_transport.aiohttp, "ClientSession", session)
    monkeypatch.setattr(halo_transport, "_SingleLeaseConnector", connector)
    monkeypatch.setattr(halo_transport.aiohttp, "ThreadedResolver", resolver)
    return resources


def _assert_resources_closed(resources):
    assert resources.resolvers
    assert all(session.closed for session in resources.sessions)
    assert all(connector.closed for connector in resources.connectors)
    for resolver in resources.resolvers:
        resolver.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connector", "session", "payload", "veto"])
async def test_preparation_failure_or_veto_closes_resources_without_dispatch(
    monkeypatch, write_resources, failure
):
    writer = HaloWriteTransport(ssl_context=_TLS_CONTEXT, default_headers={}, trust_env=False)
    committed = Mock()
    payload = {}
    if failure in ("connector", "session"):
        factory = Mock(side_effect=ValueError("synthetic preparation failure"))
        if failure == "connector":
            monkeypatch.setattr(halo_transport, "_SingleLeaseConnector", factory)
        else:
            monkeypatch.setattr(halo_transport.aiohttp, "ClientSession", factory)
    elif failure == "payload":
        payload = {"unsupported": {1, 2}}
    else:
        committed.side_effect = RuntimeError("dispatch veto")

    async with _write_server("fence", first_response=None) as (base, received):
        with pytest.raises((ValueError, TypeError, RuntimeError)):
            async with writer.put(base, headers={}, payload=payload, pre_dispatch=committed):
                pytest.fail("preparation failure reached the response")

    assert received == []
    assert committed.call_count == (1 if failure == "veto" else 0)
    _assert_resources_closed(write_resources)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "disconnect", "timeout", "cancel"])
async def test_write_resources_close_on_response_error_timeout_and_cancellation(
    monkeypatch, write_resources, outcome
):
    started = asyncio.Event()
    disconnected = asyncio.Event()

    async def stall_body(reader, writer, headers):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
        await writer.drain()
        started.set()
        try:
            assert await reader.read() == b""
        except ConnectionResetError:
            pass
        disconnected.set()

    if outcome == "timeout":
        deadline = asyncio.timeout(None)
        monkeypatch.setattr(
            halo_api,
            "asyncio",
            SimpleNamespace(**(vars(asyncio) | {"timeout": lambda _: deadline})),
        )
    response = _success_response("fence") if outcome == "success" else None
    stall = stall_body if outcome in ("timeout", "cancel") else None
    async with (
        _write_server("fence", first_response=response, after_request=stall) as (base, received),
        aiohttp.ClientSession() as session,
    ):
        client = _new_client(session, base)
        task = asyncio.create_task(_dispatch(client, "fence"))
        try:
            async with asyncio.timeout(5):
                if outcome == "cancel":
                    await started.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif outcome == "success":
                    assert await task == {}
                elif outcome == "timeout":
                    await started.wait()
                    deadline.reschedule(asyncio.get_running_loop().time() + 0.05)
                    with pytest.raises(HaloWriteOutcomeUnknown):
                        await task
                else:
                    with pytest.raises(HaloWriteOutcomeUnknown):
                        await task
                if stall is not None:
                    await disconnected.wait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert not session.closed
        assert received == [_expected_request("fence")]
    assert write_resources.sessions
    assert write_resources.connectors
    _assert_resources_closed(write_resources)


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_env", [False, True])
async def test_write_preserves_headers_and_shared_session_for_subsequent_read(
    write_resources, monkeypatch, trust_env
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    observed_headers = []

    async def respond(reader, writer, headers):
        observed_headers.append(headers)
        writer.write(_success_response("fence"))
        await writer.drain()

    async with (
        _write_server("find", first_response=None, after_request=respond) as (base, received),
        aiohttp.ClientSession(
            headers={"User-Agent": "synthetic-ha-agent"}, trust_env=trust_env
        ) as shared,
    ):
        connector = shared.connector
        original_headers = dict(shared.headers)
        committed = Mock()
        await _new_client(shared, base).async_find_collar(
            "synthetic-collar", pre_dispatch=committed
        )
        committed.assert_called_once_with()
        assert not shared.closed
        assert shared.connector is connector
        assert dict(shared.headers) == original_headers
        async with shared.get(base) as response:
            assert await response.json() == {}

    assert len(received) == 2
    assert len(write_resources.sessions) == 1
    assert len(write_resources.connectors) == 1
    assert write_resources.connectors[0]._ssl is _TLS_CONTEXT
    assert write_resources.connectors[0]._resolver is write_resources.resolvers[0]
    assert write_resources.sessions[0].trust_env is trust_env
    write_resources.sessions[0].put.assert_called_once()
    assert write_resources.sessions[0].put.call_args.kwargs["allow_redirects"] is False
    assert observed_headers[0]["user-agent"].strip() == "synthetic-ha-agent"
    assert observed_headers[0]["authorization"].strip() == "Bearer synthetic-access"
    assert "Halo-Client".lower() in observed_headers[0]
    assert "authorization" not in observed_headers[1]
    _assert_resources_closed(write_resources)


@pytest.mark.asyncio
async def test_real_find_transaction_keeps_lock_and_cooldown_through_cancelled_reconciliation():
    lock = asyncio.Lock()
    cooldowns = {}
    reconciling = asyncio.Event()
    finish_refresh = asyncio.Event()
    pet = {
        "id": "synthetic-pet",
        "collarInfo": {"id": "synthetic-collar", "telemetry": {"walk": None}},
    }
    collar = {
        "id": "synthetic-collar",
        "petInfo": {"id": "synthetic-pet"},
        "telemetry": {"walk": None, "manifest": {"timestamp": datetime.now(UTC).isoformat()}},
    }
    subscription = {"features": [{"featureType": {"id": "findcollar"}, "isEnabled": True}]}
    refresh_count = 0

    async def refresh():
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 2:
            reconciling.set()
            await finish_refresh.wait()

    async with (
        _write_server("find", first_response=None) as (base, received),
        aiohttp.ClientSession() as session,
    ):
        client = _new_client(session, base)

        async def execute():
            await async_find_collar(
                coordinator=SimpleNamespace(async_refresh=refresh, last_update_success=True),
                client=client,
                entry=SimpleNamespace(options={CONF_ENABLE_FIND_COLLAR: True}),
                control_lock=lock,
                cooldowns=cooldowns,
                state_getter=lambda: (pet, collar, subscription),
            )

        first = asyncio.create_task(execute())
        second = None
        try:
            async with asyncio.timeout(5):
                await reconciling.wait()
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                second = asyncio.create_task(execute())
                await asyncio.sleep(0)
                assert not first.done()
                assert not second.done()
                assert lock.locked()
                assert received == [_expected_request("find")]
                assert find_collar_cooldown_remaining(cooldowns, "synthetic-collar") > 0
                finish_refresh.set()
                with pytest.raises(asyncio.CancelledError):
                    await first
                with pytest.raises(HaloControlError, match="cooldown"):
                    await second
        finally:
            finish_refresh.set()
            first.cancel()
            if second is not None:
                second.cancel()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )

        assert not lock.locked()
        assert refresh_count == 2
        assert received == [_expected_request("find")]


@asynccontextmanager
async def _malformed_status_line_server():
    handlers = []

    async def serve(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            async with asyncio.timeout(5):
                head = await reader.readuntil(b"\r\n\r\n")
                for field in head.decode("ascii").split("\r\n"):
                    name, _, value = field.partition(":")
                    if name.lower() == "content-length":
                        await reader.readexactly(int(value))
                writer.write(b"HTTP/1.1 abc garbage\r\n\r\n")
                await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with server:
            yield f"http://127.0.0.1:{port}"
    finally:
        if handlers:
            await asyncio.gather(*handlers)


@pytest.mark.parametrize(
    ("request_kind", "header_in_repr", "secret"),
    [
        ("get", "Bearer synthetic-access", "synthetic-access"),
        ("token", "Halo-Client", "synthetic-client"),
    ],
)
async def test_transport_error_message_omits_request_headers(
    monkeypatch, request_kind, header_in_repr, secret
):
    monkeypatch.setattr(halo_api, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    async with (
        _malformed_status_line_server() as base_url,
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
    ):
        client = _new_client(session, base_url)
        with pytest.raises(HaloApiError) as raised:
            if request_kind == "get":
                await client.async_get("/pet/my")
            else:
                await client.async_refresh_token()

    cause = raised.value.__cause__
    assert isinstance(cause, aiohttp.ClientResponseError)
    assert header_in_repr in repr(cause)
    message = str(raised.value)
    assert "Authorization" not in message
    assert "Halo-Client" not in message
    assert secret in repr(cause)
    assert secret not in message
    assert "ClientResponseError: 400" in message
