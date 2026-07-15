from __future__ import annotations

from collections.abc import Callable
from typing import Any

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


async def async_set_fence_mode(
    *,
    coordinator,
    client,
    entry,
    state_getter: Callable[[], tuple[dict[str, Any] | None, dict[str, Any] | None]],
    enabled: bool,
) -> None:
    """Execute one guarded fence-mode transition and confirm reported state."""
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        raise HaloControlError("Halo fence controls are disabled in integration options")
    if not enabled and not entry.options.get(CONF_ALLOW_FENCE_DISABLE, False):
        raise HaloControlError("Disabling Halo fences is not allowed in integration options")

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

    await client.async_set_fences_enabled(pet["id"], enabled=enabled)

    await coordinator.async_request_refresh()
    if not coordinator.last_update_success:
        raise HaloControlError(
            "Halo command was sent but state confirmation failed; check the official Halo app"
        )
    confirmed_pet, _confirmed_collar = state_getter()
    if (
        confirmed_pet is None
        or confirmed_pet.get("isFencesSynchronized") is not True
        or pet_fences_enabled(confirmed_pet) is not enabled
    ):
        raise HaloControlError(
            "Halo command was sent but the collar did not confirm the requested fence state"
        )
