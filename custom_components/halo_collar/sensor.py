from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import PERCENTAGE, UnitOfTime

from .const import DOMAIN
from .entity import HaloEntity
from .helpers import last_telemetry as _last_telemetry
from .helpers import nested as _nested
from .helpers import pretty_status as _pretty_status
from .helpers import seconds_to_hours as _seconds_to_hours
from .helpers import telemetry as _telemetry


@dataclass(frozen=True, kw_only=True)
class HaloSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


SENSORS = (
    HaloSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda c: _telemetry(c, "batteryChargePercent"),
    ),
    HaloSensorDescription(
        key="battery_status",
        translation_key="battery_status",
        value_fn=lambda c: _pretty_status(_telemetry(c, "batteryStatus")),
    ),
    HaloSensorDescription(
        key="remaining_battery_lifetime",
        translation_key="remaining_battery_lifetime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda c: _seconds_to_hours(_telemetry(c, "remainingBatteryLifetimeInSeconds")),
    ),
    HaloSensorDescription(
        key="current_adapter",
        translation_key="current_adapter",
        value_fn=lambda c: _pretty_status(_telemetry(c, "currentAdapter")),
    ),
    HaloSensorDescription(
        key="wifi_status",
        translation_key="wifi_status",
        value_fn=lambda c: _pretty_status(_nested(c, "telemetry", "wiFi", "status")),
    ),
    HaloSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=lambda c: _nested(c, "telemetry", "wiFi", "signalStrength"),
    ),
    HaloSensorDescription(
        key="cellular_status",
        translation_key="cellular_status",
        value_fn=lambda c: _pretty_status(_nested(c, "telemetry", "cellular", "status")),
    ),
    HaloSensorDescription(
        key="cellular_signal",
        translation_key="cellular_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=lambda c: _nested(c, "telemetry", "cellular", "signalStrength"),
    ),
    HaloSensorDescription(
        key="gps_accuracy",
        translation_key="gps_accuracy",
        native_unit_of_measurement="m",
        suggested_display_precision=1,
        value_fn=lambda c: _nested(c, "petInfo", "telemetry", "gpsAccuracyInMeters"),
    ),
    HaloSensorDescription(
        key="gps_accuracy_status",
        translation_key="location_status",
        value_fn=lambda c: _pretty_status(_nested(c, "petInfo", "telemetry", "gpsAccuracyStatus")),
    ),
    HaloSensorDescription(
        key="safety_status",
        translation_key="safety_status",
        value_fn=lambda c: _pretty_status(_nested(c, "petInfo", "safetyStatus")),
    ),
    HaloSensorDescription(
        key="firmware",
        translation_key="firmware",
        value_fn=lambda c: _nested(c, "firmware", "formattedVersion"),
    ),
    HaloSensorDescription(
        key="last_telemetry",
        translation_key="last_telemetry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_telemetry,
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = [
        HaloSensor(coordinator, entry, collar, description)
        for collar in coordinator.data.collars
        for description in SENSORS
    ]
    async_add_entities(entities)


class HaloSensor(HaloEntity, SensorEntity):
    entity_description: HaloSensorDescription

    def __init__(self, coordinator, entry, collar, description: HaloSensorDescription) -> None:
        super().__init__(coordinator, entry, collar)
        self.entity_description = description
        self._attr_unique_id = f"{self._collar_id}_{description.key}"

    @property
    def native_value(self):
        collar = self.collar
        if collar is None:
            return None
        return self.entity_description.value_fn(collar)
