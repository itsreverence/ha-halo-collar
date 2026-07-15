from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

if TYPE_CHECKING:
    from aiohttp import ClientSession

TOKEN_REFRESH_SKEW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
# Transient statuses worth retrying; anything else surfaces immediately.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# Delays between attempts; total attempts = len(RETRY_BACKOFF_SECONDS) + 1.
RETRY_BACKOFF_SECONDS = (1.0, 3.0)

_LOGGER = logging.getLogger(__name__)


class HaloApiError(Exception):
    """Raised when Halo API calls fail."""


class HaloAuthError(HaloApiError):
    """Raised when Halo authentication (login or token refresh) fails."""


@dataclass(slots=True)
class HaloState:
    pets: list[dict[str, Any]]
    collars: list[dict[str, Any]]
    subscription: dict[str, Any]
    server_time: str | None = None


class HaloApiClient:
    def __init__(
        self,
        *,
        session: ClientSession,
        access_token: str,
        refresh_token: str,
        expires_at: float,
        client_id: str,
        client_secret: str,
        api_base: str,
        auth_base: str,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_base = api_base.rstrip("/")
        self._auth_base = auth_base.rstrip("/")

    @property
    def token_snapshot(self) -> dict[str, Any]:
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "client_id": self._client_id,
        }

    async def async_fetch_state(self) -> HaloState:
        pets, collars, subscription, server_time = await self.async_get_many(
            "/pet/my", "/collar/my", "/subscription/my", "/system/server-date-time"
        )
        return HaloState(
            pets=list(pets or []),
            collars=list(collars or []),
            subscription=dict(subscription or {}),
            server_time=server_time if isinstance(server_time, str) else None,
        )

    async def async_get_many(self, *paths: str) -> list[Any]:
        return [await self.async_get(path) for path in paths]

    async def async_get(self, path: str) -> Any:
        await self._async_refresh_if_needed()
        response = await self._async_get_with_retry(path)
        if response.status == 401:
            # Access token rejected despite looking valid; refresh once and retry.
            await self.async_refresh_token()
            response = await self._async_get_with_retry(path)
            if response.status == 401:
                raise HaloAuthError(f"GET {path} unauthorized even after a token refresh")
        if response.status >= 400:
            text = await response.text()
            raise HaloApiError(f"GET {path} failed: HTTP {response.status}: {text[:200]}")
        return await response.json(content_type=None)

    async def async_set_fences_enabled(self, pet_id: str, *, enabled: bool) -> dict[str, Any]:
        """Set the pet's fence mode through Halo's instant-mode endpoint.

        Writes are deliberately not retried on 429/5xx or connection errors:
        callers must refresh state before deciding whether another write is safe.
        """
        if not pet_id:
            raise HaloApiError("Pet ID is required to change fence mode")
        payload = {"modePatch": {"fencesOn": enabled}}
        path = f"/pet/{quote(pet_id, safe='')}/instant-mode"
        return await self._async_put_json(path, payload)

    async def _async_put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self._async_refresh_if_needed()
        response = await self._async_put_once(path, payload)
        if response.status == 401:
            await response.text()
            raise HaloApiError(
                f"PUT {path} returned HTTP 401; write was not retried because outcome is unknown"
            )
        if not 200 <= response.status < 300:
            text = await response.text()
            raise HaloApiError(f"PUT {path} failed: HTTP {response.status}: {text[:200]}")
        result = await response.json(content_type=None)
        if not isinstance(result, dict):
            raise HaloApiError(f"PUT {path} returned an invalid response")
        return result

    async def _async_put_once(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                return await self._session.put(
                    f"{self._api_base}{path}",
                    json=payload,
                    headers=self._headers(),
                    allow_redirects=False,
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HaloApiError(f"PUT {path} failed: {err!r}") from err

    async def _async_get_with_retry(self, path: str) -> Any:
        """GET with a short backoff for timeouts, connection errors, 429s, and 5xx."""
        attempts = len(RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    response = await self._session.get(
                        f"{self._api_base}{path}",
                        headers=self._headers(),
                    )
            except (TimeoutError, aiohttp.ClientError) as err:
                if attempt == attempts - 1:
                    raise HaloApiError(f"GET {path} failed: {err!r}") from err
                continue
            if response.status in RETRYABLE_STATUS and attempt < attempts - 1:
                continue
            return response
        raise AssertionError("unreachable")

    async def _async_refresh_if_needed(self) -> None:
        if not self._access_token or time.time() >= self._expires_at - TOKEN_REFRESH_SKEW_SECONDS:
            await self.async_refresh_token()

    async def async_refresh_token(self) -> None:
        if not self._client_secret:
            raise HaloApiError("Halo OAuth client secret is required to refresh tokens")
        await self._async_token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            }
        )

    async def async_login(self, email: str, password: str, *, scope: str) -> None:
        """Exchange user credentials for access/refresh tokens (OAuth password grant)."""
        if not self._client_secret:
            raise HaloApiError("Halo OAuth client secret is required to sign in")
        await self._async_token_request(
            {
                "grant_type": "password",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "username": email,
                "password": password,
                "scope": scope,
            }
        )

    async def _async_token_request(self, data: dict[str, str]) -> None:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await self._session.post(
                    f"{self._auth_base}/connect/token",
                    data=data,
                    headers=self._token_headers(),
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HaloApiError(f"Token request failed: {err!r}") from err
        text = await response.text()
        if response.status >= 500:
            # Identity-server outage, not bad credentials; must not trigger reauth.
            raise HaloApiError(f"Token endpoint error: HTTP {response.status}: {text[:200]}")
        payload = await response.json(content_type=None)
        if response.status >= 400:
            _LOGGER.warning(
                "Halo token request rejected: HTTP %s, oauth_error=%s, description=%s",
                response.status,
                payload.get("error") if isinstance(payload, dict) else None,
                payload.get("error_description") if isinstance(payload, dict) else text[:200],
            )
            raise HaloAuthError(f"Token request failed: HTTP {response.status}: {payload}")
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise HaloAuthError(f"Token request returned no access_token: {payload}")
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = (
            time.time() + int(payload.get("expires_in", 0)) - TOKEN_REFRESH_SKEW_SECONDS
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            **self._client_headers(),
        }

    def _token_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            **self._client_headers(),
        }

    def _client_headers(self) -> dict[str, str]:
        return {
            "Halo-Client": (
                f"clientId={self._client_id}&version=2.11.0"
                "&appInstanceId=00000000-0000-0000-0000-000000000000"
                "&timezone=America%2FNew_York"
            ),
            "Halo-Amplitude-SessionId": "0",
        }
