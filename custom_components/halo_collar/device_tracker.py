from __future__ import annotations

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType

from .const import DOMAIN
from .entity import HaloEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        HaloPetTracker(coordinator, entry, collar) for collar in coordinator.data.collars
    )


class HaloPetTracker(HaloEntity, TrackerEntity):
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
