from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError

from .api import HaloApiError
from .const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
    DOMAIN,
)
from .entity import HaloEntity
from .helpers import has_active_walk, is_online, pet_fences_enabled


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False) or not entry.options.get(
        CONF_ALLOW_FENCE_DISABLE, False
    ):
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities(
        HaloFenceModeSwitch(coordinator, entry, stored["client"], collar)
        for collar in coordinator.data.collars
    )


class HaloFenceModeSwitch(HaloEntity, SwitchEntity):
    """Explicitly opted-in fence mode control."""

    _attr_translation_key = "fence_mode"
    _attr_icon = "mdi:fence"

    def __init__(self, coordinator, entry, client, collar) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._attr_unique_id = f"{self._collar_id}_fence_mode"

    @property
    def available(self) -> bool:
        collar = self.collar
        pet = self.pet
        stale_after = self._entry.options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS)
        return (
            collar is not None
            and pet is not None
            and pet_fences_enabled(pet) is not None
            and is_online(collar, stale_after=float(stale_after))
        )

    @property
    def is_on(self) -> bool | None:
        return pet_fences_enabled(self.pet)

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_fences(True)

    async def async_turn_off(self, **kwargs) -> None:
        pet = self.pet
        collar = self.collar
        if has_active_walk(pet, collar):
            raise HomeAssistantError("Halo fences cannot be disabled during an active walk")
        await self._async_set_fences(False)

    async def _async_set_fences(self, enabled: bool) -> None:
        pet = self.pet
        if pet is None or not pet.get("id"):
            raise HomeAssistantError("Halo pet/collar mapping is unavailable")
        try:
            await self._client.async_set_fences_enabled(pet["id"], enabled=enabled)
            await self.coordinator.async_request_refresh()
        except HaloApiError as err:
            action = "enable" if enabled else "disable"
            raise HomeAssistantError(f"Could not {action} Halo fences: {err}") from err
