from __future__ import annotations

import asyncio
from asyncio import Lock
from collections.abc import Callable, Coroutine
from typing import Any

from .api import HaloWriteOutcomeUnknown
from .const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
)
from .helpers import fence_disable_block_reason, is_online, pet_fences_enabled


class HaloControlError(Exception):
    """Raised when a guarded control cannot be executed or confirmed safely."""


def control_stale_after(entry) -> float:
    """Never let control freshness be looser than the default telemetry threshold."""
    configured = float(entry.options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS))
    return min(configured, float(DEFAULT_STALE_AFTER_SECONDS))


_CONTROL_LOCKS = "_control_locks"


def control_lock_for(domain_data: dict[str, Any], entry_id: str) -> Lock:
    """Return an entry lock whose lifetime spans setup reloads."""
    locks = domain_data.setdefault(_CONTROL_LOCKS, {})
    return locks.setdefault(entry_id, Lock())


def remove_control_lock(domain_data: dict[str, Any], entry_id: str) -> None:
    """Drop a lock only when its config entry is permanently removed."""
    locks = domain_data.get(_CONTROL_LOCKS, {})
    locks.pop(entry_id, None)


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
    stale_after: float,
) -> bool:
    pet, collar = state_getter()
    return (
        pet is not None
        and collar is not None
        and pet.get("id") == pet_id
        and collar.get("id") == collar_id
        and is_online(collar, stale_after=stale_after)
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
    stale_after: float,
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
            stale_after=stale_after,
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
        stale_after=stale_after,
    ):
        raise HaloControlError(
            "Halo command was sent but the collar did not confirm the requested fence state"
        )


async def _async_finish_committed_phase(coro: Coroutine[Any, Any, None]) -> None:
    """Finish dispatch/reconciliation before propagating caller cancellation."""
    task = asyncio.create_task(coro)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
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
        pet, collar = _validate_snapshot(
            state_getter,
            enabled=enabled,
            stale_after=stale_after,
        )
        pet_id = pet["id"]
        collar_id = collar["id"]

        def validate_dispatch_boundary() -> None:
            _validate_options(entry, enabled=enabled)
            _validate_snapshot(
                state_getter,
                enabled=enabled,
                stale_after=stale_after,
                expected_pet_id=pet_id,
                expected_collar_id=collar_id,
            )

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
                stale_after=stale_after,
                pre_dispatch=validate_dispatch_boundary,
            )
        )
