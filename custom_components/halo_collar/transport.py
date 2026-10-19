from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import aiohttp


class _ReplayBlocked(aiohttp.ClientError):
    pass


class _SingleLeaseConnector(aiohttp.TCPConnector):
    def __init__(self, *, ssl_context: ssl.SSLContext, resolver: aiohttp.ThreadedResolver):
        super().__init__(ssl=ssl_context, resolver=resolver, force_close=True, limit=1)
        self._leased = False

    async def connect(self, *args: Any, **kwargs: Any):
        if self._leased:
            raise _ReplayBlocked("Write connection lease already used")
        # aiohttp's disconnected-PUT retry acquires another connection here.
        self._leased = True
        return await super().connect(*args, **kwargs)


class HaloWriteTransport:
    """Own a fresh session and one connection lease for each approved PUT."""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext,
        default_headers: Mapping[str, str],
        trust_env: bool,
    ) -> None:
        self._ssl_context = ssl_context
        self._default_headers = dict(default_headers)
        self._trust_env = trust_env

    @asynccontextmanager
    async def put(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: dict[str, Any] | None = None,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        resolver = aiohttp.ThreadedResolver()
        try:
            async with _SingleLeaseConnector(
                ssl_context=self._ssl_context, resolver=resolver
            ) as connector:
                async with aiohttp.ClientSession(
                    connector=connector,
                    connector_owner=False,
                    headers=self._default_headers,
                    trust_env=self._trust_env,
                    cookie_jar=aiohttp.DummyCookieJar(),
                ) as session:
                    request_options = {
                        "headers": dict(headers),
                        "data": aiohttp.JsonPayload(payload) if payload is not None else None,
                        "allow_redirects": False,
                    }
                    if pre_dispatch is not None:
                        pre_dispatch()
                    async with session.put(url, **request_options) as response:
                        yield response
        finally:
            # Explicitly supplied resolvers belong to us, not the connector.
            await resolver.close()
