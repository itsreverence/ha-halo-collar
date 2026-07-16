from __future__ import annotations

try:
    from homeassistant.components.device_tracker import TrackerEntity
except ImportError:  # Home Assistant 2024.11 compatibility
    from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType

from .const import DOMAIN
from .entity import HaloEntity
from .helpers import indoors_on_wifi


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        HaloPetTracker(coordinator, entry, collar) for collar in coordinator.data.collars
    )


if hasattr(TrackerEntity, "in_zones"):
    from homeassistant.components.zone import ENTITY_ID_HOME

    class _IndoorHomeTrackerMixin:
        @property
        def in_zones(self) -> list[str] | None:
            """Prefer Home zone membership while Halo reports indoor Wi-Fi."""
            collar = self.collar
            if collar is not None and indoors_on_wifi(collar):
                return [ENTITY_ID_HOME]
            return None

else:
    from homeassistant.const import STATE_HOME

    class _IndoorHomeTrackerMixin:
        @property
        def location_name(self) -> str | None:
            """Use the legacy location API on older supported HA releases."""
            collar = self.collar
            if collar is not None and indoors_on_wifi(collar):
                return STATE_HOME
            return None


class HaloPetTracker(_IndoorHomeTrackerMixin, HaloEntity, TrackerEntity):
    _attr_name = None

    def __init__(self, coordinator, entry, collar) -> None:
        super().__init__(coordinator, entry, collar)
        self._attr_unique_id = f"{self._collar_id}_pet_tracker"

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        pet_info = (self.collar or {}).get("petInfo", {})
        location = pet_info.get("location") or pet_info.get("lastLocation") or {}
        return location.get("latitude")

    @property
    def longitude(self) -> float | None:
        pet_info = (self.collar or {}).get("petInfo", {})
        location = pet_info.get("location") or pet_info.get("lastLocation") or {}
        return location.get("longitude")

    @property
    def location_accuracy(self) -> float | None:
        return ((self.collar or {}).get("petInfo", {}).get("telemetry", {}) or {}).get(
            "gpsAccuracyInMeters"
        )
