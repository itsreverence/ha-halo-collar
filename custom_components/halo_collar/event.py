from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import callback

from .const import DOMAIN
from .entity import HaloEntity

EVENT_FENCE_BREACH = "fence_breach"


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        HaloFenceBreachEvent(coordinator, entry, collar) for collar in coordinator.data.collars
    )


class HaloFenceBreachEvent(HaloEntity, EventEntity):
    """Fires an event when Halo reports a new fence breach, for automation triggers."""

    _attr_translation_key = "fence_breach"
    _attr_event_types = [EVENT_FENCE_BREACH]

    def __init__(self, coordinator, entry, collar) -> None:
        super().__init__(coordinator, entry, collar)
        self._attr_unique_id = f"{self._collar_id}_fence_breach_event"
        self._last_breach = self._current_breach()

    def _current_breach(self) -> Any:
        return ((self.collar or {}).get("telemetry") or {}).get("fenceBreach")

    @callback
    def _handle_coordinator_update(self) -> None:
        breach = self._current_breach()
        if breach is not None and breach != self._last_breach:
            attributes = breach if isinstance(breach, dict) else {"detail": breach}
            self._trigger_event(EVENT_FENCE_BREACH, attributes)
        self._last_breach = breach
        super()._handle_coordinator_update()
