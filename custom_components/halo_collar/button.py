from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import HaloApiError
from .const import CONF_ENABLE_FENCE_CONTROLS, DOMAIN
from .controls import (
    HaloControlError,
    async_set_fence_mode,
    control_lock_for,
    control_stale_after,
)
from .entity import HaloEntity
from .helpers import is_online


async def async_setup_entry(hass, entry, async_add_entities):
    if not entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    async_add_entities(
        HaloEnableFencesButton(
            coordinator,
            entry,
            stored["client"],
            collar,
            control_lock_for(stored),
        )
        for collar in coordinator.data.collars
    )


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
