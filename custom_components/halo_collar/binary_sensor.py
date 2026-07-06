from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .const import DOMAIN
from .entity import HaloEntity
from .helpers import is_online


@dataclass(frozen=True, kw_only=True)
class HaloBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], bool]


BINARY_SENSORS = (
    HaloBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=is_online,
    ),
    HaloBinarySensorDescription(
        key="fence_breach",
        translation_key="fence_breach",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda c: c.get("telemetry", {}).get("fenceBreach") is not None,
    ),
    HaloBinarySensorDescription(
        key="gps_calibration_required",
        translation_key="gps_calibration_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c: bool(c.get("telemetry", {}).get("isGpsCalibrationRequired")),
    ),
    HaloBinarySensorDescription(
        key="compass_calibration_required",
        translation_key="compass_calibration_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c: bool(c.get("telemetry", {}).get("isCompassCalibrationRequired")),
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        HaloBinarySensor(coordinator, entry, collar, description)
        for collar in coordinator.data.collars
        for description in BINARY_SENSORS
    )


class HaloBinarySensor(HaloEntity, BinarySensorEntity):
    entity_description: HaloBinarySensorDescription

    def __init__(
        self,
        coordinator,
        entry,
        collar,
        description: HaloBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry, collar)
        self.entity_description = description
        self._attr_unique_id = f"{self._collar_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        collar = self.collar
        if collar is None:
            return None
        return self.entity_description.value_fn(collar)
