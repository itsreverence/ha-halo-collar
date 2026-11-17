from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

if TYPE_CHECKING:
    from uuid import UUID

    from aiohttp import ClientSession

    from .transport import HaloWriteTransport

TOKEN_REFRESH_SKEW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 30
OPTIONAL_SERVER_TIME_TIMEOUT_SECONDS = 5
OPTIONAL_WALK_HISTORY_TIMEOUT_SECONDS = 5
RECALL_WINDOW_TIMEOUT_SECONDS = 5
# RunInstantCorrectionDto.CommandNumber is a signed 32-bit integer.
HALO_RECALL_MAX_COMMAND_NUMBER = 2**31 - 1
# The only correction type this integration can send. The same endpoint also delivers
# Warning, FirstTime and Escalation corrections, so no caller can choose the type.
_RECALL_CORRECTION_TYPE = "returnwhistle"
# MobileDataRequestDto fields that describe Home Assistant rather than a phone. Platform
# and idiom are Halo's DevicePlatformDto and DeviceIdiomDto names, lowercased like the
# enum values the Halo app sends.
_RECALL_DEVICE = {
    "model": "Home Assistant",
    "manufacturer": "Home Assistant",
    "platform": "unknown",
    "idiom": "unknown",
}
_TIMESPAN_PATTERN = re.compile(r"(?:(\d{1,8})\.)?(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,7}))?")
_PATH_ID_PATTERN = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,127}")
# The Halo app's own walk-history page size. A wider window reduces one dog's walks
# crowding another dog out on multi-dog accounts, in a single request per poll.
WALK_HISTORY_PAGE_SIZE = 30
# Halo rejects API calls from app versions below its server-enforced Android floor.
# Keep this aligned with a publicly released official Android app version.
HALO_ANDROID_CLIENT_VERSION = "2.13.0"
_CLIENT_VERSION_PATTERN = re.compile(r"\d{1,5}(?:\.\d{1,5}){2,3}")
# Transient statuses worth retrying; anything else surfaces immediately.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
# Delays between attempts; total attempts = len(RETRY_BACKOFF_SECONDS) + 1.
RETRY_BACKOFF_SECONDS = (1.0, 3.0)

_LOGGER = logging.getLogger(__name__)


def _error_text(err: BaseException) -> str:
    """Name and str() of a transport error. repr() can carry request headers."""
    detail = str(err)
    return f"{type(err).__name__}: {detail}" if detail else type(err).__name__


def _safe_walk_number_or_string(value: Any) -> int | float | str | None:
    """Keep only scalar walk metrics the fail-soft extractors can normalize."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
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
            for key in ("walkedDurationInSeconds", "walkedDistanceInMeters", "feedbacksCount"):
                if (value := _safe_walk_number_or_string(raw_pet.get(key))) is not None:
                    pet[key] = value
            pets.append(pet)
        walk["pets"] = pets
    return walk


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    """Copy one provider mapping, treating a malformed optional envelope as unknown."""
    return dict(value) if isinstance(value, dict) else {}


def _normalize_pet(raw_pet: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional pet mapping envelopes used by entity extractors."""
    pet = dict(raw_pet)
    for key in ("collarInfo", "metrics", "telemetry"):
        if key in pet:
            pet[key] = _mapping_or_empty(pet[key])
    return pet


def _normalize_collar(raw_collar: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional collar mapping envelopes used by entity extractors."""
    collar = dict(raw_collar)
    for key in ("diagnostics", "firmware", "issues", "telemetry"):
        if key in collar:
            collar[key] = _mapping_or_empty(collar[key])

    if "petInfo" in collar:
        pet_info = _mapping_or_empty(collar["petInfo"])
        for key in ("lastLocation", "location", "telemetry"):
            if key in pet_info:
                pet_info[key] = _mapping_or_empty(pet_info[key])
        collar["petInfo"] = pet_info
    return collar


def _validate_unique_record_ids(records: list[dict[str, Any]], *, label: str) -> None:
    """Require stable, unique provider IDs before entities are constructed."""
    ids = [record.get("id") for record in records]
    if any(not isinstance(record_id, str) or not record_id for record_id in ids):
        raise HaloApiError(f"Halo {label} state response contained an item without a valid id")
    if len(set(ids)) != len(ids):
        raise HaloApiError(f"Halo {label} state response contained duplicate ids")


def _client_version_tuple(value: Any) -> tuple[int, int, int, int] | None:
    """Parse a bounded three- or four-part numeric Halo client version."""
    if not isinstance(value, str) or _CLIENT_VERSION_PATTERN.fullmatch(value) is None:
        return None
    parts = tuple(int(part) for part in value.split("."))
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else 0)


def _is_newer_client_version(candidate: str, current: str) -> bool:
    """Compare two already-bounded client version strings safely."""
    candidate_parts = _client_version_tuple(candidate)
    current_parts = _client_version_tuple(current)
    return (
        candidate_parts is not None
        and current_parts is not None
        and candidate_parts > current_parts
    )


def _newer_minimum_client_version(payload: Any, current: str) -> str | None:
    """Return a strictly newer provider floor from a validated error response."""
    if not isinstance(payload, str):
        return None
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("details"), dict):
        return None
    details = body["details"]
    if type(body.get("errorCode")) is not int or body["errorCode"] != 3003:
        return None
    minimum = details.get("minimalSupportedVersion")
    minimum_parts = _client_version_tuple(minimum)
    current_parts = _client_version_tuple(current)
    if minimum_parts is None or current_parts is None or minimum_parts <= current_parts:
        return None
    return minimum


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
    # "ok", "unavailable" (timeout or error), or "unexpected_response" (unrecognized shape).
    walk_history_status: str = "ok"
    server_time_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class HaloRecallCommand:
    mobile_id: int
    command_number: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HaloRecallAccepted:
    """Halo accepted the command. Halo reports no playback confirmation."""


@dataclass(frozen=True, slots=True)
class HaloRecallStaleNumber:
    """Halo rejected a reused or lower command number. The whistle did not play."""

    current_command_number: int | None


@dataclass(frozen=True, slots=True)
class HaloRecallExpired:
    """Halo rejected the command because it expired. The whistle did not play."""


HaloRecallReply = HaloRecallAccepted | HaloRecallStaleNumber | HaloRecallExpired


def halo_mobile_id(value: Any) -> int | None:
    """Parse MobileIdDto.MobileId, a signed 16-bit integer."""
    if type(value) is not int or not -(2**15) <= value < 2**15:
        return None
    return value


def _recall_command_number(value: Any) -> int | None:
    if type(value) is not int or not 0 <= value <= HALO_RECALL_MAX_COMMAND_NUMBER:
        return None
    return value


def _parse_recall_reply(payload: dict[str, Any]) -> HaloRecallReply:
    """Map TestCorrectionResponseDto; anything but a definitive answer is ambiguous."""
    result = payload.get("result")
    outcome = result.casefold() if isinstance(result, str) else None
    if outcome == "success":
        return HaloRecallAccepted()
    if outcome == "oldcommandnumber":
        return HaloRecallStaleNumber(_recall_command_number(payload.get("currentCommandNumber")))
    if outcome == "commandexpired":
        return HaloRecallExpired()
    raise HaloWriteOutcomeUnknown("Halo Recall reply had no definitive result; outcome is unknown")


def _parse_timespan(value: Any) -> timedelta | None:
    """Parse a positive .NET TimeSpan ([d.]hh:mm:ss[.fffffff]) or a number of seconds."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            window = timedelta(seconds=value)
        elif isinstance(value, str) and (match := _TIMESPAN_PATTERN.fullmatch(value)):
            days, hours, minutes, seconds, fraction = match.groups()
            if int(hours) > 23 or int(minutes) > 59 or int(seconds) > 59:
                return None
            window = timedelta(
                days=int(days or 0),
                hours=int(hours),
                minutes=int(minutes),
                seconds=int(seconds),
                microseconds=int((fraction or "0").ljust(7, "0")) // 10,
            )
        else:
            return None
    except (OverflowError, ValueError):
        return None
    return window if window > timedelta(0) else None


def _utc_wire_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path_id(value: Any, kind: str) -> str:
    """Return a Halo ID that can only form one URL path segment.

    Only URL-unreserved characters pass, so nothing needs escaping, and a leading dot
    is refused: URL normalization drops a "." or ".." segment, which would send the
    write to a different route.
    """
    if not isinstance(value, str) or _PATH_ID_PATTERN.fullmatch(value) is None:
        raise HaloApiError(f"Halo {kind} ID is not safe to use in a request path")
    return value


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
        write_transport: HaloWriteTransport | None = None,
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session = session
        self._write_transport = write_transport
        self._on_token_update = on_token_update
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_base = api_base.rstrip("/")
        self._auth_base = auth_base.rstrip("/")
        self._client_version = HALO_ANDROID_CLIENT_VERSION
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
        pets, collars, subscription = await self.async_get_many(
            "/pet/my", "/collar/my", "/subscription/my"
        )
        if not isinstance(pets, list):
            raise HaloApiError("Halo pet state response was not a list")
        if not all(isinstance(pet, dict) for pet in pets):
            raise HaloApiError("Halo pet state response contained a non-object item")
        if not isinstance(collars, list):
            raise HaloApiError("Halo collar state response was not a list")
        if not all(isinstance(collar, dict) for collar in collars):
            raise HaloApiError("Halo collar state response contained a non-object item")
        if not isinstance(subscription, dict):
            raise HaloApiError("Halo subscription state response was not an object")

        _validate_unique_record_ids(pets, label="pet")
        _validate_unique_record_ids(collars, label="collar")
        pets = [_normalize_pet(pet) for pet in pets]
        collars = [_normalize_collar(collar) for collar in collars]

        server_time = None
        server_time_requested_at = monotonic()
        try:
            async with asyncio.timeout(OPTIONAL_SERVER_TIME_TIMEOUT_SECONDS):
                server_time = await self.async_get("/system/server-date-time")
        except HaloAuthError:
            raise
        except (TimeoutError, HaloApiError):
            _LOGGER.warning("Halo server time was unavailable; continuing without server time")

        walks: list[dict[str, Any]] = []
        walk_history_status = "ok"
        try:
            async with asyncio.timeout(OPTIONAL_WALK_HISTORY_TIMEOUT_SECONDS):
                walk_history = await self.async_get(
                    f"/walk/my?page=1&pageSize={WALK_HISTORY_PAGE_SIZE}"
                )
        except HaloAuthError:
            raise
        except (TimeoutError, HaloApiError):
            _LOGGER.warning("Halo walk history was unavailable; continuing without walk history")
            walk_history_status = "unavailable"
        else:
            if isinstance(walk_history, dict) and isinstance(walk_history.get("results"), list):
                walks = [
                    _sanitize_walk_record(walk)
                    for walk in walk_history["results"][:WALK_HISTORY_PAGE_SIZE]
                    if isinstance(walk, dict)
                ]
            else:
                walk_history_status = "unexpected_response"
        has_server_time = isinstance(server_time, str)
        return HaloState(
            pets=pets,
            collars=collars,
            subscription=subscription,
            server_time=server_time if has_server_time else None,
            walks=walks,
            walk_history_status=walk_history_status,
            server_time_monotonic=server_time_requested_at if has_server_time else None,
        )

    async def async_get_many(self, *paths: str) -> list[Any]:
        return [await self.async_get(path) for path in paths]

    async def async_get(self, path: str) -> Any:
        await self._async_refresh_if_needed()
        request_access_token = self._access_token
        request_client_version = self._client_version
        status, payload = await self._async_get_with_retry(
            path, client_version=request_client_version
        )
        client_version_retried = False
        if status == 400 and (
            minimum := _newer_minimum_client_version(payload, request_client_version)
        ):
            if _is_newer_client_version(minimum, self._client_version):
                self._client_version = minimum
            client_version_retried = True
            _LOGGER.info("Halo raised its minimum client version; retrying one read")
            status, payload = await self._async_get_with_retry(path)
        if status == 401:
            # Access token rejected despite looking valid; refresh once and retry.
            await self.async_refresh_token(rejected_access_token=request_access_token)
            request_client_version = self._client_version
            status, payload = await self._async_get_with_retry(
                path, client_version=request_client_version
            )
            if (
                not client_version_retried
                and status == 400
                and (minimum := _newer_minimum_client_version(payload, request_client_version))
            ):
                if _is_newer_client_version(minimum, self._client_version):
                    self._client_version = minimum
                client_version_retried = True
                _LOGGER.info("Halo raised its minimum client version; retrying one read")
                status, payload = await self._async_get_with_retry(path)
            if status == 401:
                raise HaloAuthError(f"GET {path} unauthorized even after a token refresh")
        if not 200 <= status < 300:
            raise HaloApiError(f"GET {path} failed: HTTP {status}")
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
        path = f"/pet/{_path_id(pet_id, 'pet')}/instant-mode"
        payload = {"modePatch": {"fencesOn": enabled}}
        return await self._async_write_json("PUT", path, payload, pre_dispatch=pre_dispatch)

    async def async_get_recall_window(self) -> timedelta:
        try:
            async with asyncio.timeout(RECALL_WINDOW_TIMEOUT_SECONDS):
                configuration = await self.async_get("/configuration")
        except TimeoutError:
            raise HaloApiError("Halo configuration read timed out") from None
        correction = configuration.get("correction") if isinstance(configuration, dict) else None
        window = _parse_timespan(
            correction.get("runPositiveCorrectionExpiration")
            if isinstance(correction, dict)
            else None
        )
        if window is None:
            raise HaloApiError("Halo did not report a valid Recall command window")
        return window

    async def async_register_recall_device(
        self,
        installation_id: UUID,
        *,
        version_string: str,
        pre_dispatch: Callable[[], None],
    ) -> int:
        """Register this installation as a Halo mobile device and return its mobileId.

        Halo has no route to remove the registration. The request is sent at most once.
        A reply without a usable mobileId is ambiguous: Halo may have created a record.
        """
        payload = {
            "internalMobileId": str(installation_id),
            "versionString": version_string,
            **_RECALL_DEVICE,
        }
        result = await self._async_write_json(
            "POST", "/account/mobile-data", payload, pre_dispatch=pre_dispatch
        )
        mobile_id = halo_mobile_id(result.get("mobileId"))
        if mobile_id is None:
            raise HaloWriteOutcomeUnknown(
                "Halo registration reply had no usable mobileId; outcome is unknown"
            )
        return mobile_id

    async def async_send_recall_whistle(
        self,
        pet_id: str,
        command: HaloRecallCommand,
        *,
        pre_dispatch: Callable[[], None],
    ) -> HaloRecallReply:
        """Ask Halo to play one pet's Recall whistle, at most once and never retried."""
        path = f"/pet/{_path_id(pet_id, 'pet')}/run-instant-correction"
        payload = {
            "mobileId": command.mobile_id,
            "commandNumber": command.command_number,
            "expirationDate": _utc_wire_time(command.expires_at),
            "correctionType": _RECALL_CORRECTION_TYPE,
        }
        result = await self._async_write_json("POST", path, payload, pre_dispatch=pre_dispatch)
        return _parse_recall_reply(result)

    async def async_find_collar(
        self,
        collar_id: str,
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        """Ask one collar to blink and play Halo's find sound (not the Recall whistle).

        This physical command has no response body or durable confirmation state.
        It is sent at most once and is never retried after dispatch.
        """
        path = f"/collar/{_path_id(collar_id, 'collar')}/find"
        await self._async_put_no_content(path, pre_dispatch=pre_dispatch)

    async def _async_put_no_content(
        self,
        path: str,
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> None:
        if self._write_transport is None:
            raise HaloApiError("Safe write transport is unavailable")
        await self._async_refresh_if_needed()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._write_transport.request(
                    "PUT",
                    f"{self._api_base}{path}",
                    headers=self._headers(),
                    pre_dispatch=pre_dispatch,
                ) as response:
                    if response.status == 404:
                        raise HaloCollarNotFound("Halo collar was not found")
                    if not 200 <= response.status < 300:
                        raise HaloWriteOutcomeUnknown(
                            f"Halo API write returned HTTP {response.status}; outcome is unknown"
                        )
        except (HaloCollarNotFound, HaloWriteOutcomeUnknown):
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError):
            raise HaloWriteOutcomeUnknown(
                "Halo API write dispatch failed; outcome is unknown"
            ) from None

    async def _async_write_json(
        self,
        method: Literal["PUT", "POST"],
        path: str,
        payload: dict[str, Any],
        *,
        pre_dispatch: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        if self._write_transport is None:
            raise HaloApiError("Safe write transport is unavailable")
        await self._async_refresh_if_needed()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with self._write_transport.request(
                    method,
                    f"{self._api_base}{path}",
                    headers=self._headers(),
                    payload=payload,
                    pre_dispatch=pre_dispatch,
                ) as response:
                    if response.status == 401:
                        await response.text()
                        raise HaloWriteOutcomeUnknown(
                            "Halo API write returned HTTP 401; write was not retried "
                            "because outcome is unknown"
                        )
                    if not 200 <= response.status < 300:
                        await response.text()
                        raise HaloWriteOutcomeUnknown(
                            f"Halo API write returned HTTP {response.status}; outcome is unknown"
                        )
                    result = await response.json(content_type=None)
                    if not isinstance(result, dict):
                        raise HaloWriteOutcomeUnknown(
                            "Halo API write returned an invalid success response; "
                            "outcome is unknown"
                        )
                    return result
        except HaloWriteOutcomeUnknown:
            raise
        except (TimeoutError, aiohttp.ClientError, ValueError):
            raise HaloWriteOutcomeUnknown(
                "Halo API write dispatch or response processing failed; outcome is unknown"
            ) from None

    async def _async_get_with_retry(
        self, path: str, *, client_version: str | None = None
    ) -> tuple[int, Any]:
        """Complete a GET under one bounded attempt budget, with safe retries."""
        if client_version is None:
            client_version = self._client_version
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
                        headers=self._headers(client_version=client_version),
                        allow_redirects=False,
                    )
                    status = response.status
                    if not 200 <= status < 300:
                        payload: Any = await response.text()
                    else:
                        payload = await response.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                last_error = err
                if attempt == attempts - 1:
                    raise HaloApiError(f"GET {path} failed: {_error_text(err)}") from err
                continue
            finally:
                release = getattr(response, "release", None)
                if callable(release):
                    release()
            if status in RETRYABLE_STATUS and attempt < attempts - 1:
                continue
            return status, payload
        detail = _error_text(last_error) if last_error is not None else "no error recorded"
        raise AssertionError(f"unreachable after GET failure: {detail}")

    async def _async_refresh_if_needed(self) -> None:
        if self._token_needs_refresh():
            async with self._refresh_lock:
                if self._token_needs_refresh():
                    await self._async_refresh_token_unlocked()

    def _token_needs_refresh(self) -> bool:
        return not self._access_token or time.time() >= self._expires_at

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
                    allow_redirects=False,
                )
                if response.status == 429:
                    raise HaloApiError("Token endpoint error: HTTP 429")
                await response.text()
                if 300 <= response.status < 400:
                    raise HaloApiError(f"Token endpoint returned HTTP {response.status}")
                if response.status >= 500:
                    # Identity-server outage, not bad credentials; must not trigger reauth.
                    raise HaloApiError(f"Token endpoint error: HTTP {response.status}")
                payload = await response.json(content_type=None)
        except HaloApiError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            raise HaloApiError(f"Token request failed: {_error_text(err)}") from err
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
            _LOGGER.warning("Halo token request rejected: HTTP %s", response.status)
            raise HaloAuthError(f"Token request failed: HTTP {response.status}")
        if not isinstance(payload, dict):
            raise HaloApiError("Token request returned an invalid success payload")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token", self._refresh_token)
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise HaloAuthError("Token request returned no access_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise HaloApiError("Token request returned an invalid refresh_token")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise HaloApiError("Token request returned an invalid expires_in")
        try:
            expires_in_seconds = float(expires_in)
        except (OverflowError, ValueError):
            raise HaloApiError("Token request returned an invalid expires_in") from None
        if (
            not math.isfinite(expires_in_seconds)
            or expires_in_seconds <= TOKEN_REFRESH_SKEW_SECONDS
        ):
            raise HaloApiError("Token request returned an invalid expires_in")

        # Commit the fully validated token set atomically.
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = time.time() + expires_in_seconds - TOKEN_REFRESH_SKEW_SECONDS
        if self._on_token_update is not None:
            self._on_token_update(self.token_snapshot)

    def _headers(self, *, client_version: str | None = None) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            **self._client_headers(client_version=client_version),
        }

    def _token_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            **self._client_headers(),
        }

    def _client_headers(self, *, client_version: str | None = None) -> dict[str, str]:
        if client_version is None:
            client_version = self._client_version
        return {
            "Halo-Client": (
                f"clientId={self._client_id}&version={client_version}"
                "&appInstanceId=00000000-0000-0000-0000-000000000000"
                "&timezone=America%2FNew_York"
            ),
            "Halo-Amplitude-SessionId": "0",
        }
