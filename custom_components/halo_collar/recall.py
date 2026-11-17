from __future__ import annotations

import hashlib
import time
import uuid
from asyncio import Lock
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, cast

from .api import (
    HALO_RECALL_MAX_COMMAND_NUMBER,
    HaloRecallAccepted,
    HaloRecallCommand,
    HaloRecallExpired,
    HaloState,
    HaloWriteOutcomeUnknown,
    halo_mobile_id,
)
from .const import CONF_ENABLE_RECALL_WHISTLE, RECALL_COOLDOWN_SECONDS
from .controls import (
    HaloControlError,
    async_finish_committed_phase,
    async_preflight_refresh,
    control_stale_after,
    ensure_live_runtime,
)
from .helpers import (
    active_walk_state,
    is_online,
    last_telemetry,
    nested,
    parse_timestamp,
    pet_for_collar,
    recall_command_support,
)

RECALL_MAX_LIFETIME = timedelta(seconds=30)
RECALL_MIN_REMAINING_SECONDS = 2.0
_STORAGE_VERSION = 1


class RecallReason(StrEnum):
    READY = "ready"
    DISABLED = "disabled"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    REGISTRATION_REQUIRED = "registration_required"
    REGISTRATION_UNCERTAIN = "registration_uncertain"
    CLOUD_UNAVAILABLE = "cloud_unavailable"
    MAPPING_UNAVAILABLE = "mapping_unavailable"
    REPORT_UNKNOWN = "report_unknown"
    REPORT_STALE = "report_stale"
    WALK_UNKNOWN = "walk_unknown"
    WALK_ACTIVE = "walk_active"
    FIRMWARE_UNKNOWN = "firmware_unknown"
    FIRMWARE_UNSUPPORTED = "firmware_unsupported"
    COUNTER_EXHAUSTED = "counter_exhausted"
    COOLDOWN = "cooldown"


REFUSALS: Mapping[RecallReason, str] = MappingProxyType(
    {
        RecallReason.DISABLED: "Halo Recall whistle is disabled in integration options",
        RecallReason.STORAGE_UNAVAILABLE: (
            "Halo Recall whistle state could not be read, so nothing was sent"
        ),
        RecallReason.REGISTRATION_REQUIRED: (
            "Home Assistant is not registered with Halo for the Recall whistle; "
            "register it in the integration options"
        ),
        RecallReason.REGISTRATION_UNCERTAIN: (
            "The Halo registration outcome is unknown and is never retried; "
            "see the Recall whistle recovery steps"
        ),
        RecallReason.CLOUD_UNAVAILABLE: "Could not refresh Halo state before the Recall whistle",
        RecallReason.MAPPING_UNAVAILABLE: "Halo pet/collar mapping is unavailable",
        RecallReason.REPORT_UNKNOWN: "Halo has not reported when the collar last reported",
        RecallReason.REPORT_STALE: "Halo collar telemetry is stale",
        RecallReason.WALK_UNKNOWN: "Recall whistle is blocked while the walk state is unknown",
        RecallReason.WALK_ACTIVE: "Recall whistle is blocked during an active walk",
        RecallReason.FIRMWARE_UNKNOWN: (
            "Halo did not report whether this collar supports numbered commands"
        ),
        RecallReason.FIRMWARE_UNSUPPORTED: (
            "This collar's firmware does not support numbered, expiring commands"
        ),
        RecallReason.COUNTER_EXHAUSTED: "This collar has used every Recall command number",
        RecallReason.COOLDOWN: "Halo Recall whistle cooldown is still running",
    }
)


@dataclass(frozen=True, slots=True)
class Unregistered:
    """No registration request was ever prepared for this entry."""


@dataclass(frozen=True, slots=True)
class RegistrationPending:
    """Saved before the one registration request. Found on load, it needs recovery."""

    installation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RegistrationUncertain:
    """The registration request was sent and its outcome is unknown. It is never resent."""

    installation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Registered:
    mobile_id: int
    # Next command number per collar serial; Halo keys its sequence by (mobileId, serial).
    # A missing serial starts at 0. HALO_RECALL_MAX_COMMAND_NUMBER + 1 means exhausted.
    next_by_serial: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


Registration = Unregistered | RegistrationPending | RegistrationUncertain | Registered

_STATE_NAMES: Mapping[type, str] = {
    Unregistered: "unregistered",
    RegistrationPending: "pending",
    RegistrationUncertain: "uncertain",
    Registered: "registered",
}


@dataclass(frozen=True, slots=True)
class RecallTarget:
    pet_id: str
    collar_id: str
    serial: str


class RecallStorageError(Exception):
    """Local Recall state could not be read or written."""


class RecallStore(Protocol):
    async def async_load(self) -> dict[str, Any] | None:
        """Return the saved document, or None only when nothing was ever saved.

        Anything else that is not a saved document raises RecallStorageError.
        """

    async def async_save(self, data: dict[str, Any]) -> None:
        """Replace the saved document atomically, raising RecallStorageError on failure."""


def recall_account_binding(identity: Any) -> str | None:
    if not isinstance(identity, str) or not identity.strip():
        return None
    return hashlib.sha256(identity.strip().lower().encode()).hexdigest()


def registration_state_name(registration: Registration | None) -> str:
    return "unavailable" if registration is None else _STATE_NAMES[type(registration)]


def recall_wearers(state: HaloState) -> dict[str, dict[str, Any] | None]:
    """Every dog in this snapshot, with the collar a press would whistle, or None."""
    return {
        pet["id"]: None if (resolved := _resolve(state, pet["id"])) is None else resolved[1]
        for pet in state.pets
    }


def recall_serial(state: HaloState, pet_id: str) -> str | None:
    resolved = _resolve(state, pet_id)
    return None if resolved is None else resolved[2].serial


def _resolve(
    state: HaloState, pet_id: str
) -> tuple[dict[str, Any], dict[str, Any], RecallTarget] | None:
    pets = [pet for pet in state.pets if pet.get("id") == pet_id]
    if len(pets) != 1:
        return None
    pet = pets[0]
    collar_id = nested(pet, "collarInfo", "id")
    collars = [collar for collar in state.collars if collar.get("id") == collar_id]
    if not isinstance(collar_id, str) or not collar_id or len(collars) != 1:
        return None
    collar = collars[0]
    if nested(collar, "petInfo", "id") != pet_id:
        return None
    if pet_for_collar(state.pets, collar, state.collars) is not pet:
        return None
    serial = collar.get("serialNumber")
    if not isinstance(serial, str) or not serial:
        return None
    if sum(item.get("serialNumber") == serial for item in state.collars) != 1:
        return None
    return pet, collar, RecallTarget(pet_id, collar_id, serial)


def classify_recall(
    state: HaloState,
    pet_id: str,
    *,
    enabled: bool,
    registration: Registration | None,
    cloud_success: bool,
    stale_after: float,
    now: datetime,
    cooldown_remaining: Callable[[str], float],
) -> RecallTarget | RecallReason:
    if not enabled:
        return RecallReason.DISABLED
    if (registration_reason := _registration_reason(registration)) is not None:
        return registration_reason
    if not cloud_success:
        return RecallReason.CLOUD_UNAVAILABLE
    resolved = _resolve(state, pet_id)
    if resolved is None:
        return RecallReason.MAPPING_UNAVAILABLE
    pet, collar, target = resolved
    if last_telemetry(collar) is None:
        return RecallReason.REPORT_UNKNOWN
    if not is_online(collar, now=now, stale_after=stale_after):
        return RecallReason.REPORT_STALE
    walking = active_walk_state(pet, collar)
    if walking is None:
        return RecallReason.WALK_UNKNOWN
    if walking:
        return RecallReason.WALK_ACTIVE
    supported = recall_command_support(collar)
    if supported is None:
        return RecallReason.FIRMWARE_UNKNOWN
    if not supported:
        return RecallReason.FIRMWARE_UNSUPPORTED
    registered = cast(Registered, registration)
    if registered.next_by_serial.get(target.serial, 0) > HALO_RECALL_MAX_COMMAND_NUMBER:
        return RecallReason.COUNTER_EXHAUSTED
    if cooldown_remaining(target.serial) > 0:
        return RecallReason.COOLDOWN
    return target


def _require_registered(registration: Registration | None) -> Registered:
    if (reason := _registration_reason(registration)) is not None:
        raise HaloControlError(REFUSALS[reason])
    return cast(Registered, registration)


def _registration_reason(registration: Registration | None) -> RecallReason | None:
    if registration is None:
        return RecallReason.STORAGE_UNAVAILABLE
    if isinstance(registration, Unregistered):
        return RecallReason.REGISTRATION_REQUIRED
    if isinstance(registration, Registered):
        return None
    return RecallReason.REGISTRATION_UNCERTAIN


def _enabled(entry) -> bool:
    return bool(entry.options.get(CONF_ENABLE_RECALL_WHISTLE, False))


def _server_clock(state: HaloState) -> tuple[datetime, float] | None:
    server_now = parse_timestamp(state.server_time)
    if server_now is None or state.server_time_monotonic is None:
        return None
    return server_now, state.server_time_monotonic


def _advanced(registration: Registered, serial: str, next_number: int) -> Registered:
    return Registered(
        registration.mobile_id,
        MappingProxyType({**registration.next_by_serial, serial: next_number}),
    )


def _encode(account: str, registration: Registration) -> dict[str, Any]:
    body: dict[str, Any] = {"state": registration_state_name(registration)}
    if isinstance(registration, Registered):
        body["mobile_id"] = registration.mobile_id
        body["next_by_serial"] = dict(registration.next_by_serial)
    elif not isinstance(registration, Unregistered):
        body["installation_id"] = str(registration.installation_id)
    return {"version": _STORAGE_VERSION, "account": account, "registration": body}


def _decode(data: Any, account: str) -> Registration | None:
    if not isinstance(data, dict) or data.get("version") != _STORAGE_VERSION:
        return None
    if data.get("account") != account or not isinstance(body := data.get("registration"), dict):
        return None
    state = body.get("state")
    if state == "unregistered":
        return Unregistered()
    if state in ("pending", "uncertain"):
        try:
            installation_id = uuid.UUID(body.get("installation_id"))
        except (AttributeError, TypeError, ValueError):
            return None
        if state == "pending":
            return RegistrationPending(installation_id)
        return RegistrationUncertain(installation_id)
    if state != "registered":
        return None
    mobile_id = halo_mobile_id(body.get("mobile_id"))
    counters = body.get("next_by_serial")
    if mobile_id is None or not isinstance(counters, dict):
        return None
    if not all(
        isinstance(serial, str)
        and serial
        and type(number) is int
        and 0 <= number <= HALO_RECALL_MAX_COMMAND_NUMBER + 1
        for serial, number in counters.items()
    ):
        return None
    return Registered(mobile_id, MappingProxyType(dict(counters)))


class RecallOwner:
    def __init__(self, *, store: RecallStore, account: str | None, lock: Lock) -> None:
        self._store = store
        self._account = account
        self._lock = lock
        self._loaded = False
        self._registration: Registration | None = None
        self._cooldown_until: dict[str, float] = {}
        self._restart_hold_until = 0.0

    @property
    def registration(self) -> Registration | None:
        return self._registration

    def cooldown_remaining(self, serial: str) -> float:
        deadline = max(self._cooldown_until.get(serial, 0.0), self._restart_hold_until)
        return max(0.0, deadline - time.monotonic())

    def status(self, state: HaloState, pet_id: str, *, entry, cloud_success: bool) -> RecallReason:
        result = classify_recall(
            state,
            pet_id,
            enabled=_enabled(entry),
            registration=self._registration,
            cloud_success=cloud_success,
            stale_after=control_stale_after(entry),
            now=datetime.now(UTC),
            cooldown_remaining=self.cooldown_remaining,
        )
        return result if isinstance(result, RecallReason) else RecallReason.READY

    async def async_load(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            await self._async_ensure_loaded()

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._registration = await self._async_read()
        self._loaded = True
        if isinstance(self._registration, Registered):
            self._restart_hold_until = time.monotonic() + RECALL_COOLDOWN_SECONDS

    async def _async_read(self) -> Registration | None:
        if self._account is None:
            return None
        try:
            data = await self._store.async_load()
        except RecallStorageError:
            return None
        return Unregistered() if data is None else _decode(data, self._account)

    async def _async_save(self, registration: Registration) -> None:
        data = _encode(cast(str, self._account), registration)

        async def write() -> None:
            await self._store.async_save(data)
            self._registration = registration

        await async_finish_committed_phase(write(), dispatch_committed=lambda: True)

    async def _async_raise_floor(self, serial: str, floor: int) -> None:
        registration = cast(Registered, self._registration)
        if registration.next_by_serial.get(serial, 0) >= floor:
            return
        try:
            await self._async_save(_advanced(registration, serial, floor))
        except RecallStorageError as err:
            raise HaloControlError(
                "Halo rejected an old Recall command number and the newer number could not "
                "be saved locally"
            ) from err

    async def async_press(
        self, pet_id: str, *, coordinator, client, entry, is_current: Callable[[], bool]
    ) -> None:
        """Ask Halo to play one pet's Recall whistle, sending at most one request.

        Returning means only that Halo accepted the command before it expired. Halo
        reports no playback confirmation.
        """
        async with self._lock:
            if not _enabled(entry):
                raise HaloControlError(REFUSALS[RecallReason.DISABLED])
            await self._async_ensure_loaded()
            registration = _require_registered(self._registration)
            window = min(await client.async_get_recall_window(), RECALL_MAX_LIFETIME)

            await async_preflight_refresh(
                coordinator, is_current, failure=REFUSALS[RecallReason.CLOUD_UNAVAILABLE]
            )

            strictest_stale_after = control_stale_after(entry)

            def current_strictest_stale_after() -> float:
                nonlocal strictest_stale_after
                strictest_stale_after = min(strictest_stale_after, control_stale_after(entry))
                return strictest_stale_after

            def authorized_target(stale_after: float) -> RecallTarget:
                result = classify_recall(
                    coordinator.data,
                    pet_id,
                    enabled=_enabled(entry),
                    registration=registration,
                    cloud_success=coordinator.last_update_success,
                    stale_after=stale_after,
                    now=datetime.now(UTC),
                    cooldown_remaining=self.cooldown_remaining,
                )
                if isinstance(result, RecallReason):
                    raise HaloControlError(REFUSALS[result])
                return result

            target = authorized_target(strictest_stale_after)
            clock = _server_clock(coordinator.data)
            if clock is None:
                raise HaloControlError(
                    "Halo did not report its clock, so the Recall command cannot expire safely"
                )
            server_now, sampled_at = clock
            expires_at = (server_now + window).replace(microsecond=0)
            lifetime = (expires_at - server_now).total_seconds()

            def ensure_lifetime() -> None:
                if lifetime - (time.monotonic() - sampled_at) < RECALL_MIN_REMAINING_SECONDS:
                    raise HaloControlError(
                        "Too little of the Recall command lifetime remained, so nothing was sent"
                    )

            ensure_lifetime()
            number = registration.next_by_serial.get(target.serial, 0)
            try:
                await self._async_save(_advanced(registration, target.serial, number + 1))
            except RecallStorageError as err:
                raise HaloControlError(
                    "Could not save the Recall command number locally, so nothing was sent"
                ) from err
            command = HaloRecallCommand(
                mobile_id=registration.mobile_id, command_number=number, expires_at=expires_at
            )
            dispatch_committed = False

            def validate_and_commit_dispatch() -> None:
                nonlocal dispatch_committed
                ensure_live_runtime(is_current)
                if authorized_target(current_strictest_stale_after()) != target:
                    raise HaloControlError(
                        "Halo pet/collar mapping changed before command dispatch"
                    )
                ensure_lifetime()
                self._cooldown_until[target.serial] = time.monotonic() + RECALL_COOLDOWN_SECONDS
                dispatch_committed = True

            await async_finish_committed_phase(
                _async_dispatch_and_settle(
                    owner=self,
                    client=client,
                    pet_id=pet_id,
                    serial=target.serial,
                    command=command,
                    pre_dispatch=validate_and_commit_dispatch,
                ),
                dispatch_committed=lambda: dispatch_committed,
            )

    async def async_register(
        self, *, client, version_string: str, is_current: Callable[[], bool]
    ) -> None:
        """Register this installation with Halo once. Only a confirmed options flow calls this.

        Halo cannot remove a registration. The intent is saved before the one request,
        and an ambiguous outcome leaves registration needing recovery instead of a retry.
        """
        async with self._lock:
            await self._async_ensure_loaded()
            if not isinstance(self._registration, Unregistered):
                _require_registered(self._registration)
                return
            if not is_current():
                raise HaloControlError(_RUNTIME_CHANGED)
            dispatch_committed = False

            def validate_and_commit_dispatch() -> None:
                nonlocal dispatch_committed
                if not is_current():
                    raise HaloControlError(_RUNTIME_CHANGED)
                dispatch_committed = True

            await async_finish_committed_phase(
                _async_register_and_record(
                    owner=self,
                    client=client,
                    pending=RegistrationPending(uuid.uuid4()),
                    version_string=version_string,
                    pre_dispatch=validate_and_commit_dispatch,
                    dispatched=lambda: dispatch_committed,
                ),
                dispatch_committed=lambda: dispatch_committed,
            )


_RUNTIME_CHANGED = "Halo Collar reloaded before registration, so nothing was sent; try again"


async def _async_dispatch_and_settle(
    *,
    owner: RecallOwner,
    client,
    pet_id: str,
    serial: str,
    command: HaloRecallCommand,
    pre_dispatch: Callable[[], None],
) -> None:
    try:
        reply = await client.async_send_recall_whistle(pet_id, command, pre_dispatch=pre_dispatch)
    except HaloWriteOutcomeUnknown as err:
        raise HaloControlError(
            "Halo Recall whistle outcome is unknown. It was not retried; "
            "wait for the cooldown before trying again"
        ) from err
    if isinstance(reply, HaloRecallAccepted):
        return
    if isinstance(reply, HaloRecallExpired):
        raise HaloControlError(
            "Halo reported that the Recall command expired, so the whistle did not play"
        )
    if reply.current_command_number is not None:
        await owner._async_raise_floor(serial, reply.current_command_number + 1)
    raise HaloControlError(
        "Halo rejected an old Recall command number, so the whistle did not play; "
        "try again after the cooldown"
    )


async def _async_register_and_record(
    *,
    owner: RecallOwner,
    client,
    pending: RegistrationPending,
    version_string: str,
    pre_dispatch: Callable[[], None],
    dispatched: Callable[[], bool],
) -> None:
    try:
        try:
            await owner._async_save(pending)
        except RecallStorageError as err:
            raise HaloControlError(
                "Could not save the registration intent locally, so nothing was sent"
            ) from err
        mobile_id = await client.async_register_recall_device(
            pending.installation_id, version_string=version_string, pre_dispatch=pre_dispatch
        )
    except BaseException:
        if owner.registration == pending:
            outcome = (
                RegistrationUncertain(pending.installation_id) if dispatched() else Unregistered()
            )
            try:
                await owner._async_save(outcome)
            except RecallStorageError:
                pass
        raise
    try:
        await owner._async_save(Registered(mobile_id))
    except RecallStorageError as err:
        raise HaloControlError(
            "Halo accepted the registration, but it could not be saved locally; "
            "see the Recall whistle recovery steps"
        ) from err
