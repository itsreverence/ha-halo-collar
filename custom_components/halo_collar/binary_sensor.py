from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory

from .const import CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS, DOMAIN
from .entity import HaloEntity
from .helpers import (
    active_walk_state,
    firmware_update_available,
    is_online,
    pet_fences_enabled,
    reporting_issue,
)


def _online(collar: dict[str, Any], _pet, entry) -> bool:
    stale_after = entry.options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS)
    return is_online(collar, stale_after=float(stale_after))


@dataclass(frozen=True, kw_only=True)
class HaloBinarySensorDescription(BinarySensorEntityDescription):
    # value_fn receives (collar, pet, config entry).
    value_fn: Callable[[dict[str, Any], dict[str, Any] | None, Any], bool | None]


BINARY_SENSORS = (
    HaloBinarySensorDescription(
        key="online",
        translation_key="online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=_online,
    ),
    HaloBinarySensorDescription(
        key="fence_breach",
        translation_key="fence_breach",
        device_class=BinarySensorDeviceClass.SAFETY,
        value_fn=lambda c, _pet, _entry: c.get("telemetry", {}).get("fenceBreach") is not None,
    ),
    HaloBinarySensorDescription(
        key="gps_calibration_required",
        translation_key="gps_calibration_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c, _pet, _entry: bool(
            c.get("telemetry", {}).get("isGpsCalibrationRequired")
        ),
    ),
    HaloBinarySensorDescription(
        key="compass_calibration_required",
        translation_key="compass_calibration_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda c, _pet, _entry: bool(
            c.get("telemetry", {}).get("isCompassCalibrationRequired")
        ),
    ),
    HaloBinarySensorDescription(
        key="fences_enabled",
        translation_key="fences_enabled",
        value_fn=lambda _c, pet, _entry: pet_fences_enabled(pet),
    ),
    HaloBinarySensorDescription(
        key="fences_synchronized",
        translation_key="fences_synchronized",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda _c, pet, _entry: (
            pet.get("isFencesSynchronized")
            if pet is not None and isinstance(pet.get("isFencesSynchronized"), bool)
            else None
        ),
    ),
    HaloBinarySensorDescription(
        key="active_walk",
        translation_key="active_walk",
        value_fn=lambda c, p, _entry: active_walk_state(p, c),
    ),
    HaloBinarySensorDescription(
        key="collar_reporting_issue",
        translation_key="collar_reporting_issue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p, _entry: reporting_issue(c),
    ),
    HaloBinarySensorDescription(
        key="firmware_update_available",
        translation_key="firmware_update_available",
        device_class=BinarySensorDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p, _entry: firmware_update_available(c),
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
        return self.entity_description.value_fn(collar, self.pet, self._entry)
