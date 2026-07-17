from __future__ import annotations

from typing import cast

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import HaloApiError, HaloState
from .const import (
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    DOMAIN,
)
from .controls import (
    HaloControlError,
    async_find_collar,
    async_set_fence_mode,
    control_lock_for,
    control_stale_after,
    find_collar_cooldown_remaining,
    find_collar_cooldowns_for,
)
from .entity import HaloEntity
from .helpers import has_active_walk, is_online, subscription_feature_enabled


async def async_setup_entry(hass, entry, async_add_entities):
    fence_controls = entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False)
    find_collar = entry.options.get(CONF_ENABLE_FIND_COLLAR, False)
    if not fence_controls and not find_collar:
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    control_lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)
    entities = []
    if fence_controls:
        entities.extend(
            HaloEnableFencesButton(
                coordinator,
                entry,
                stored["client"],
                collar,
                control_lock,
            )
            for collar in coordinator.data.collars
        )
    if find_collar:
        cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
        entities.extend(
            HaloFindCollarButton(
                coordinator,
                entry,
                stored["client"],
                collar,
                control_lock,
                cooldowns,
            )
            for collar in coordinator.data.collars
        )
    async_add_entities(entities)


class HaloEnableFencesButton(HaloEntity, ButtonEntity):
    """Fail-safe, idempotent control that can only enable fence enforcement."""

    _attr_translation_key = "enable_fences"
    _attr_icon = "mdi:shield-check"

    def __init__(self, coordinator, entry, client, collar, control_lock) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._control_lock = control_lock
        self._attr_unique_id = f"{self._collar_id}_enable_fences"

    @property
    def available(self) -> bool:
        collar = self.collar
        pet = self.pet
        stale_after = control_stale_after(self._entry)
        return (
            collar is not None
            and pet is not None
            and is_online(collar, stale_after=float(stale_after))
        )

    async def async_press(self) -> None:
        try:
            await async_set_fence_mode(
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                control_lock=self._control_lock,
                state_getter=lambda: (self.pet, self.collar),
                enabled=True,
            )
        except (HaloApiError, HaloControlError) as err:
            raise HomeAssistantError(f"Could not enable Halo fences: {err}") from err


class HaloFindCollarButton(HaloEntity, ButtonEntity):
    """Explicitly opted-in physical sound-and-light command."""

    _attr_translation_key = "find_collar"
    _attr_icon = "mdi:bell-ring"

    def __init__(
        self,
        coordinator,
        entry,
        client,
        collar,
        control_lock,
        cooldowns,
    ) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._control_lock = control_lock
        self._cooldowns = cooldowns
        self._attr_unique_id = f"{self._collar_id}_find_collar"

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        collar = self.collar
        pet = self.pet
        state = cast(HaloState, self.coordinator.data)
        return (
            self.coordinator.last_update_success
            and collar is not None
            and pet is not None
            and is_online(collar, stale_after=control_stale_after(self._entry))
            and not has_active_walk(pet, collar)
            and subscription_feature_enabled(state.subscription, "findcollar")
            and find_collar_cooldown_remaining(self._cooldowns, self._collar_id) <= 0
        )

    async def async_press(self) -> None:
        try:
            await async_find_collar(
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                control_lock=self._control_lock,
                cooldowns=self._cooldowns,
                state_getter=lambda: (
                    self.pet,
                    self.collar,
                    cast(HaloState, self.coordinator.data).subscription,
                ),
            )
        except (HaloApiError, HaloControlError) as err:
            raise HomeAssistantError(f"Could not find Halo collar: {err}") from err
