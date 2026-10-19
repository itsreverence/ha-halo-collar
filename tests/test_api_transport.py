from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest

from custom_components.halo_collar.api import HaloApiClient, HaloWriteOutcomeUnknown


def _new_client(session, base_url):
    return HaloApiClient(
        session=session,
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
async def _write_server(operation, *, first_response):
    received = []
    handlers = []

    async def serve(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            async with asyncio.timeout(5):
                head = await reader.readuntil(b"\r\n\r\n")
                request_line, *fields = head.decode("ascii").split("\r\n")
                headers = dict(field.lower().split(":", 1) for field in fields if ":" in field)
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                received.append((request_line, json.loads(body) if body else None))
                response = first_response if len(received) == 1 else _success_response(operation)
                if response is not None:
                    writer.write(response)
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

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
