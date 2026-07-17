from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTime,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import HaloState
from .const import DOMAIN
from .entity import HaloEntity
from .helpers import WalkSummary as _WalkSummary
from .helpers import active_walk_distance as _active_walk_distance
from .helpers import active_walk_duration as _active_walk_duration
from .helpers import activity_value as _activity_value
from .helpers import average_connectivity as _average_connectivity
from .helpers import count_goal_progress_attributes as _count_goal_progress_attributes
from .helpers import count_value as _count_value
from .helpers import current_fence_name as _current_fence_name
from .helpers import fence_configuration_status as _fence_configuration_status
from .helpers import goal_progress_attributes as _goal_progress_attributes
from .helpers import last_telemetry as _last_telemetry
from .helpers import latest_completed_walk as _latest_completed_walk
from .helpers import nested as _nested
from .helpers import next_expected_telemetry as _next_expected_telemetry
from .helpers import pet_safety_status as _pet_safety_status
from .helpers import pretty_status as _pretty_status
from .helpers import seconds_to_hours as _seconds_to_hours
from .helpers import telemetry as _telemetry


@dataclass(frozen=True, kw_only=True)
class HaloSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any], dict[str, Any] | None], Any]
    attributes_fn: Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]] | None = None


SENSORS = (
    HaloSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda c, _p: _telemetry(c, "batteryChargePercent"),
    ),
    HaloSensorDescription(
        key="battery_status",
        translation_key="battery_status",
        value_fn=lambda c, _p: _pretty_status(_telemetry(c, "batteryStatus")),
    ),
    HaloSensorDescription(
        key="remaining_battery_lifetime",
        translation_key="remaining_battery_lifetime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda c, _p: _seconds_to_hours(
            _telemetry(c, "remainingBatteryLifetimeInSeconds")
        ),
    ),
    HaloSensorDescription(
        key="current_adapter",
        translation_key="current_adapter",
        value_fn=lambda c, _p: _pretty_status(_telemetry(c, "currentAdapter")),
    ),
    HaloSensorDescription(
        key="wifi_status",
        translation_key="wifi_status",
        value_fn=lambda c, _p: _pretty_status(_nested(c, "telemetry", "wiFi", "status")),
    ),
    HaloSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=lambda c, _p: _nested(c, "telemetry", "wiFi", "signalStrength"),
    ),
    HaloSensorDescription(
        key="cellular_status",
        translation_key="cellular_status",
        value_fn=lambda c, _p: _pretty_status(_nested(c, "telemetry", "cellular", "status")),
    ),
    HaloSensorDescription(
        key="cellular_signal",
        translation_key="cellular_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        value_fn=lambda c, _p: _nested(c, "telemetry", "cellular", "signalStrength"),
    ),
    HaloSensorDescription(
        key="gps_accuracy",
        translation_key="gps_accuracy",
        native_unit_of_measurement=UnitOfLength.METERS,
        suggested_display_precision=1,
        value_fn=lambda c, _p: _nested(c, "petInfo", "telemetry", "gpsAccuracyInMeters"),
    ),
    HaloSensorDescription(
        key="gps_accuracy_status",
        translation_key="location_status",
        value_fn=lambda c, _p: _pretty_status(
            _nested(c, "petInfo", "telemetry", "gpsAccuracyStatus")
        ),
    ),
    HaloSensorDescription(
        key="safety_status",
        translation_key="safety_status",
        value_fn=lambda _c, pet: _pet_safety_status(pet),
    ),
    HaloSensorDescription(
        key="firmware",
        translation_key="firmware",
        value_fn=lambda c, _p: _nested(c, "firmware", "formattedVersion"),
    ),
    HaloSensorDescription(
        key="last_telemetry",
        translation_key="last_telemetry",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c, _p: _last_telemetry(c),
    ),
    HaloSensorDescription(
        key="active_walk_duration",
        translation_key="active_walk_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, p: _active_walk_duration(p, c),
    ),
    HaloSensorDescription(
        key="active_walk_distance",
        translation_key="active_walk_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c, p: _active_walk_distance(p, c),
    ),
    HaloSensorDescription(
        key="activity_duration",
        translation_key="activity_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _c, p: _activity_value(p, "activityDurationInSec"),
        attributes_fn=lambda _c, p: _goal_progress_attributes(
            p, "activityDurationInSec", "activityDurationGoalInSec"
        ),
    ),
    HaloSensorDescription(
        key="outdoor_time",
        translation_key="outdoor_time",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _c, p: _activity_value(p, "outdoorTimeInSec"),
        attributes_fn=lambda _c, p: _goal_progress_attributes(
            p, "outdoorTimeInSec", "outdoorTimeGoalInSec"
        ),
    ),
    HaloSensorDescription(
        key="traveled_distance",
        translation_key="traveled_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda _c, p: _activity_value(p, "traveledDistance"),
        attributes_fn=lambda _c, p: _goal_progress_attributes(
            p, "traveledDistance", "traveledDistanceGoal"
        ),
    ),
    HaloSensorDescription(
        key="walks_today",
        translation_key="walks_today",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda _c, p: _count_value(p, "walksCount"),
        attributes_fn=lambda _c, p: _count_goal_progress_attributes(
            p, "walksCount", "walksCountGoal"
        ),
    ),
    HaloSensorDescription(
        key="current_fence",
        translation_key="current_fence",
        value_fn=lambda _c, p: _current_fence_name(p),
    ),
    HaloSensorDescription(
        key="fence_configuration",
        translation_key="fence_configuration",
        value_fn=lambda _c, p: _fence_configuration_status(p),
    ),
    HaloSensorDescription(
        key="average_connectivity",
        translation_key="average_connectivity",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p: _average_connectivity(c),
    ),
    HaloSensorDescription(
        key="next_expected_telemetry",
        translation_key="next_expected_telemetry",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, _p: _next_expected_telemetry(c),
    ),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[Entity] = [
        HaloSensor(coordinator, entry, collar, description)
        for collar in coordinator.data.collars
        for description in SENSORS
    ]
    entities.extend(
        HaloWalkSensor(coordinator, entry, collar, description)
        for collar in coordinator.data.collars
        for description in WALK_SENSORS
    )
    entities.append(HaloSubscriptionSensor(coordinator, entry))
    async_add_entities(entities)


class HaloSensor(HaloEntity, SensorEntity):
    entity_description: HaloSensorDescription

    def __init__(self, coordinator, entry, collar, description: HaloSensorDescription) -> None:
        super().__init__(coordinator, entry, collar)
        self.entity_description = description
        self._attr_unique_id = f"{self._collar_id}_{description.key}"
        self._update_values()

    def _update_values(self) -> None:
        collar = self.collar
        attributes_fn = self.entity_description.attributes_fn
        self._attr_native_value = (
            self.entity_description.value_fn(collar, self.pet) if collar is not None else None
        )
        self._attr_extra_state_attributes = (
            attributes_fn(collar, self.pet)
            if collar is not None and attributes_fn is not None
            else {}
        )

    def _handle_coordinator_update(self) -> None:
        self._update_values()
        super()._handle_coordinator_update()


WALK_SENSORS = (
    SensorEntityDescription(
        key="last_walk",
        translation_key="last_walk",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="last_walk_duration",
        translation_key="last_walk_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    SensorEntityDescription(
        key="last_walk_distance",
        translation_key="last_walk_distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.METERS,
    ),
    SensorEntityDescription(
        key="last_walk_average_speed",
        translation_key="last_walk_average_speed",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_display_precision=2,
    ),
)


class HaloWalkSensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    HaloEntity, SensorEntity
):
    """Expose a privacy-safe summary of the latest completed cloud walk."""

    entity_description: SensorEntityDescription

    def __init__(self, coordinator, entry, collar, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, entry, collar)
        self.entity_description = description  # pyright: ignore[reportIncompatibleVariableOverride]
        self._attr_unique_id = f"{self._collar_id}_{description.key}"
        self._update_values()

    def _walk_summary(self) -> _WalkSummary | None:
        pet = self.pet
        pet_id = pet.get("id") if pet is not None else None
        state = cast(HaloState, self.coordinator.data)
        return _latest_completed_walk(state.walks, pet_id)

    def _update_values(self) -> None:
        summary = self._walk_summary()
        if summary is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        key = self.entity_description.key
        if key == "last_walk":
            self._attr_native_value = summary["ended_at"]
            attributes: dict[str, str] = {}
            if summary["started_at"] is not None:
                attributes["started_at"] = summary["started_at"].isoformat()
            if summary["start_trigger"] is not None:
                attributes["start_trigger"] = summary["start_trigger"]
            self._attr_extra_state_attributes = attributes
        elif key == "last_walk_duration":
            self._attr_native_value = summary["duration"]
            self._attr_extra_state_attributes = {}
        elif key == "last_walk_distance":
            self._attr_native_value = summary["distance"]
            self._attr_extra_state_attributes = {}
        elif key == "last_walk_average_speed":
            duration = summary["duration"]
            distance = summary["distance"]
            self._attr_native_value = (
                distance / duration
                if duration is not None and duration > 0 and distance is not None
                else None
            )
            self._attr_extra_state_attributes = {}
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}

    def _handle_coordinator_update(self) -> None:
        self._update_values()
        super()._handle_coordinator_update()


class HaloSubscriptionSensor(  # pyright: ignore[reportIncompatibleVariableOverride]
    CoordinatorEntity, SensorEntity
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "subscription"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_subscription"
        self._update_values()

    @property
    def suggested_object_id(self) -> str:
        """Avoid a generic account-level ``sensor.subscription`` entity ID."""
        return "halo_subscription"

    def _update_values(self) -> None:
        state = cast(HaloState, self.coordinator.data)
        subscription = state.subscription
        if not isinstance(subscription, dict):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        access_level = _pretty_status(subscription.get("accessLevel"))
        self._attr_native_value = access_level if isinstance(access_level, str) else None
        attributes: dict[str, Any] = {}
        usage_allowed = subscription.get("isApplicationUsageAllowed")
        if isinstance(usage_allowed, bool):
            attributes["application_usage_allowed"] = usage_allowed
        for source, target in (
            ("maxCollarsCount", "max_collars"),
            ("maxGeoFencesCount", "max_fences"),
        ):
            value = _count_value({"metrics": subscription}, source)
            if value is not None:
                attributes[target] = value
        self._attr_extra_state_attributes = attributes

    def _handle_coordinator_update(self) -> None:
        self._update_values()
        super()._handle_coordinator_update()
