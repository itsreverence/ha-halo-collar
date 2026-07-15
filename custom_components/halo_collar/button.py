from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import HaloApiError
from .const import (
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
    DOMAIN,
)
from .entity import HaloEntity
from .helpers import is_online


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities(
        HaloEnableFencesButton(coordinator, entry, stored["client"], collar)
        for collar in coordinator.data.collars
    )


class HaloEnableFencesButton(HaloEntity, ButtonEntity):
    """Fail-safe, idempotent control that can only enable fence enforcement."""

    _attr_translation_key = "enable_fences"
    _attr_icon = "mdi:shield-check"

    def __init__(self, coordinator, entry, client, collar) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._attr_unique_id = f"{self._collar_id}_enable_fences"

    @property
    def available(self) -> bool:
        collar = self.collar
        pet = self.pet
        stale_after = self._entry.options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS)
        return (
            collar is not None
            and pet is not None
            and is_online(collar, stale_after=float(stale_after))
        )

    async def async_press(self) -> None:
        pet = self.pet
        if pet is None or not pet.get("id"):
            raise HomeAssistantError("Halo pet/collar mapping is unavailable")
        try:
            await self._client.async_set_fences_enabled(pet["id"], enabled=True)
            await self.coordinator.async_request_refresh()
        except HaloApiError as err:
            raise HomeAssistantError(f"Could not enable Halo fences: {err}") from err
