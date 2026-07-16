from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_collar.api import HaloApiError, HaloAuthError, HaloState
from custom_components.halo_collar.const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    DOMAIN,
)
from custom_components.halo_collar.controls import control_lock_for


class FakeHaloClient:
    def __init__(self, state):
        self.state = state
        self.fetches = 0
        self.writes = []
        self._snapshot = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 4_102_444_800.0,
        }

    @property
    def token_snapshot(self):
        return dict(self._snapshot)

    async def async_fetch_state(self):
        self.fetches += 1
        return self.state

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        return {"ok": True}


class FailingHaloClient(FakeHaloClient):
    def __init__(self, state, error):
        super().__init__(state)
        self.error = error

    async def async_fetch_state(self):
        self.fetches += 1
        raise self.error


def _state(*, indoors: bool = False):
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    pet = {
        "id": "pet-1",
        "name": "Cowboy",
        "collarInfo": {"id": "collar-1"},
        "isFencesSynchronized": True,
        "fencesState": "upToDate",
        "metrics": {
            "activityDurationInSec": 900,
            "activityDurationGoalInSec": 1200,
            "outdoorTimeInSec": 300,
            "outdoorTimeGoalInSec": 600,
            "traveledDistance": 1400,
            "traveledDistanceGoal": 2000,
            "walksCount": 1,
            "walksCountGoal": 2,
        },
        "telemetry": {
            "mode": {"fencesOn": True},
            "walk": None,
            "safetyStatus": "safe",
            "geoFence": {"name": "Synthetic Fence", "id": "excluded"},
        },
    }
    collar = {
        "id": "collar-1",
        "type": "Halo 4",
        "serialNumber": "serial-1",
        "diagnostics": {"averageConnectivityPercent": 87.5},
        "issues": {"hasReportingIssue": True},
        "hasFirmwareUpdatesAvailable": False,
        "petInfo": {
            "id": "pet-1",
            "name": "Cowboy",
            "location": {"latitude": 42.0, "longitude": -83.0},
            "telemetry": {
                "gpsAccuracyInMeters": 5.0,
                "gpsAccuracyStatus": "indoors" if indoors else "outdoors",
            },
        },
        "telemetry": {
            "manifest": {"timestamp": timestamp},
            "mode": {"fencesOn": True},
            "walk": None,
            "secondsToNextTelemetry": 300,
            "batteryChargePercent": 90,
            "currentAdapter": "wifi",
        },
    }
    return HaloState(
        pets=[pet],
        collars=[collar],
        subscription={
            "accessLevel": "basic",
            "isApplicationUsageAllowed": True,
            "maxCollarsCount": 2,
            "maxGeoFencesCount": 20,
        },
        server_time=timestamp,
    )


def _entry(options):
    return MockConfigEntry(
        domain=DOMAIN,
        title="Halo Collar",
        unique_id="owner@example.com",
        data={
            CONF_ACCESS_TOKEN: "access",
            CONF_REFRESH_TOKEN: "refresh",
            CONF_EXPIRES_AT: 4_102_444_800.0,
            CONF_CLIENT_ID: "halo.app.android",
            CONF_CLIENT_SECRET: "public-client-secret",
        },
        options={
            CONF_SCAN_INTERVAL: 300,
            CONF_STALE_AFTER: 900,
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ALLOW_FENCE_DISABLE: False,
            **options,
        },
    )


async def _setup(hass, entry, client):
    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        return_value=client,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _state_for_unique_id(hass, platform: str, unique_id: str):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_runtime_setup_registers_platforms_and_guarded_control_surface(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    assert entry.state is ConfigEntryState.LOADED
    assert client.fetches == 1
    assert hass.states.get("button.cowboy_enable_fences") is not None
    assert hass.states.get("switch.cowboy_fence_mode") is None
    assert hass.states.get("device_tracker.cowboy") is not None
    assert hass.states.get("event.cowboy_fence_breach_event") is not None
    assert hass.states.get("binary_sensor.cowboy_fences_enabled").state == STATE_ON
    assert hass.states.get("binary_sensor.cowboy_fence_state_synchronized").state == STATE_ON

    collar_id = client.state.collars[0]["id"]
    activity = _state_for_unique_id(hass, "sensor", f"{collar_id}_activity_duration")
    assert float(activity.state) == 900
    assert activity.attributes["goal"] == 1200.0
    assert activity.attributes["progress_percent"] == 75.0
    outdoor_time = _state_for_unique_id(hass, "sensor", f"{collar_id}_outdoor_time")
    assert float(outdoor_time.state) == 300
    assert outdoor_time.attributes["goal"] == 600.0
    assert outdoor_time.attributes["progress_percent"] == 50.0
    distance = _state_for_unique_id(hass, "sensor", f"{collar_id}_traveled_distance")
    assert float(distance.state) == 1400
    assert distance.attributes["goal"] == 2000.0
    assert distance.attributes["progress_percent"] == 70.0
    walks_today = _state_for_unique_id(hass, "sensor", f"{collar_id}_walks_today")
    assert int(walks_today.state) == 1
    assert walks_today.attributes["goal"] == 2
    assert walks_today.attributes["progress_percent"] == 50.0
    fence = _state_for_unique_id(hass, "sensor", f"{collar_id}_current_fence")
    assert fence.state == "Synthetic Fence"
    assert "id" not in fence.attributes
    fence_configuration = _state_for_unique_id(hass, "sensor", f"{collar_id}_fence_configuration")
    assert fence_configuration.state == "Up to date"
    assert _state_for_unique_id(hass, "sensor", f"{collar_id}_average_connectivity").state == "87.5"
    last_report = datetime.fromisoformat(
        client.state.collars[0]["telemetry"]["manifest"]["timestamp"]
    )
    next_telemetry = _state_for_unique_id(hass, "sensor", f"{collar_id}_next_expected_telemetry")
    assert next_telemetry.state == (last_report + timedelta(seconds=300)).isoformat()
    assert _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_active_walk").state == "off"
    assert (
        _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_collar_reporting_issue").state
        == STATE_ON
    )
    assert (
        _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_firmware_update_available").state
        == "off"
    )

    subscription = _state_for_unique_id(hass, "sensor", f"{entry.entry_id}_subscription")
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_subscription")
        == "sensor.halo_subscription"
    )
    assert subscription.state == "Basic"
    assert subscription.attributes["application_usage_allowed"] is True
    assert subscription.attributes["max_collars"] == 2
    assert subscription.attributes["max_fences"] == 20


async def test_runtime_insight_sensor_updates_after_coordinator_refresh(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    client.state.pets[0]["metrics"].update({"outdoorTimeInSec": 480, "outdoorTimeGoalInSec": 960})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    outdoor_time = _state_for_unique_id(hass, "sensor", f"{collar_id}_outdoor_time")
    assert float(outdoor_time.state) == 480
    assert outdoor_time.attributes["goal"] == 960.0
    assert outdoor_time.attributes["progress_percent"] == 50.0


async def test_runtime_active_walk_entity_is_unknown_when_walk_fields_are_missing(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    client.state.pets[0]["telemetry"].pop("walk")
    client.state.collars[0]["telemetry"].pop("walk")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    active_walk = _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_active_walk")
    assert active_walk.state == STATE_UNKNOWN


async def test_runtime_subscription_entity_fails_soft_on_empty_and_malformed_payloads(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    client.state.subscription = {}
    await coordinator.async_refresh()
    empty = _state_for_unique_id(hass, "sensor", f"{entry.entry_id}_subscription")
    assert empty.state == STATE_UNKNOWN
    assert "application_usage_allowed" not in empty.attributes
    assert "max_collars" not in empty.attributes
    assert "max_fences" not in empty.attributes

    client.state.subscription = {
        "accessLevel": {},
        "isApplicationUsageAllowed": "yes",
        "maxCollarsCount": True,
        "maxGeoFencesCount": 1.5,
    }
    await coordinator.async_refresh()
    malformed = _state_for_unique_id(hass, "sensor", f"{entry.entry_id}_subscription")
    assert malformed.state == STATE_UNKNOWN
    assert "application_usage_allowed" not in malformed.attributes
    assert "max_collars" not in malformed.attributes
    assert "max_fences" not in malformed.attributes


async def test_runtime_tracker_uses_home_zone_when_halo_reports_indoor_wifi(hass):
    client = FakeHaloClient(_state(indoors=True))
    entry = _entry({})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    tracker = hass.states.get("device_tracker.cowboy")
    assert tracker is not None
    assert tracker.state == "home"


async def test_runtime_reload_preserves_domain_lock_and_revokes_disable_surface(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)

    domain_data = hass.data[DOMAIN]
    original_lock = control_lock_for(domain_data, entry.entry_id)

    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        return_value=client,
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_ALLOW_FENCE_DISABLE: True},
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is original_lock
    assert hass.states.get("switch.cowboy_fence_mode").state == STATE_ON

    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        return_value=client,
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_ENABLE_FENCE_CONTROLS: True,
                CONF_ALLOW_FENCE_DISABLE: False,
            },
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is original_lock
    revoked = hass.states.get("switch.cowboy_fence_mode")
    assert revoked is None or revoked.state == STATE_UNAVAILABLE
    assert client.writes == []


async def test_runtime_manual_unload_and_setup_reuses_lock(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    original_lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]

    await _setup(hass, entry, client)
    assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is original_lock


@pytest.mark.parametrize(
    ("error", "expected_state"),
    [
        (lambda: HaloAuthError("expired"), ConfigEntryState.SETUP_ERROR),
        (lambda: HaloApiError("offline"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_runtime_first_refresh_classifies_auth_and_transient_failures(
    hass, error, expected_state
):
    client = FailingHaloClient(_state(), error())
    entry = _entry({})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        return_value=client,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is expected_state
    assert client.fetches == 1


async def test_runtime_token_persistence_and_interval_updates_do_not_reload(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    client._snapshot = {
        "access_token": "rotated-access",
        "refresh_token": "rotated-refresh",
        "expires_at": 4_102_444_900.0,
    }
    reload_mock = AsyncMock()
    with patch.object(hass.config_entries, "async_reload", reload_mock):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_SCAN_INTERVAL: 600},
        )
        await hass.async_block_till_done()

    assert entry.data[CONF_ACCESS_TOKEN] == "rotated-access"
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh"
    assert coordinator.update_interval.total_seconds() == 600
    reload_mock.assert_not_awaited()
