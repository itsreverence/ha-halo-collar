from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import ClientSession

TOKEN_REFRESH_SKEW_SECONDS = 60


class HaloApiError(Exception):
    """Raised when Halo API calls fail."""


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
        response = await self._session.get(
            f"{self._api_base}{path}",
            headers=self._headers(),
        )
        if response.status == 401:
            await self.async_refresh_token()
            response = await self._session.get(
                f"{self._api_base}{path}",
                headers=self._headers(),
            )
        if response.status >= 400:
            text = await response.text()
            raise HaloApiError(f"GET {path} failed: HTTP {response.status}: {text[:200]}")
        return await response.json(content_type=None)

    async def _async_refresh_if_needed(self) -> None:
        if not self._access_token or time.time() >= self._expires_at - TOKEN_REFRESH_SKEW_SECONDS:
            await self.async_refresh_token()

    async def async_refresh_token(self) -> None:
        if not self._client_secret:
            raise HaloApiError("Halo OAuth client secret is required to refresh tokens")
        response = await self._session.post(
            f"{self._auth_base}/connect/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            },
            headers={"Accept": "application/json"},
        )
        payload = await response.json(content_type=None)
        if response.status >= 400:
            raise HaloApiError(f"Token refresh failed: HTTP {response.status}: {payload}")
        self._access_token = payload["access_token"]
        self._refresh_token = payload.get("refresh_token", self._refresh_token)
        self._expires_at = (
            time.time() + int(payload.get("expires_in", 0)) - TOKEN_REFRESH_SKEW_SECONDS
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "Halo-Client": (
                f"clientId={self._client_id}&version=2.11.0"
                "&appInstanceId=00000000-0000-0000-0000-000000000000"
                "&timezone=America%2FNew_York"
            ),
            "Halo-Amplitude-SessionId": "0",
        }
