from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

if TYPE_CHECKING:
    from aiohttp import ClientSession

TOKEN_REFRESH_SKEW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
OPTIONAL_WALK_HISTORY_TIMEOUT_SECONDS = 5
# Transient statuses worth retrying; anything else surfaces immediately.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# Delays between attempts; total attempts = len(RETRY_BACKOFF_SECONDS) + 1.
RETRY_BACKOFF_SECONDS = (1.0, 3.0)

_LOGGER = logging.getLogger(__name__)


def _safe_walk_number_or_string(value: Any) -> int | float | str | None:
    """Keep only scalar walk metrics the fail-soft extractors can normalize."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    return value


def _sanitize_walk_record(raw_walk: dict[str, Any]) -> dict[str, Any]:
    """Copy only the privacy-safe walk fields needed by the entity extractors."""
    walk: dict[str, Any] = {}
    for key in ("endedAt", "startedAt"):
        if isinstance(value := raw_walk.get(key), str):
            walk[key] = value
    if raw_walk.get("startTrigger") == "button":
        walk["startTrigger"] = "button"

    raw_pets = raw_walk.get("pets")
    if isinstance(raw_pets, list):
        pets: list[dict[str, Any]] = []
        for raw_pet in raw_pets:
            if not isinstance(raw_pet, dict):
                continue
            pet: dict[str, Any] = {}
            if isinstance(pet_id := raw_pet.get("id"), str):
                pet["id"] = pet_id
            for key in ("walkedDurationInSeconds", "walkedDistanceInMeters"):
                if (value := _safe_walk_number_or_string(raw_pet.get(key))) is not None:
                    pet[key] = value
            pets.append(pet)
        walk["pets"] = pets
    return walk


class HaloApiError(Exception):
    """Raised when Halo API calls fail."""


class HaloAuthError(HaloApiError):
    """Raised when Halo authentication (login or token refresh) fails."""


class HaloCollarNotFound(HaloApiError):
    """Raised when Halo definitively reports that the target collar was not found."""


class HaloWriteOutcomeUnknown(HaloApiError):
    """Raised when a dispatched write may have reached Halo but was not confirmed."""


@dataclass(slots=True)
class HaloState:
    pets: list[dict[str, Any]]
    collars: list[dict[str, Any]]
    subscription: dict[str, Any]
    server_time: str | None = None
    walks: list[dict[str, Any]] = field(default_factory=list)


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
        self._refresh_lock = asyncio.Lock()

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
        if not isinstance(pets, list):
            raise HaloApiError("Halo pet state response was not a list")
        if not isinstance(collars, list):
            raise HaloApiError("Halo collar state response was not a list")
        if not isinstance(subscription, dict):
            raise HaloApiError("Halo subscription state response was not an object")

        walks: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(OPTIONAL_WALK_HISTORY_TIMEOUT_SECONDS):
                walk_history = await self.async_get("/walk/my?page=1&pageSize=10")
        except HaloAuthError:
            raise
        except (TimeoutError, HaloApiError):
            _LOGGER.warning("Halo walk history was unavailable; continuing without walk history")
        else:
            if isinstance(walk_history, dict) and isinstance(walk_history.get("results"), list):
                walks = [
                    _sanitize_walk_record(walk)
                    for walk in walk_history["results"][:10]
                    if isinstance(walk, dict)
                ]
        return HaloState(
            pets=pets,
            collars=collars,
            subscription=subscription,
            server_time=server_time if isinstance(server_time, str) else None,
            walks=walks,
        )

    async def async_get_many(self, *paths: str) -> list[Any]:
        return [await self.async_get(path) for path in paths]

    async def async_get(self, path: str) -> Any:
        await self._async_refresh_if_needed()
        request_access_token = self._access_token
        status, payload = await self._async_get_with_retry(path)
        if status == 401:
            # Access token rejected despite looking valid; refresh once and retry.
            await self.async_refresh_token(rejected_access_token=request_access_token)
            status, payload = await self._async_get_with_retry(path)
            if status == 401:
                raise HaloAuthError(f"GET {path} unauthorized even after a token refresh")
        if status >= 400:
            raise HaloApiError(f"GET {path} failed: HTTP {status}: {str(payload)[:200]}")
        return payload

    async def async_set_fences_enabled(
        self,
        pet_id: str,
        *,
        enabled: bool,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Set the pet's fence mode through Halo's instant-mode endpoint.

        Writes are deliberately not retried on 429/5xx or connection errors:
        callers must refresh state before deciding whether another write is safe.
        """
        if not pet_id:
            raise HaloApiError("Pet ID is required to change fence mode")
        payload = {"modePatch": {"fencesOn": enabled}}
        path = f"/pet/{quote(pet_id, safe='')}/instant-mode"
        return await self._async_put_json(path, payload, pre_dispatch=pre_dispatch)

    async def async_find_collar(
        self,
        collar_id: str,
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        """Ask one collar to blink and play Halo's ten-second Return Whistle.

        This physical command has no response body or durable confirmation state.
        It is sent at most once and is never retried after dispatch.
        """
        if not collar_id:
            raise HaloApiError("Collar ID is required to find a collar")
        path = f"/collar/{quote(collar_id, safe='')}/find"
        await self._async_put_no_content(path, pre_dispatch=pre_dispatch)

    async def _async_put_no_content(
        self,
        path: str,
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        await self._async_refresh_if_needed()
        response = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                if pre_dispatch is not None:
                    pre_dispatch()
                response = await self._session.put(
                    f"{self._api_base}{path}",
                    headers=self._headers(),
                    allow_redirects=False,
                )
                if response.status == 404:
                    raise HaloCollarNotFound("Halo collar was not found")
                if not 200 <= response.status < 300:
                    raise HaloWriteOutcomeUnknown(
                        f"PUT {path} returned HTTP {response.status}; outcome is unknown"
                    )
        except (HaloCollarNotFound, HaloWriteOutcomeUnknown):
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise HaloWriteOutcomeUnknown(
                f"PUT {path} dispatch failed; outcome is unknown"
            ) from err
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()

    async def _async_put_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        await self._async_refresh_if_needed()
        response = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await self._async_put_once(path, payload, pre_dispatch=pre_dispatch)
                if response.status == 401:
                    await response.text()
                    raise HaloWriteOutcomeUnknown(
                        f"PUT {path} returned HTTP 401; write was not retried "
                        "because outcome is unknown"
                    )
                if not 200 <= response.status < 300:
                    text = await response.text()
                    raise HaloWriteOutcomeUnknown(
                        f"PUT {path} returned HTTP {response.status}; "
                        f"outcome is unknown: {text[:200]}"
                    )
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    raise HaloWriteOutcomeUnknown(
                        f"PUT {path} returned an invalid success response; outcome is unknown"
                    )
                return result
        except HaloWriteOutcomeUnknown:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            raise HaloWriteOutcomeUnknown(
                f"PUT {path} dispatch or response processing failed; outcome is unknown"
            ) from err
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()

    async def _async_put_once(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> Any:
        try:
            if pre_dispatch is not None:
                pre_dispatch()
            return await self._session.put(
                f"{self._api_base}{path}",
                json=payload,
                headers=self._headers(),
                allow_redirects=False,
            )
        except aiohttp.ClientError as err:
            raise HaloWriteOutcomeUnknown(
                f"PUT {path} transport failed after dispatch; outcome is unknown: {err!r}"
            ) from err

    async def _async_get_with_retry(self, path: str) -> tuple[int, Any]:
        """Complete a GET under one bounded attempt budget, with safe retries."""
        attempts = len(RETRY_BACKOFF_SECONDS) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt - 1])
            response = None
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    response = await self._session.get(
                        f"{self._api_base}{path}",
                        headers=self._headers(),
                    )
                    status = response.status
                    if status >= 400:
                        payload: Any = await response.text()
                    else:
                        payload = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                last_error = err
                if attempt == attempts - 1:
                    raise HaloApiError(f"GET {path} failed: {err!r}") from err
                continue
            finally:
                release = getattr(response, "release", None)
                if callable(release):
                    release()
            if status in RETRYABLE_STATUS and attempt < attempts - 1:
                continue
            return status, payload
        raise AssertionError(f"unreachable after GET failure: {last_error!r}")

    async def _async_refresh_if_needed(self) -> None:
        if self._token_needs_refresh():
            async with self._refresh_lock:
                if self._token_needs_refresh():
                    await self._async_refresh_token_unlocked()

    def _token_needs_refresh(self) -> bool:
        return (
            not self._access_token or time.time() >= self._expires_at - TOKEN_REFRESH_SKEW_SECONDS
        )

    async def async_refresh_token(self, *, rejected_access_token: str | None = None) -> None:
        """Refresh once, deduplicating concurrent expiry and 401 recovery paths."""
        async with self._refresh_lock:
            if rejected_access_token is not None and self._access_token != rejected_access_token:
                return
            await self._async_refresh_token_unlocked()

    async def _async_refresh_token_unlocked(self) -> None:
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
        response = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                response = await self._session.post(
                    f"{self._auth_base}/connect/token",
                    data=data,
                    headers=self._token_headers(),
                )
                text = await response.text()
                if response.status >= 500:
                    # Identity-server outage, not bad credentials; must not trigger reauth.
                    raise HaloApiError(
                        f"Token endpoint error: HTTP {response.status}: {text[:200]}"
                    )
                payload = await response.json(content_type=None)
        except HaloApiError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HaloApiError(f"Token request failed: {err!r}") from err
        except ValueError as err:
            if response is not None and 400 <= response.status < 500:
                raise HaloAuthError(
                    f"Token request was rejected with invalid JSON: HTTP {response.status}"
                ) from err
            raise HaloApiError("Token request returned an invalid JSON response") from err
        finally:
            release = getattr(response, "release", None)
            if callable(release):
                release()
        if response is None:
            raise HaloApiError("Token request completed without a response")
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
