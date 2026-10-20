from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from custom_components.halo_collar.api import HaloState
from custom_components.halo_collar.helpers import reported_activity_options
from custom_components.halo_collar.sensor import SENSORS, WALK_SENSORS, HaloSensor, HaloWalkSensor


async def test_reported_activity_and_every_walk_sensor_write_and_refresh_real_ha_states(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    if hasattr(device_registry, "async_setup"):
        device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    state = HaloState(
        pets=[
            {
                "id": "pet-1",
                "collarInfo": {"id": "collar-1"},
                "telemetry": {"activityType": 5},
            }
        ],
        collars=[
            {
                "id": "collar-1",
                "petInfo": {"id": "pet-1", "telemetry": {"activityType": 9}},
                "telemetry": {
                    "activityType": 9,
                    "manifest": {"timestamp": "2020-01-01T00:00:00Z"},
                },
            }
        ],
        subscription={},
        server_time=None,
        walks=[
            {
                "startedAt": "2026-07-06T11:00:00Z",
                "endedAt": "2026-07-06T11:10:00Z",
                "startTrigger": "button",
                "pets": [
                    {
                        "id": "pet-1",
                        "walkedDurationInSeconds": 600,
                        "walkedDistanceInMeters": 1200,
                        "feedbacksCount": 0,
                    }
                ],
            }
        ],
    )
    failure = False

    async def fetch():
        if failure:
            raise UpdateFailed("synthetic required refresh failure")
        return state

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), config_entry=None, name="synthetic", update_method=fetch
    )
    coordinator.async_set_updated_data(state)
    entry = SimpleNamespace(entry_id="entry-1", options={})
    activity_description = next(d for d in SENSORS if d.key == "last_reported_activity")
    activity = HaloSensor(coordinator, entry, state.collars[0], activity_description)
    walk_sensors = {
        d.key: HaloWalkSensor(coordinator, entry, state.collars[0], d) for d in WALK_SENSORS
    }
    entities = [activity, *walk_sensors.values()]
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await component.async_setup({})
    await component.async_add_entities(entities)

    async def refresh():
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    def written(entity):
        result = hass.states.get(entity.entity_id)
        assert result is not None
        return result

    try:
        assert activity.unique_id == "collar-1_last_reported_activity"
        assert written(activity).state == "eating_drinking"
        assert written(activity).attributes["device_class"] == "enum"
        assert written(activity).attributes["options"] == reported_activity_options()
        expected = {
            "last_walk": "2026-07-06T11:10:00+00:00",
            "last_walk_duration": "600",
            "last_walk_distance": "1200.0",
            "last_walk_average_speed": "2.0",
            "last_walk_feedback_count": "0",
        }
        assert set(expected) == set(walk_sensors)
        for key, sensor in walk_sensors.items():
            assert sensor.unique_id == f"collar-1_{key}"
            assert written(sensor).state == expected[key]
        assert written(walk_sensors["last_walk"]).attributes["started_at"] == (
            datetime(2026, 7, 6, 11, tzinfo=UTC).isoformat()
        )
        assert written(walk_sensors["last_walk"]).attributes["start_trigger"] == "Button"
        count = walk_sensors["last_walk_feedback_count"]
        for entity in (activity, count):
            assert entity.native_unit_of_measurement is None
            assert entity.state_class is None
            assert entity.extra_state_attributes == {}
        assert count.device_class is None

        state.pets[0]["telemetry"]["activityType"] = 4
        state.walks[0]["pets"][0]["feedbacksCount"] = "3"
        await refresh()
        assert written(activity).state == "running"
        assert written(count).state == "3"

        failure = True
        await refresh()
        for entity in entities:
            assert written(entity).state == "unavailable"
        failure = False
        await refresh()
        assert written(activity).state == "running"
        assert written(count).state == "3"

        state.pets[0]["telemetry"] = {}
        state.walks[0]["pets"][0].pop("feedbacksCount")
        await refresh()
        assert written(activity).state == "unknown"
        assert written(count).state == "unknown"
        assert written(walk_sensors["last_walk_duration"]).state == "600"

        state.pets[0]["telemetry"]["activityType"] = 1
        state.walks[0]["pets"][0]["feedbacksCount"] = 8
        valid_state = deepcopy(state)
        state.pets.append(deepcopy(state.pets[0]))
        await refresh()
        for entity in entities:
            assert written(entity).state == "unknown"

        state = valid_state
        state.walks = []
        await refresh()
        assert written(activity).state == "walking"
        for entity in walk_sensors.values():
            assert written(entity).state == "unknown"

        state.collars = []
        await refresh()
        for entity in entities:
            assert written(entity).state == "unavailable"
    finally:
        await component._async_reset()
        await coordinator.async_shutdown()
        await hass.async_stop(force=True)
