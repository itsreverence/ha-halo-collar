from __future__ import annotations

import asyncio
import time
from asyncio import Lock
from collections.abc import Callable, Coroutine
from typing import Any

from .api import HaloCollarNotFound, HaloWriteOutcomeUnknown
from .const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
    FIND_COLLAR_COOLDOWN_SECONDS,
)
from .helpers import (
    fence_disable_block_reason,
    has_active_walk,
    is_online,
    nested,
    pet_fences_enabled,
    subscription_feature_enabled,
)


class HaloControlError(Exception):
    """Raised when a guarded control cannot be executed or confirmed safely."""


def control_stale_after(entry) -> float:
    """Never let control freshness be looser than the default telemetry threshold."""
    configured = float(entry.options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS))
    return min(configured, float(DEFAULT_STALE_AFTER_SECONDS))


_CONTROL_LOCKS = "_control_locks"
_FIND_COLLAR_COOLDOWNS = "_find_collar_cooldowns"


def control_lock_for(domain_data: dict[str, Any], entry_id: str) -> Lock:
    """Return an entry lock whose lifetime spans setup reloads."""
    locks = domain_data.setdefault(_CONTROL_LOCKS, {})
    return locks.setdefault(entry_id, Lock())


def find_collar_cooldowns_for(domain_data: dict[str, Any], entry_id: str) -> dict[str, float]:
    """Return reload-stable dispatch timestamps for one config entry."""
    entries = domain_data.setdefault(_FIND_COLLAR_COOLDOWNS, {})
    return entries.setdefault(entry_id, {})


def find_collar_cooldown_remaining(cooldowns: dict[str, float], collar_id: str) -> float:
    """Return seconds remaining in the conservative post-dispatch cooldown."""
    dispatched_at = cooldowns.get(collar_id)
    if dispatched_at is None:
        return 0.0
    return max(0.0, dispatched_at + FIND_COLLAR_COOLDOWN_SECONDS - time.monotonic())


def remove_control_lock(domain_data: dict[str, Any], entry_id: str) -> None:
    """Drop control runtime state only when its config entry is permanently removed."""
    locks = domain_data.get(_CONTROL_LOCKS, {})
    locks.pop(entry_id, None)
    cooldown_entries = domain_data.get(_FIND_COLLAR_COOLDOWNS, {})
    cooldown_entries.pop(entry_id, None)


def _validate_options(entry, *, enabled: bool) -> None:
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        raise HaloControlError("Halo fence controls are disabled in integration options")
    if not enabled and not entry.options.get(CONF_ALLOW_FENCE_DISABLE, False):
        raise HaloControlError("Disabling Halo fences is not allowed in integration options")


def _is_confirmed(
    state_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    *,
    enabled: bool,
    pet_id: str,
    collar_id: str,
    stale_after_getter: Callable[[], float],
) -> bool:
    pet, collar = state_getter()
    return (
        pet is not None
        and collar is not None
        and pet.get("id") == pet_id
        and collar.get("id") == collar_id
        and is_online(collar, stale_after=stale_after_getter())
        and pet.get("isFencesSynchronized") is True
        and pet_fences_enabled(pet) is enabled
    )


def _validate_snapshot(
    state_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    *,
    enabled: bool,
    stale_after: float,
    expected_pet_id: str | None = None,
    expected_collar_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed unless the current snapshot is safe for this exact target."""
    pet, collar = state_getter()
    if pet is None or collar is None or not pet.get("id") or not collar.get("id"):
        raise HaloControlError("Halo pet/collar mapping is unavailable")
    if expected_pet_id is not None and pet.get("id") != expected_pet_id:
        raise HaloControlError("Halo pet/collar mapping changed before command dispatch")
    if expected_collar_id is not None and collar.get("id") != expected_collar_id:
        raise HaloControlError("Halo pet/collar mapping changed before command dispatch")
    if not is_online(collar, stale_after=stale_after):
        raise HaloControlError("Halo collar telemetry is stale")
    if not enabled:
        block_reason = fence_disable_block_reason(pet, collar, stale_after=stale_after)
        if block_reason is not None:
            raise HaloControlError(block_reason)
    return pet, collar


async def _async_dispatch_and_reconcile(
    *,
    coordinator,
    client,
    pet_id: str,
    collar_id: str,
    state_getter,
    enabled: bool,
    stale_after_getter: Callable[[], float],
    pre_dispatch,
) -> None:
    try:
        await client.async_set_fences_enabled(
            pet_id,
            enabled=enabled,
            pre_dispatch=pre_dispatch,
        )
    except HaloWriteOutcomeUnknown as err:
        await coordinator.async_refresh()
        if coordinator.last_update_success and _is_confirmed(
            state_getter,
            enabled=enabled,
            pet_id=pet_id,
            collar_id=collar_id,
            stale_after_getter=stale_after_getter,
        ):
            return
        raise HaloControlError(
            "Halo write outcome is unknown and could not be reconciled; check the official Halo app"
        ) from err

    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise HaloControlError(
            "Halo command was sent but state confirmation failed; check the official Halo app"
        )
    if not _is_confirmed(
        state_getter,
        enabled=enabled,
        pet_id=pet_id,
        collar_id=collar_id,
        stale_after_getter=stale_after_getter,
    ):
        raise HaloControlError(
            "Halo command was sent but the collar did not confirm the requested fence state"
        )


async def _async_finish_committed_phase(
    coro: Coroutine[Any, Any, None],
    *,
    dispatch_committed: Callable[[], bool],
) -> None:
    """Cancel safely before dispatch, or finish a committed transaction."""
    task = asyncio.create_task(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if not dispatch_committed():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise
            if task.done():
                break
        except Exception as err:
            if cancelled:
                raise asyncio.CancelledError from err
            raise

    if cancelled:
        try:
            task.result()
        except Exception as err:
            raise asyncio.CancelledError from err
        raise asyncio.CancelledError
    task.result()


async def async_set_fence_mode(
    *,
    coordinator,
    client,
    entry,
    control_lock: Lock,
    state_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    enabled: bool,
) -> None:
    """Execute one serialized fence transition and confirm reported state."""
    async with control_lock:
        _validate_options(entry, enabled=enabled)

        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise HaloControlError("Could not refresh Halo state before changing fence mode")

        stale_after = control_stale_after(entry)
        strictest_stale_after = stale_after

        def current_strictest_stale_after() -> float:
            nonlocal strictest_stale_after
            strictest_stale_after = min(
                strictest_stale_after,
                control_stale_after(entry),
            )
            return strictest_stale_after

        pet, collar = _validate_snapshot(
            state_getter,
            enabled=enabled,
            stale_after=stale_after,
        )
        pet_id = pet["id"]
        collar_id = collar["id"]
        dispatch_committed = False

        def validate_dispatch_boundary() -> None:
            nonlocal dispatch_committed
            _validate_options(entry, enabled=enabled)
            _validate_snapshot(
                state_getter,
                enabled=enabled,
                stale_after=current_strictest_stale_after(),
                expected_pet_id=pet_id,
                expected_collar_id=collar_id,
            )
            dispatch_committed = True

        # Options and live state can change while preflight or OAuth refresh is
        # in flight. Recheck the exact target and all safety gates immediately
        # before every transport dispatch.
        _validate_options(entry, enabled=enabled)
        await _async_finish_committed_phase(
            _async_dispatch_and_reconcile(
                coordinator=coordinator,
                client=client,
                pet_id=pet_id,
                collar_id=collar_id,
                state_getter=state_getter,
                enabled=enabled,
                stale_after_getter=current_strictest_stale_after,
                pre_dispatch=validate_dispatch_boundary,
            ),
            dispatch_committed=lambda: dispatch_committed,
        )


def _validate_find_options(entry) -> None:
    if not entry.options.get(CONF_ENABLE_FIND_COLLAR, False):
        raise HaloControlError("Halo Find Collar is disabled in integration options")


def _ensure_find_cooldown_clear(cooldowns: dict[str, float], collar_id: str) -> None:
    remaining = find_collar_cooldown_remaining(cooldowns, collar_id)
    if remaining > 0:
        raise HaloControlError(f"Halo Find Collar cooldown has {remaining:.0f} seconds remaining")


def _validate_find_snapshot(
    state_getter: Callable[
        [], tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]
    ],
    *,
    stale_after: float,
    expected_pet_id: str | None = None,
    expected_collar_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail closed unless Find Collar is safe for one exact current mapping."""
    pet, collar, subscription = state_getter()
    if pet is None or collar is None:
        raise HaloControlError("Halo pet/collar mapping is unavailable")
    pet_id = pet.get("id")
    collar_id = collar.get("id")
    if not isinstance(pet_id, str) or not pet_id or not isinstance(collar_id, str) or not collar_id:
        raise HaloControlError("Halo pet/collar mapping is unavailable")
    if nested(collar, "petInfo", "id") != pet_id or nested(pet, "collarInfo", "id") != collar_id:
        raise HaloControlError("Halo pet/collar mapping is unavailable")
    if expected_pet_id is not None and pet_id != expected_pet_id:
        raise HaloControlError("Halo pet/collar mapping changed before command dispatch")
    if expected_collar_id is not None and collar_id != expected_collar_id:
        raise HaloControlError("Halo pet/collar mapping changed before command dispatch")
    if not is_online(collar, stale_after=stale_after):
        raise HaloControlError("Halo collar telemetry is stale")
    if has_active_walk(pet, collar):
        raise HaloControlError("Find Collar is blocked during an active or unknown walk")
    if not subscription_feature_enabled(subscription, "findcollar"):
        raise HaloControlError("Halo subscription does not enable Find Collar")
    return pet, collar


async def _async_dispatch_find_and_refresh(
    *,
    coordinator,
    client,
    collar_id: str,
    pre_dispatch,
) -> None:
    try:
        await client.async_find_collar(collar_id, pre_dispatch=pre_dispatch)
    except HaloCollarNotFound as err:
        await coordinator.async_refresh()
        raise HaloControlError(
            "Halo collar was not found; the physical command was not repeated"
        ) from err
    except HaloWriteOutcomeUnknown as err:
        await coordinator.async_refresh()
        raise HaloControlError(
            "Halo Find Collar outcome is unknown; do not retry until the cooldown expires"
        ) from err

    # Halo exposes no durable sound/light confirmation field. Refresh only to
    # restore the newest telemetry while retaining the provider's success result.
    await coordinator.async_refresh()


async def async_find_collar(
    *,
    coordinator,
    client,
    entry,
    control_lock: Lock,
    cooldowns: dict[str, float],
    state_getter: Callable[
        [], tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]
    ],
) -> None:
    """Execute one serialized, at-most-once Return Whistle transaction."""
    async with control_lock:
        _validate_find_options(entry)

        # A stale pre-refresh snapshot is never authorization, but it can safely
        # reject a repeat without another provider call.
        _pet, existing_collar, _subscription = state_getter()
        if existing_collar is not None and isinstance(existing_collar.get("id"), str):
            _ensure_find_cooldown_clear(cooldowns, existing_collar["id"])

        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise HaloControlError("Could not refresh Halo state before finding the collar")

        strictest_stale_after = control_stale_after(entry)

        def current_strictest_stale_after() -> float:
            nonlocal strictest_stale_after
            strictest_stale_after = min(
                strictest_stale_after,
                control_stale_after(entry),
            )
            return strictest_stale_after

        pet, collar = _validate_find_snapshot(
            state_getter,
            stale_after=strictest_stale_after,
        )
        pet_id = pet["id"]
        collar_id = collar["id"]
        _ensure_find_cooldown_clear(cooldowns, collar_id)
        dispatch_committed = False

        def validate_and_commit_dispatch() -> None:
            nonlocal dispatch_committed
            _validate_find_options(entry)
            _validate_find_snapshot(
                state_getter,
                stale_after=current_strictest_stale_after(),
                expected_pet_id=pet_id,
                expected_collar_id=collar_id,
            )
            _ensure_find_cooldown_clear(cooldowns, collar_id)
            # Start the cooldown before transport dispatch. Even if transport
            # fails immediately, the collar may have received the command.
            cooldowns[collar_id] = time.monotonic()
            dispatch_committed = True

        await _async_finish_committed_phase(
            _async_dispatch_find_and_refresh(
                coordinator=coordinator,
                client=client,
                collar_id=collar_id,
                pre_dispatch=validate_and_commit_dispatch,
            ),
            dispatch_committed=lambda: dispatch_committed,
        )
