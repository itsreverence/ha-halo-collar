from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .api import HaloApiError
from .const import CONF_ALLOW_FENCE_DISABLE, CONF_ENABLE_FENCE_CONTROLS, DOMAIN
from .controls import (
    HaloControlError,
    async_set_fence_mode,
    control_lock_for,
    control_stale_after,
)
from .entity import HaloEntity
from .helpers import is_online, pet_fences_enabled


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False) or not entry.options.get(
        CONF_ALLOW_FENCE_DISABLE, False
    ):
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities(
        HaloFenceModeSwitch(
            coordinator,
            entry,
            stored["client"],
            collar,
            control_lock_for(hass.data[DOMAIN], entry.entry_id),
        )
        for collar in coordinator.data.collars
    )


class HaloFenceModeSwitch(HaloEntity, SwitchEntity):
    """Explicitly opted-in fence mode control."""

    _attr_translation_key = "fence_mode"
    _attr_icon = "mdi:fence"

    def __init__(self, coordinator, entry, client, collar, control_lock) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._control_lock = control_lock
        self._attr_unique_id = f"{self._collar_id}_fence_mode"

    @property
    def available(self) -> bool:
        collar = self.collar
        pet = self.pet
        return (
            collar is not None
            and pet is not None
            and pet_fences_enabled(pet) is not None
            and pet.get("isFencesSynchronized") is True
            and is_online(collar, stale_after=control_stale_after(self._entry))
        )

    @property
    def is_on(self) -> bool | None:
        return pet_fences_enabled(self.pet)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_fences(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_fences(False)

    async def _async_set_fences(self, enabled: bool) -> None:
        try:
            await async_set_fence_mode(
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                control_lock=self._control_lock,
                state_getter=lambda: (self.pet, self.collar),
                enabled=enabled,
            )
        except (HaloApiError, HaloControlError) as err:
            action = "enable" if enabled else "disable"
            raise HomeAssistantError(f"Could not {action} Halo fences: {err}") from err
