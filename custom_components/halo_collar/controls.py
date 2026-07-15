from __future__ import annotations

from asyncio import Lock
from collections.abc import Callable
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


def control_lock_for(stored: dict[str, Any], collar_id: str) -> Lock:
    """Return the shared per-entry, per-collar transaction lock."""
    locks = stored.setdefault("control_locks", {})
    return locks.setdefault(collar_id, Lock())


def _validate_options(entry, *, enabled: bool) -> None:
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        raise HaloControlError("Halo fence controls are disabled in integration options")
    if not enabled and not entry.options.get(CONF_ALLOW_FENCE_DISABLE, False):
        raise HaloControlError("Disabling Halo fences is not allowed in integration options")


def _is_confirmed(
    state_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    *,
    enabled: bool,
) -> bool:
    pet, _collar = state_getter()
    return (
        pet is not None
        and pet.get("isFencesSynchronized") is True
        and pet_fences_enabled(pet) is enabled
    )


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

        await coordinator.async_request_refresh()
        if not coordinator.last_update_success:
            raise HaloControlError("Could not refresh Halo state before changing fence mode")

        pet, collar = state_getter()
        stale_after = control_stale_after(entry)
        if pet is None or collar is None or not pet.get("id"):
            raise HaloControlError("Halo pet/collar mapping is unavailable")
        if not is_online(collar, stale_after=stale_after):
            raise HaloControlError("Halo collar telemetry is stale")
        if not enabled:
            block_reason = fence_disable_block_reason(pet, collar, stale_after=stale_after)
            if block_reason is not None:
                raise HaloControlError(block_reason)

        # Options can change while the preflight refresh is in flight. Recheck
        # immediately before the one and only network write.
        _validate_options(entry, enabled=enabled)
        try:
            await client.async_set_fences_enabled(pet["id"], enabled=enabled)
        except HaloWriteOutcomeUnknown as err:
            await coordinator.async_request_refresh()
            if coordinator.last_update_success and _is_confirmed(state_getter, enabled=enabled):
                return
            raise HaloControlError(
                "Halo write outcome is unknown and could not be reconciled; "
                "check the official Halo app"
            ) from err

        await coordinator.async_request_refresh()
        if not coordinator.last_update_success:
            raise HaloControlError(
                "Halo command was sent but state confirmation failed; check the official Halo app"
            )
        if not _is_confirmed(state_getter, enabled=enabled):
            raise HaloControlError(
                "Halo command was sent but the collar did not confirm the requested fence state"
            )
