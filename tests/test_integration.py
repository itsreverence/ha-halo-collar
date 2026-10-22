from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.halo_collar import api as halo_api
from custom_components.halo_collar import async_unload_entry
from custom_components.halo_collar import config_flow as halo_config_flow
from custom_components.halo_collar.api import HaloApiError, HaloAuthError, HaloState
from custom_components.halo_collar.const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EMAIL,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_EXPIRES_AT,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    DOMAIN,
)
from custom_components.halo_collar.control_status import find_status_signal
from custom_components.halo_collar.controls import (
    HaloControlError,
    control_lock_for,
    find_collar_cooldowns_for,
)
from custom_components.halo_collar.diagnostics import async_get_config_entry_diagnostics
from custom_components.halo_collar.helpers import reported_activity_options


class FakeHaloClient:
    def __init__(self, state):
        self.state = state
        self.fetches = 0
        self.writes = []
        self.finds = []
        self.on_token_update = None
        self._snapshot = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 4_102_444_800.0,
        }

    @property
    def token_snapshot(self):
        return dict(self._snapshot)

    def commit_tokens(self, snapshot):
        self._snapshot = snapshot
        self.on_token_update(self.token_snapshot)

    async def async_fetch_state(self):
        self.fetches += 1
        return self.state

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        self.state = deepcopy(self.state)
        for pet in self.state.pets:
            if pet["id"] == pet_id:
                pet["telemetry"]["mode"]["fencesOn"] = enabled
        return {"ok": True}

    async def async_find_collar(self, collar_id, *, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.finds.append(collar_id)


class FailingHaloClient(FakeHaloClient):
    def __init__(self, state, error):
        super().__init__(state)
        self.error = error

    async def async_fetch_state(self):
        self.fetches += 1
        raise self.error


class ToggleFailureClient(FakeHaloClient):
    def __init__(self, state):
        super().__init__(state)
        self.error: Exception | None = None

    async def async_fetch_state(self):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return self.state


def _complete_issues(status: str = "noIssue") -> dict:
    return {
        "hasReportingIssue": False,
        "battery": {
            "chargingIssue2IssueStatus": status,
            "degradationIssueStatus": status,
            "gaugeChipFailureIssueStatus": status,
        },
        "gps": {
            "chipFailure1IssueStatus": status,
            "chipFailure4IssueStatus": status,
            "outdatedChipFailureIssueStatus": status,
        },
        "lte": {
            "moduleFailureIssueStatus": status,
            "simCardScrappedIssueStatus": status,
            "simCardSlotFailureIssueStatus": status,
        },
        "wifi": {"moduleFailureIssueStatus": status},
        "other": {
            "audioChipFailureIssueStatus": status,
            "bleChipFailureIssueStatus": status,
            "processorCorruption1IssueStatus": status,
            "processorCorruption2IssueStatus": status,
            "processorCorruption3IssueStatus": status,
        },
        "sensors": {
            "compassCalibrationFailureIssueStatus": status,
            "memsChipFailureIssueStatus": status,
        },
    }


def _state(*, indoors: bool = False, walks: list[dict] | None = None):
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    pet = {
        "id": "pet-1",
        "name": "Cowboy",
        "collarInfo": {"id": "collar-1", "telemetry": {"walk": None}},
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
            "features": [
                {
                    "featureType": {"id": "findcollar"},
                    "isEnabled": True,
                }
            ],
        },
        server_time=timestamp,
        walks=[] if walks is None else walks,
    )


def _state_with_second_collar():
    state = _state()
    pet = deepcopy(state.pets[0])
    pet["id"] = "pet-2"
    pet["name"] = "Synthetic Two"
    pet["collarInfo"]["id"] = "collar-2"
    collar = deepcopy(state.collars[0])
    collar["id"] = "collar-2"
    collar["serialNumber"] = "serial-2"
    collar["petInfo"]["id"] = "pet-2"
    collar["petInfo"]["name"] = "Synthetic Two"
    state.pets.append(pet)
    state.collars.append(collar)
    return state


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
    def build_client(**kwargs):
        client.on_token_update = kwargs["on_token_update"]
        return client

    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        side_effect=build_client,
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
    assert hass.states.get("button.cowboy_find_collar_sound_and_light") is None
    assert hass.states.get("switch.cowboy_fence_mode") is None
    assert hass.states.get("device_tracker.cowboy") is not None
    assert hass.states.get("event.cowboy_fence_breach_event") is not None
    assert hass.states.get("binary_sensor.cowboy_fences_enabled").state == STATE_ON
    assert hass.states.get("binary_sensor.cowboy_fence_state_synchronized").state == STATE_ON
    assert (
        "device_class"
        not in hass.states.get("binary_sensor.cowboy_fence_state_synchronized").attributes
    )

    collar_id = client.state.collars[0]["id"]
    activity = _state_for_unique_id(hass, "sensor", f"{collar_id}_activity_duration")
    assert float(activity.state) == 15
    assert activity.attributes["unit_of_measurement"] == "min"
    assert activity.attributes["goal"] == 1200.0
    assert activity.attributes["progress_percent"] == 75.0
    outdoor_time = _state_for_unique_id(hass, "sensor", f"{collar_id}_outdoor_time")
    assert float(outdoor_time.state) == 5
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
        _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_collar_hardware_issue").state
        == STATE_UNKNOWN
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


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("gps_calibration_required", "isGpsCalibrationRequired"),
        ("compass_calibration_required", "isCompassCalibrationRequired"),
    ],
)
@pytest.mark.parametrize(
    ("telemetry", "expected"),
    [
        pytest.param({"flag": True}, STATE_ON, id="true"),
        pytest.param({"flag": False}, STATE_OFF, id="false"),
        pytest.param({}, STATE_UNKNOWN, id="missing"),
        pytest.param({"flag": None}, STATE_UNKNOWN, id="null"),
        pytest.param({"flag": "false"}, STATE_UNKNOWN, id="string-false"),
        pytest.param({"flag": "true"}, STATE_UNKNOWN, id="string-true"),
        pytest.param({"flag": 0}, STATE_UNKNOWN, id="zero"),
        pytest.param({"flag": 1}, STATE_UNKNOWN, id="one"),
        pytest.param({"flag": []}, STATE_UNKNOWN, id="list"),
        pytest.param({"flag": {}}, STATE_UNKNOWN, id="object"),
    ],
)
async def test_runtime_calibration_requires_reported_boolean(hass, key, field, telemetry, expected):
    state = _state()
    if "flag" in telemetry:
        state.collars[0]["telemetry"][field] = telemetry["flag"]
    client = FakeHaloClient(state)
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    assert _state_for_unique_id(hass, "binary_sensor", f"collar-1_{key}").state == expected


async def test_runtime_hardware_issue_is_bounded_and_fail_closed(hass):
    state = _state()
    issues = _complete_issues()
    issues["battery"]["degradationIssueStatus"] = "nonCatastrophic"
    issues["other"]["audioChipFailureIssueStatus"] = "catastrophic"
    state.collars[0]["issues"] = issues
    client = FakeHaloClient(state)
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    collar_id = state.collars[0]["id"]

    hardware = _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_collar_hardware_issue")
    assert hardware.state == STATE_ON
    assert hardware.attributes["critical_issues"] == ["Audio chip"]
    assert hardware.attributes["non_critical_issues"] == ["Battery degradation"]
    serialized = json.dumps(hardware.attributes).lower()
    assert "status" not in serialized
    assert "serial" not in serialized
    assert "id" not in serialized

    issues["wifi"]["moduleFailureIssueStatus"] = "unexpected-provider-value"
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()
    hardware = _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_collar_hardware_issue")
    assert hardware.state == STATE_UNKNOWN
    assert "critical_issues" not in hardware.attributes
    assert "non_critical_issues" not in hardware.attributes


async def test_find_collar_button_is_opt_in_and_dispatches_one_synthetic_command(hass, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr("custom_components.halo_collar.controls.time.monotonic", lambda: clock[0])
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    collar_id = client.state.collars[0]["id"]
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{collar_id}_find_collar")
    assert entity_id == "button.cowboy_find_collar_sound_and_light"
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
    status = _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status")
    assert status.state == "ready"
    assert status.attributes["device_class"] == "enum"
    assert status.attributes["advisory"] is True

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert client.finds == ["collar-1"]
    assert client.writes == []
    assert client.fetches == 3
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "cooldown"

    clock[0] += 61.0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
    assert client.fetches == 3
    assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "ready"


@pytest.mark.parametrize(("service", "enabled"), [("turn_on", True), ("turn_off", False)])
async def test_fence_switch_writes_requested_mode_and_shows_confirmed_state(hass, service, enabled):
    state = _state()
    state.pets[0]["telemetry"]["mode"]["fencesOn"] = not enabled
    client = FakeHaloClient(state)
    entry = _entry({CONF_ALLOW_FENCE_DISABLE: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    before, after = (STATE_OFF, STATE_ON) if enabled else (STATE_ON, STATE_OFF)
    assert hass.states.get("switch.cowboy_fence_mode").state == before

    await hass.services.async_call(
        "switch", service, {"entity_id": "switch.cowboy_fence_mode"}, blocking=True
    )
    await hass.async_block_till_done()

    assert client.writes == [("pet-1", enabled)]
    assert hass.states.get("switch.cowboy_fence_mode").state == after


async def test_fence_switch_turn_off_during_active_walk_is_refused_without_a_write(hass):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ALLOW_FENCE_DISABLE: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    client.state = deepcopy(client.state)
    telemetry = client.state.collars[0]["telemetry"]
    telemetry["walk"] = {"startTime": telemetry["manifest"]["timestamp"]}

    with pytest.raises(HomeAssistantError, match="active walk"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.cowboy_fence_mode"}, blocking=True
        )
    await hass.async_block_till_done()

    assert client.writes == []
    assert hass.states.get("switch.cowboy_fence_mode").state == STATE_ON


async def test_enable_fences_button_writes_enable_and_shows_confirmed_state(hass):
    state = _state()
    state.pets[0]["telemetry"]["mode"]["fencesOn"] = False
    client = FakeHaloClient(state)
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    assert hass.states.get("binary_sensor.cowboy_fences_enabled").state == STATE_OFF

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.cowboy_enable_fences"}, blocking=True
    )
    await hass.async_block_till_done()

    assert client.writes == [("pet-1", True)]
    assert hass.states.get("binary_sensor.cowboy_fences_enabled").state == STATE_ON


@pytest.mark.parametrize(
    "options,actions",
    [
        ({CONF_ENABLE_FENCE_CONTROLS: False}, set()),
        ({}, {"enable_fences"}),
        ({CONF_ALLOW_FENCE_DISABLE: True}, {"enable_fences", "disable_fences"}),
        (
            {CONF_ENABLE_FENCE_CONTROLS: False, CONF_ALLOW_FENCE_DISABLE: True},
            set(),
        ),
        (
            {CONF_ENABLE_FENCE_CONTROLS: False, CONF_ENABLE_FIND_COLLAR: True},
            {"find_collar"},
        ),
    ],
)
async def test_status_entities_follow_existing_opt_in_tiers(hass, options, actions):
    client = FakeHaloClient(_state())
    entry = _entry(options)
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    registry = er.async_get(hass)
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])
    for action in ("enable_fences", "disable_fences", "find_collar"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"collar-1_{action}_status")
        assert (entity_id is not None) == (action in actions)
        if entity_id is not None:
            state = hass.states.get(entity_id)
            assert state.state == "ready"
            assert state.attributes["advisory"] is True
            for reason in state.attributes["options"]:
                assert translations[
                    f"component.{DOMAIN}.entity.sensor.{action}_status.state.{reason}"
                ]
    assert client.fetches == 1
    assert client.writes == client.finds == []
    assert (
        _state_for_unique_id(hass, "sensor", "collar-1_last_telemetry").attributes["device_class"]
        == "timestamp"
    )
    assert (
        _state_for_unique_id(hass, "binary_sensor", "collar-1_online").attributes["device_class"]
        == "connectivity"
    )


async def test_status_remains_readable_through_unknown_stale_missing_and_cloud_failure(hass):
    client = ToggleFailureClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True, CONF_ALLOW_FENCE_DISABLE: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async def refresh_and_expect(expected):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        for action in ("enable_fences", "disable_fences", "find_collar"):
            status = _state_for_unique_id(hass, "sensor", f"collar-1_{action}_status")
            assert status.state == expected

    client.state.collars[0]["telemetry"]["manifest"].clear()
    await refresh_and_expect("report_unknown")
    client.state.collars[0]["telemetry"]["manifest"]["timestamp"] = (
        datetime.now(UTC) - timedelta(seconds=1000)
    ).isoformat()
    await refresh_and_expect("report_stale")
    client.state.pets.clear()
    await refresh_and_expect("mapping_unavailable")
    client.state.collars.clear()
    await refresh_and_expect("mapping_unavailable")
    client.error = HaloApiError("synthetic outage")
    await refresh_and_expect("cloud_unavailable")
    client.error = None
    client.state = _state()
    await refresh_and_expect("ready")


async def test_advisory_ready_does_not_authorize_a_find_after_fresh_walk_rejection(hass):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "ready"
    client.state = deepcopy(client.state)
    client.state.collars[0]["telemetry"]["walk"] = {}
    button = _state_for_unique_id(hass, "button", "collar-1_find_collar")
    with pytest.raises(HomeAssistantError, match="active or unknown walk"):
        await hass.services.async_call(
            "button", "press", {"entity_id": button.entity_id}, blocking=True
        )
    await hass.async_block_till_done()
    assert client.finds == []
    assert client.fetches == 2
    assert (
        _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "walk_active"
    )


async def test_find_status_notifications_are_scoped_to_entry_and_collar(hass, monkeypatch):
    monkeypatch.setattr(
        "custom_components.halo_collar.controls.time", SimpleNamespace(monotonic=lambda: 1000.0)
    )
    client = FakeHaloClient(_state_with_second_collar())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
    cooldowns.update({"collar-1": 1000.0, "collar-2": 1000.0})
    async_dispatcher_send(hass, find_status_signal("other-entry", "collar-1"))
    assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "ready"
    async_dispatcher_send(hass, find_status_signal(entry.entry_id, "collar-1"))
    assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == "cooldown"
    assert _state_for_unique_id(hass, "sensor", "collar-2_find_collar_status").state == "ready"
    async_dispatcher_send(hass, find_status_signal(entry.entry_id, "collar-2"))
    assert _state_for_unique_id(hass, "sensor", "collar-2_find_collar_status").state == "cooldown"
    assert client.fetches == 1


async def test_find_status_restores_cooldown_on_reload_with_button_disabled(hass, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(
        "custom_components.halo_collar.controls.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    button = _state_for_unique_id(hass, "button", "collar-1_find_collar")
    status_id = _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").entity_id
    await hass.services.async_call(
        "button", "press", {"entity_id": button.entity_id}, blocking=True
    )
    assert hass.states.get(status_id).state == "cooldown"
    assert await hass.config_entries.async_unload(entry.entry_id)
    er.async_get(hass).async_update_entity(
        button.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    clock[0] += 20
    await _setup(hass, entry, client)
    assert hass.states.get(status_id).state == "cooldown"
    fetches = client.fetches
    clock[0] += 41
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=41))
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "ready"
    assert client.fetches == fetches


@pytest.mark.parametrize("failed_reconciliation", [False, True])
async def test_status_cooldown_lifecycle_survives_button_disabled_after_reload(
    hass, monkeypatch, failed_reconciliation
):
    clock = [1000.0]
    monkeypatch.setattr(
        "custom_components.halo_collar.controls.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    client = ToggleFailureClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    registry = er.async_get(hass)
    button_id = registry.async_get_entity_id("button", DOMAIN, "collar-1_find_collar")
    old_button = hass.data[DATA_INSTANCES]["button"].get_entity(button_id)
    old_coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
    status_id = _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").entity_id
    dispatch_waiting = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def late_dispatch(collar_id, *, pre_dispatch):
        dispatch_waiting.set()
        await release_dispatch.wait()
        pre_dispatch()
        client.finds.append(collar_id)
        if failed_reconciliation:
            client.error = HaloApiError("synthetic reconciliation outage")

    client.async_find_collar = late_dispatch
    task = asyncio.create_task(old_button.async_press())
    try:
        async with asyncio.timeout(10):
            await dispatch_waiting.wait()
            assert await hass.config_entries.async_unload(entry.entry_id)
            registry.async_update_entity(button_id, disabled_by=er.RegistryEntryDisabler.USER)
            await _setup(hass, entry, client)
            assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is not old_coordinator
            assert find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id) is cooldowns
            assert hass.data[DATA_INSTANCES]["button"].get_entity(button_id) is None
            assert hass.states.get(status_id).state == "ready"
            release_dispatch.set()
            await task
    finally:
        release_dispatch.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await hass.async_block_till_done()
    assert client.finds == ["collar-1"]
    assert hass.states.get(status_id).state == "cooldown"
    fetches = client.fetches
    for _ in range(3):
        async_dispatcher_send(hass, find_status_signal(entry.entry_id, "collar-1"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "cooldown"
    assert client.fetches == fetches
    clock[0] += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == "ready"
    assert client.fetches == fetches
    cooldowns["collar-1"] = clock[0]
    async_dispatcher_send(hass, find_status_signal(entry.entry_id, "collar-1"))
    assert hass.states.get(status_id).state == "cooldown"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    removed = hass.states.get(status_id)
    async_dispatcher_send(hass, find_status_signal(entry.entry_id, "collar-1"))
    clock[0] += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=244))
    await hass.async_block_till_done()
    assert hass.states.get(status_id) == removed
    assert client.fetches == fetches


@pytest.mark.parametrize("failure", [False, True])
async def test_find_status_listener_failure_cannot_change_transaction_result(hass, failure):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    button = _state_for_unique_id(hass, "button", "collar-1_find_collar")

    @callback
    def broken_listener():
        raise RuntimeError("synthetic advisory listener failure")

    remove = async_dispatcher_connect(
        hass, find_status_signal(entry.entry_id, "collar-1"), broken_listener
    )
    if failure:

        async def reject(collar_id, *, pre_dispatch):
            raise HaloControlError("synthetic transaction rejection")

        client.async_find_collar = reject
    try:
        if failure:
            with pytest.raises(HomeAssistantError, match="synthetic transaction rejection"):
                await hass.services.async_call(
                    "button", "press", {"entity_id": button.entity_id}, blocking=True
                )
        else:
            await hass.services.async_call(
                "button", "press", {"entity_id": button.entity_id}, blocking=True
            )
        await hass.async_block_till_done()
        assert client.finds == ([] if failure else ["collar-1"])
        assert _state_for_unique_id(hass, "sensor", "collar-1_find_collar_status").state == (
            "ready" if failure else "cooldown"
        )
    finally:
        remove()


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
    assert float(outdoor_time.state) == 8
    assert outdoor_time.attributes["goal"] == 960.0
    assert outdoor_time.attributes["progress_percent"] == 50.0


async def test_reported_activity_translations_match_enum_options(hass):
    state = _state()
    state.pets[0]["telemetry"]["activityType"] = 8
    client = FakeHaloClient(state)
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    activity = _state_for_unique_id(hass, "sensor", "collar-1_last_reported_activity")
    assert activity.state == "human_carry"
    assert activity.attributes["friendly_name"] == "Cowboy Last reported activity"
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])
    prefix = f"component.{DOMAIN}.entity.sensor.last_reported_activity.state."
    actual = {
        key.removeprefix(prefix): value
        for key, value in translations.items()
        if key.startswith(prefix)
    }
    assert set(actual) == set(activity.attributes["options"]) == set(reported_activity_options())
    assert actual["human_carry"] == "Being carried"
    assert actual["eating_drinking"] == "Eating or drinking"
    assert client.writes == client.finds == []


async def test_runtime_telemetry_sensors_read_live_payload_shape(hass):
    state = _state()
    collar = state.collars[0]
    reported = datetime.now(UTC).replace(microsecond=0)
    collar["firmware"] = {"formattedVersion": "03.06.64"}
    collar["petInfo"]["telemetry"].update(
        {"gpsAccuracyInMeters": 10.01, "gpsAccuracyStatus": "indoors"}
    )
    collar["telemetry"].update(
        {
            "manifest": {"timestamp": reported.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "batteryStatus": "notcharged",
            "remainingBatteryLifetimeInSeconds": 117411,
            "currentAdapter": "wifi",
            "wiFi": {"status": "socketconnected", "signalStrength": -70},
            "cellular": {"status": "disconnected", "signalStrength": -87},
        }
    )
    client = FakeHaloClient(state)
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    expected = {
        "battery_status": "Not charging",
        "remaining_battery_lifetime": "32.6",
        "current_adapter": "Wi-Fi",
        "wifi_status": "Connected",
        "cellular_signal": "-87",
        "gps_accuracy_status": "Indoors",
        "firmware": "03.06.64",
        "last_telemetry": reported.isoformat(),
    }
    assert {
        key: _state_for_unique_id(hass, "sensor", f"collar-1_{key}").state for key in expected
    } == expected


async def test_runtime_telemetry_sensors_are_unknown_when_telemetry_is_malformed(hass):
    state = _state()
    state.collars[0]["telemetry"] = ["unexpected", {"private": "value"}]
    client = FakeHaloClient(state)
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    for key in ("battery", "wifi_status", "last_telemetry"):
        assert _state_for_unique_id(hass, "sensor", f"collar-1_{key}").state == STATE_UNKNOWN


async def test_present_tense_telemetry_is_unknown_while_connectivity_is_off(hass):
    state = _state()
    state.pets[0]["telemetry"]["activityType"] = 2
    walk = {"durationFromStartInSeconds": 120, "walkedDistance": 250, "isPaused": True}
    state.collars[0]["telemetry"].update({"walk": walk, "fenceBreach": {}})
    state.pets[0]["collarInfo"].setdefault("telemetry", {})["walk"] = walk
    manifest = state.collars[0]["telemetry"]["manifest"]
    fresh_timestamp = manifest["timestamp"]
    client = FakeHaloClient(state)
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    present_tense = {
        ("sensor", "last_reported_activity"): "resting",
        ("sensor", "safety_status"): "Safe",
        ("sensor", "current_fence"): "Synthetic Fence",
        ("sensor", "active_walk_duration"): "2.0",
        ("sensor", "active_walk_distance"): "250.0",
        ("binary_sensor", "active_walk"): STATE_ON,
        ("binary_sensor", "active_walk_paused"): STATE_ON,
        ("binary_sensor", "fence_breach"): STATE_ON,
    }

    async def refresh_with_report(timestamp):
        manifest["timestamp"] = timestamp
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    def states():
        return {
            (platform, key): _state_for_unique_id(hass, platform, f"collar-1_{key}").state
            for platform, key in present_tense
        }

    assert states() == present_tense

    await refresh_with_report("2020-01-01T00:00:00Z")
    assert _state_for_unique_id(hass, "binary_sensor", "collar-1_online").state == "off"
    assert states() == dict.fromkeys(present_tense, STATE_UNKNOWN)
    assert _state_for_unique_id(hass, "sensor", "collar-1_battery").state == "90"

    await refresh_with_report(fresh_timestamp)
    assert states() == present_tense


async def test_runtime_walk_sensors_are_safe_and_update_after_refresh(hass):
    walks = [
        {
            "startedAt": "2026-07-06T09:00:00Z",
            "endedAt": "2026-07-06T09:10:00Z",
            "startTrigger": "scheduled_walk",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 600,
                    "walkedDistanceInMeters": 100,
                }
            ],
        },
        {
            "startedAt": "2026-07-06T11:00:00Z",
            "endedAt": "2026-07-06T11:07:00Z",
            "startTrigger": "button",
            "id": "PRIVATE_WALK_ID",
            "locationName": "PRIVATE_LOCATION_SENTINEL",
            "routeImageUrl": "https://private.invalid/route.png",
            "trailImageUrl": "https://private.invalid/trail.png",
            "user": {"id": "PRIVATE_USER_ID"},
            "feedback": {"correctionCount": 7},
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 420,
                    "walkedDistanceInMeters": 125.5,
                    "feedback": "PRIVATE_FEEDBACK_SENTINEL",
                }
            ],
        },
    ]
    client = FakeHaloClient(_state(walks=walks))
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    last_walk = _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk")
    assert last_walk.state == "2026-07-06T11:07:00+00:00"
    assert last_walk.attributes["started_at"] == "2026-07-06T11:00:00+00:00"
    assert last_walk.attributes["start_trigger"] == "Button"
    duration = _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_duration")
    assert duration.state == "7.0"
    assert duration.attributes["unit_of_measurement"] == "min"
    distance = _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_distance")
    assert distance.state == "125.5"
    assert distance.attributes["unit_of_measurement"] == "m"
    speed = _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_average_speed")
    assert float(speed.state) == pytest.approx(125.5 / 420)
    assert speed.attributes["unit_of_measurement"] == "m/s"

    forbidden_key_fragments = (
        "id",
        "location",
        "route",
        "trail",
        "user",
        "feedback",
        "correction",
    )
    standard_metadata_keys = {
        "friendly_name",
        "device_class",
        "unit_of_measurement",
    }
    forbidden_values = (
        "private_location_sentinel",
        "https://private.invalid/route.png",
        "https://private.invalid/trail.png",
        "private_walk_id",
        "private_user_id",
        "private_feedback_sentinel",
    )

    def assert_private_data_absent(value):
        if isinstance(value, dict):
            for key, nested_value in value.items():
                if key not in standard_metadata_keys:
                    assert not any(
                        fragment in str(key).lower() for fragment in forbidden_key_fragments
                    )
                assert_private_data_absent(nested_value)
        elif isinstance(value, (list, tuple, set)):
            for nested_value in value:
                assert_private_data_absent(nested_value)

    for state in (last_walk, duration, distance):
        assert_private_data_absent(state.attributes)
        serialized_attributes = json.dumps(state.attributes, default=str).lower()
        string_attributes = str(state.attributes).lower()
        forbidden_tokens = (*forbidden_key_fragments, *forbidden_values)
        assert not any(token in serialized_attributes for token in forbidden_tokens)
        assert not any(token in string_attributes for token in forbidden_tokens)

    client.state.walks = [
        {
            "endedAt": "2026-07-06T12:00:00Z",
            "startTrigger": "PRIVATE_LOCATION_SENTINEL",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 180,
                    "walkedDistanceInMeters": 50,
                }
            ],
        }
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk").state
        == "2026-07-06T12:00:00+00:00"
    )
    updated_last_walk = _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk")
    assert "start_trigger" not in updated_last_walk.attributes
    serialized_updated_attributes = json.dumps(updated_last_walk.attributes, default=str).lower()
    assert "private_location_sentinel" not in serialized_updated_attributes
    assert "private_location_sentinel" not in str(updated_last_walk.attributes).lower()
    assert _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_duration").state == "3.0"
    assert _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_distance").state == "50.0"


async def test_diagnostics_walk_history_keeps_only_scalar_pet_metrics(hass):
    walks = [
        {
            "id": "PRIVATE_WALK_ID",
            "endedAt": "2026-07-06T11:07:00Z",
            "locationName": "PRIVATE_LOCATION_SENTINEL",
            "routeImageUrl": "https://private.invalid/route.png",
            "user": {"id": "PRIVATE_USER_ID"},
            "feedback": {"correction": "PRIVATE_FEEDBACK_SENTINEL"},
            "startedAt": "2026-07-06T11:00:00Z",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 420,
                    "walkedDistanceInMeters": 125.5,
                    "feedbacksCount": 3,
                    "feedbacks": [{"type": "PRIVATE_FEEDBACK_SENTINEL"}],
                    "nested": {"route": "PRIVATE_NESTED_SENTINEL"},
                }
            ],
        }
    ]
    client = FakeHaloClient(_state(walks=walks))
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostics, default=str).lower()

    assert diagnostics["walk_history_status"] == "ok"
    assert diagnostics["walk_history"] == [
        {
            "pets": [
                {
                    "walkedDurationInSeconds": 420,
                    "walkedDistanceInMeters": 125.5,
                    "feedbacksCount": 3,
                }
            ]
        }
    ]
    assert "2026-07-06" not in json.dumps(diagnostics["walk_history"])
    for sentinel in (
        "private_walk_id",
        "private_location_sentinel",
        "private.invalid/route.png",
        "private_user_id",
        "private_feedback_sentinel",
        "private_nested_sentinel",
        "pet-1",
        "collar-1",
        "serial-1",
        "synthetic fence",
        "cowboy",
        "excluded",
    ):
        assert sentinel not in serialized


async def test_runtime_walk_sensors_are_unknown_for_empty_or_mismatched_history(hass):
    client = FakeHaloClient(_state(walks=[]))
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    for key in (
        "last_walk",
        "last_walk_duration",
        "last_walk_distance",
        "last_walk_average_speed",
    ):
        assert _state_for_unique_id(hass, "sensor", f"{collar_id}_{key}").state == STATE_UNKNOWN

    client.state.walks = [{"endedAt": "not-a-time", "pets": [{"id": "pet-other"}]}]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    for key in (
        "last_walk",
        "last_walk_duration",
        "last_walk_distance",
        "last_walk_average_speed",
    ):
        assert _state_for_unique_id(hass, "sensor", f"{collar_id}_{key}").state == STATE_UNKNOWN

    client.state.walks = [
        {
            "endedAt": "2026-07-06T12:00:00Z",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 0,
                    "walkedDistanceInMeters": 12,
                }
            ],
        }
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        _state_for_unique_id(hass, "sensor", f"{collar_id}_last_walk_average_speed").state
        == STATE_UNKNOWN
    )


async def test_runtime_active_walk_entity_is_unknown_when_walk_fields_are_missing(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    client.state.pets[0]["collarInfo"]["telemetry"].pop("walk")
    client.state.collars[0]["telemetry"].pop("walk")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    active_walk = _state_for_unique_id(hass, "binary_sensor", f"{collar_id}_active_walk")
    assert active_walk.state == STATE_UNKNOWN


async def test_runtime_active_walk_entities_track_verified_collar_snapshots(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    collar_id = client.state.collars[0]["id"]

    active_walk_unique_id = f"{collar_id}_active_walk"
    paused_unique_id = f"{collar_id}_active_walk_paused"
    duration_unique_id = f"{collar_id}_active_walk_duration"
    distance_unique_id = f"{collar_id}_active_walk_distance"
    assert _state_for_unique_id(hass, "binary_sensor", active_walk_unique_id).state == "off"
    assert _state_for_unique_id(hass, "binary_sensor", paused_unique_id).state == "off"
    assert _state_for_unique_id(hass, "sensor", duration_unique_id).state == STATE_UNKNOWN
    assert _state_for_unique_id(hass, "sensor", distance_unique_id).state == STATE_UNKNOWN

    synthetic_walk = {
        "durationFromStartInSeconds": 60,
        "walkedDistance": 125.5,
        "isPaused": False,
        "id": "PRIVATE_ACTIVE_WALK_ID",
        "location": "PRIVATE_ACTIVE_LOCATION_SENTINEL",
    }
    client.state.pets[0]["collarInfo"]["telemetry"]["walk"] = synthetic_walk
    client.state.collars[0]["telemetry"]["walk"] = synthetic_walk
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_for_unique_id(hass, "binary_sensor", active_walk_unique_id).state == STATE_ON
    assert _state_for_unique_id(hass, "binary_sensor", paused_unique_id).state == "off"
    duration = _state_for_unique_id(hass, "sensor", duration_unique_id)
    distance = _state_for_unique_id(hass, "sensor", distance_unique_id)
    assert duration.state == "1.0"
    assert duration.attributes["unit_of_measurement"] == "min"
    assert distance.state == "125.5"
    assert distance.attributes["unit_of_measurement"] == "m"
    serialized_entities = json.dumps(
        {
            "duration": duration.as_dict(),
            "distance": distance.as_dict(),
            "paused": _state_for_unique_id(hass, "binary_sensor", paused_unique_id).as_dict(),
        }
    ).lower()
    assert "private_active_walk_id" not in serialized_entities
    assert "private_active_location_sentinel" not in serialized_entities

    synthetic_walk.update(
        {"durationFromStartInSeconds": 120, "walkedDistance": 250, "isPaused": True}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_for_unique_id(hass, "binary_sensor", paused_unique_id).state == STATE_ON
    assert _state_for_unique_id(hass, "sensor", duration_unique_id).state == "2.0"
    assert _state_for_unique_id(hass, "sensor", distance_unique_id).state == "250.0"

    client.state.pets[0]["collarInfo"]["telemetry"]["walk"] = None
    client.state.collars[0]["telemetry"]["walk"] = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_for_unique_id(hass, "binary_sensor", active_walk_unique_id).state == "off"
    assert _state_for_unique_id(hass, "binary_sensor", paused_unique_id).state == "off"
    assert _state_for_unique_id(hass, "sensor", duration_unique_id).state == STATE_UNKNOWN
    assert _state_for_unique_id(hass, "sensor", distance_unique_id).state == STATE_UNKNOWN

    client.state.pets[0]["collarInfo"]["telemetry"].pop("walk")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert _state_for_unique_id(hass, "binary_sensor", active_walk_unique_id).state == STATE_UNKNOWN
    assert _state_for_unique_id(hass, "binary_sensor", paused_unique_id).state == STATE_UNKNOWN
    assert client.writes == []


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


@pytest.mark.parametrize(
    ("breach", "attributes"),
    [
        pytest.param(
            {"fenceName": "Synthetic Fence", "direction": "exit"},
            {"fenceName": "Synthetic Fence", "direction": "exit"},
            id="dict",
        ),
        pytest.param("synthetic-breach", {"detail": "synthetic-breach"}, id="scalar"),
    ],
)
async def test_fence_breach_event_fires_once_per_new_breach(hass, freezer, breach, attributes):
    state = _state()
    telemetry = state.collars[0]["telemetry"]
    earlier = {"fenceName": "Synthetic Earlier Fence"}
    telemetry["fenceBreach"] = earlier
    client = FakeHaloClient(state)
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async def refresh_with(value):
        freezer.tick(60)
        telemetry["fenceBreach"] = value
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        return hass.states.get("event.cowboy_fence_breach_event")

    assert hass.states.get("event.cowboy_fence_breach_event").state == STATE_UNKNOWN
    assert (await refresh_with(deepcopy(earlier))).state == STATE_UNKNOWN

    fired = await refresh_with(breach)
    assert fired.state != STATE_UNKNOWN
    assert fired.attributes["event_type"] == "fence_breach"
    assert {key: fired.attributes.get(key) for key in attributes} == attributes

    repeated = await refresh_with(deepcopy(breach))
    assert (repeated.state, repeated.attributes) == (fired.state, fired.attributes)

    cleared = await refresh_with(None)
    assert (cleared.state, cleared.attributes) == (fired.state, fired.attributes)


@pytest.mark.parametrize(
    ("pet_info", "expected_state", "expected"),
    [
        pytest.param(
            {
                "location": {"latitude": 12.5, "longitude": -45.25},
                "lastLocation": {"latitude": -7.75, "longitude": 101.5},
            },
            "not_home",
            {"latitude": 12.5, "longitude": -45.25, "gps_accuracy": 5.0},
            id="location",
        ),
        pytest.param(
            {"location": None, "lastLocation": {"latitude": -7.75, "longitude": 101.5}},
            "not_home",
            {"latitude": -7.75, "longitude": 101.5, "gps_accuracy": 5.0},
            id="last-location",
        ),
        pytest.param({}, STATE_UNKNOWN, {}, id="none"),
    ],
)
async def test_runtime_tracker_reports_current_or_last_known_coordinates(
    hass, pet_info, expected_state, expected
):
    state = _state()
    collar_pet_info = state.collars[0]["petInfo"]
    collar_pet_info.pop("location")
    collar_pet_info.update(pet_info)
    client = FakeHaloClient(state)
    entry = _entry({})
    entry.add_to_hass(hass)

    await _setup(hass, entry, client)

    tracker = hass.states.get("device_tracker.cowboy")
    coordinates = {
        key: tracker.attributes[key]
        for key in ("latitude", "longitude", "gps_accuracy")
        if key in tracker.attributes
    }
    assert (tracker.state, coordinates) == (expected_state, expected)


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


async def test_runtime_find_option_reload_adds_button_and_preserves_lock(hass):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: False})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)

    collar_id = client.state.collars[0]["id"]
    unique_id = f"{collar_id}_find_collar"
    assert er.async_get(hass).async_get_entity_id("button", DOMAIN, unique_id) is None
    original_lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)

    with patch(
        "custom_components.halo_collar.api.HaloApiClient",
        return_value=client,
    ):
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_ENABLE_FIND_COLLAR: True},
        )
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is original_lock
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, unique_id)
    assert entity_id == "button.cowboy_find_collar_sound_and_light"
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
    assert client.finds == []


async def test_runtime_manual_unload_and_setup_reuses_lock_and_entity_ids(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    original_lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)
    registry = er.async_get(hass)

    def registered_entities():
        return {
            item.unique_id: item.entity_id
            for item in registry.entities.values()
            if item.config_entry_id == entry.entry_id
        }

    before = registered_entities()
    assert before["collar-1_last_walk_average_speed"] == ("sensor.cowboy_recent_walk_average_speed")
    assert before["collar-1_collar_hardware_issue"] == (
        "binary_sensor.cowboy_collar_hardware_issue"
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]

    await _setup(hass, entry, client)
    assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is original_lock
    assert registered_entities() == before


async def test_failed_unload_retains_runtime_and_control_state(hass):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    domain_data = hass.data[DOMAIN]
    stored = domain_data[entry.entry_id]
    original_lock = control_lock_for(domain_data, entry.entry_id)
    cooldowns = find_collar_cooldowns_for(domain_data, entry.entry_id)
    cooldowns["collar-1"] = 123.0

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        AsyncMock(return_value=False),
    ):
        assert await async_unload_entry(hass, entry) is False

    assert domain_data[entry.entry_id] is stored
    assert control_lock_for(domain_data, entry.entry_id) is original_lock
    assert find_collar_cooldowns_for(domain_data, entry.entry_id) == {"collar-1": 123.0}


async def test_permanent_config_entry_removal_clears_control_state(hass):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    domain_data = hass.data[DOMAIN]
    control_lock_for(domain_data, entry.entry_id)
    find_collar_cooldowns_for(domain_data, entry.entry_id)["collar-1"] = 123.0

    assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
    await hass.async_block_till_done()

    assert entry.entry_id not in domain_data.get("_control_locks", {})
    assert entry.entry_id not in domain_data.get("_find_collar_cooldowns", {})


async def test_runtime_failure_marks_entities_and_controls_unavailable_until_recovery(hass):
    client = ToggleFailureClient(_state())
    entry = _entry(
        {
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ALLOW_FENCE_DISABLE: True,
            CONF_ENABLE_FIND_COLLAR: True,
        }
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    registry = er.async_get(hass)
    tracked = [
        ("sensor", "collar-1_battery"),
        ("binary_sensor", "collar-1_online"),
        ("device_tracker", "collar-1_pet_tracker"),
        ("event", "collar-1_fence_breach_event"),
        ("button", "collar-1_enable_fences"),
        ("button", "collar-1_find_collar"),
        ("switch", "collar-1_fence_mode"),
    ]
    entity_ids = [
        registry.async_get_entity_id(platform, DOMAIN, unique_id) for platform, unique_id in tracked
    ]
    assert all(entity_ids)

    client.error = HaloApiError("synthetic outage")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert all(hass.states.get(entity_id).state == STATE_UNAVAILABLE for entity_id in entity_ids)

    client.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in entity_ids)


async def test_new_collar_reload_preserves_existing_ids_and_restores_removed_collar(hass):
    client = FakeHaloClient(_state())
    entry = _entry(
        {
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ALLOW_FENCE_DISABLE: True,
            CONF_ENABLE_FIND_COLLAR: True,
        }
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    registry = er.async_get(hass)

    def registered_entities():
        return {
            item.unique_id: item.entity_id
            for item in registry.entities.values()
            if item.config_entry_id == entry.entry_id
        }

    existing = registered_entities()
    client.state = _state_with_second_collar()
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    with patch("custom_components.halo_collar.api.HaloApiClient", return_value=client):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    after_add = registered_entities()
    assert all(after_add[key] == entity_id for key, entity_id in existing.items())
    second_collar_entities = [
        ("sensor", "collar-2_battery"),
        ("binary_sensor", "collar-2_online"),
        ("device_tracker", "collar-2_pet_tracker"),
        ("event", "collar-2_fence_breach_event"),
        ("button", "collar-2_enable_fences"),
        ("button", "collar-2_find_collar"),
        ("switch", "collar-2_fence_mode"),
    ]
    second_ids = [
        registry.async_get_entity_id(platform, DOMAIN, unique_id)
        for platform, unique_id in second_collar_entities
    ]
    assert all(second_ids)
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in second_ids)

    client.state = _state()
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert all(hass.states.get(entity_id).state == STATE_UNAVAILABLE for entity_id in second_ids)

    client.state = _state_with_second_collar()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert all(hass.states.get(entity_id).state != STATE_UNAVAILABLE for entity_id in second_ids)
    assert registered_entities() == after_add


async def test_runtime_expiry_only_token_renewal_is_persisted_without_reload(hass):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    renewed_expiry = entry.data[CONF_EXPIRES_AT] + 600
    reload_mock = AsyncMock()
    with patch.object(hass.config_entries, "async_reload", reload_mock):
        client.commit_tokens(
            {
                "access_token": entry.data[CONF_ACCESS_TOKEN],
                "refresh_token": entry.data[CONF_REFRESH_TOKEN],
                "expires_at": renewed_expiry,
            }
        )
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert entry.data[CONF_EXPIRES_AT] == renewed_expiry
    assert entry.data[CONF_ACCESS_TOKEN] == "access"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh"
    reload_mock.assert_not_awaited()


@pytest.fixture
async def real_client_entry(hass, aioclient_mock):
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    state = _state()
    for path, payload in (
        ("/pet/my", state.pets),
        ("/collar/my", state.collars),
        ("/subscription/my", state.subscription),
        ("/system/server-date-time", state.server_time),
        ("/walk/my?page=1&pageSize=30", {"results": []}),
    ):
        aioclient_mock.get(f"{DEFAULT_API_BASE}{path}", json=payload)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(hass.data[DOMAIN][entry.entry_id]["client"], halo_api.HaloApiClient)
    return entry


@pytest.mark.parametrize("same_credentials", [False, True], ids=["different", "identical"])
@pytest.mark.parametrize("unload_first", [False, True], ids=["loaded", "no-runtime"])
async def test_successful_reauth_reloads_once_without_listener_warning(
    hass, real_client_entry, aioclient_mock, monkeypatch, caplog, same_credentials, unload_first
):
    entry = real_client_entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMAIL: "owner@example.com"}
    )
    await hass.async_block_till_done()
    original = dict(entry.data)
    old_client = hass.data[DOMAIN][entry.entry_id]["client"]
    if unload_first:
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.entry_id not in hass.data[DOMAIN]
    now = original[CONF_EXPIRES_AT] - 600
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    expected = {
        CONF_ACCESS_TOKEN: original[CONF_ACCESS_TOKEN] if same_credentials else "reauth-access",
        CONF_REFRESH_TOKEN: original[CONF_REFRESH_TOKEN] if same_credentials else "reauth-refresh",
        CONF_EXPIRES_AT: original[CONF_EXPIRES_AT] if same_credentials else now + 7140,
    }
    aioclient_mock.post(
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": expected[CONF_ACCESS_TOKEN],
            "refresh_token": expected[CONF_REFRESH_TOKEN],
            "expires_in": 660 if same_credentials else 7200,
        },
    )
    states = []
    successful_abort = False
    real_abort = halo_config_flow.HaloCollarConfigFlow.async_abort
    real_reload = hass.config_entries.async_reload

    def observe_abort(flow, **kwargs):
        nonlocal successful_abort
        result = real_abort(flow, **kwargs)
        successful_abort = result["reason"] == "reauth_successful"
        return result

    async def observe_reload(entry_id):
        assert successful_abort
        return await real_reload(entry_id)

    monkeypatch.setattr(halo_config_flow.HaloCollarConfigFlow, "async_abort", observe_abort)
    remove_listener = entry.async_on_state_change(lambda: states.append(entry.state))
    caplog.clear()
    try:
        with patch.object(
            hass.config_entries, "async_reload", side_effect=observe_reload
        ) as reload_spy:
            flow = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                data=entry.data,
            )
            assert flow["type"] is FlowResultType.FORM
            result = await hass.config_entries.flow.async_configure(
                flow["flow_id"],
                {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
            )
            await hass.async_block_till_done()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        reload_spy.assert_awaited_once_with(entry.entry_id)
        assert states.count(ConfigEntryState.LOADED) == 1
        assert entry.state is ConfigEntryState.LOADED
        client = hass.data[DOMAIN][entry.entry_id]["client"]
        assert client is not old_client
        assert {key: client.token_snapshot[key] for key in expected} == expected
        assert dict(entry.data) == {**original, **expected}
        assert not [
            record.getMessage()
            for record in caplog.records
            if "has an update listener and should use it for scheduling a reload"
            in record.getMessage()
        ]
    finally:
        remove_listener()


async def test_queued_inventory_controls_and_reauth_share_one_reload(hass, monkeypatch):
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMAIL: "owner@example.com"}
    )
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    client.state = _state_with_second_collar()
    login = SimpleNamespace(
        async_login=AsyncMock(),
        token_snapshot={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": 4_102_444_900.0,
        },
    )
    monkeypatch.setattr(halo_config_flow, "_client", lambda *args: login)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=entry.data
    )
    snapshots = []

    def build_client(**kwargs):
        snapshots.append({key: kwargs[key] for key in login.token_snapshot})
        return client

    with (
        patch("custom_components.halo_collar.api.HaloApiClient", side_effect=build_client),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_spy,
    ):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_ENABLE_FIND_COLLAR: True}
        )
        coordinator.async_set_updated_data(client.state)
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
        )
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
    reload_spy.assert_awaited_once_with(entry.entry_id)
    assert snapshots == [login.token_snapshot]
    assert entry.state is ConfigEntryState.LOADED
    for collar in ("collar-1", "collar-2"):
        assert (
            _state_for_unique_id(hass, "button", f"{collar}_find_collar").state != STATE_UNAVAILABLE
        )


@pytest.mark.parametrize(
    ("loaded_first", "phase", "reauth"),
    [
        (False, "before-snapshot", True),
        (True, "before-snapshot", True),
        (False, "after-snapshot", True),
        (True, "after-snapshot", True),
        (False, "forwarding", True),
        (True, "forwarding", True),
        (False, "after-snapshot", False),
        (False, "forwarding", False),
    ],
)
async def test_reload_during_setup_uses_latest_credentials_and_controls(
    hass, monkeypatch, loaded_first, phase, reauth
):
    from homeassistant.util.ssl import client_context

    from custom_components.halo_collar import async_request_reload

    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMAIL: "owner@example.com"}
    )
    if loaded_first:
        await _setup(hass, entry, FakeHaloClient(_state()))
    reached = asyncio.Event()
    release = asyncio.Event()
    snapshots = []
    login = SimpleNamespace(
        async_login=AsyncMock(),
        token_snapshot={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": 4_102_444_900.0,
        },
    )
    expected = (
        login.token_snapshot if reauth else {key: entry.data[key] for key in login.token_snapshot}
    )
    monkeypatch.setattr(halo_config_flow, "_client", lambda *args: login)
    real_executor = hass.async_add_executor_job
    real_forward = hass.config_entries.async_forward_entry_setups

    async def hold():
        if not reached.is_set():
            reached.set()
            await release.wait()

    async def executor(target, *args):
        if phase == "before-snapshot" and target is client_context:
            await hold()
        return await real_executor(target, *args)

    async def forward(*args):
        await real_forward(*args)
        if phase == "forwarding":
            await hold()

    def build_client(**kwargs):
        snapshot = {key: kwargs[key] for key in login.token_snapshot}
        snapshots.append(snapshot)
        client = FakeHaloClient(_state())
        client._snapshot = snapshot
        client.on_token_update = kwargs["on_token_update"]
        fetch = client.async_fetch_state

        async def first_refresh():
            if phase == "after-snapshot":
                await hold()
            return await fetch()

        client.async_fetch_state = first_refresh
        return client

    with (
        patch("custom_components.halo_collar.api.HaloApiClient", side_effect=build_client),
        patch.object(hass, "async_add_executor_job", executor),
        patch.object(hass.config_entries, "async_forward_entry_setups", forward),
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload_spy,
    ):
        async_request_reload(hass, entry)
        try:
            async with asyncio.timeout(10):
                await reached.wait()
                if reauth:
                    flow = await hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                        data=entry.data,
                    )
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_ENABLE_FIND_COLLAR: True}
                )
                if reauth:
                    result = await hass.config_entries.flow.async_configure(
                        flow["flow_id"],
                        {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
                    )
                    assert result["reason"] == "reauth_successful"
                release.set()
                await hass.async_block_till_done()
        finally:
            release.set()
    expected_setups = 2 if phase == "forwarding" or (phase == "after-snapshot" and reauth) else 1
    assert len(snapshots) == expected_setups
    assert reload_spy.await_count == expected_setups
    assert snapshots[-1] == expected
    assert hass.data[DOMAIN][entry.entry_id]["client"].token_snapshot == expected
    assert entry.state is ConfigEntryState.LOADED
    assert _state_for_unique_id(hass, "button", "collar-1_find_collar").state != STATE_UNAVAILABLE


async def test_inventory_failed_unload_retains_authority_without_poll_retries(hass, monkeypatch):
    from homeassistant.config_entries import OperationNotAllowed

    from custom_components.halo_collar import async_request_reload

    monkeypatch.setattr(
        "custom_components.halo_collar.controls.time", SimpleNamespace(monotonic=lambda: 123.0)
    )
    client = FakeHaloClient(_state())
    entry = _entry({CONF_ENABLE_FIND_COLLAR: True})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)
    cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
    cooldowns["collar-1"] = 123.0
    client.state = _state_with_second_collar()
    try:
        with (
            patch.object(hass.config_entries, "async_unload_platforms", return_value=False),
            patch.object(
                hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
            ) as reload_spy,
        ):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.FAILED_UNLOAD
            assert hass.data[DOMAIN][entry.entry_id] is stored
            assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is lock
            assert find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id) is cooldowns
            assert cooldowns == {"collar-1": 123.0}
            rotated = {**client.token_snapshot, "refresh_token": "still-authorized"}
            client.commit_tokens(rotated)
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            assert entry.data[CONF_REFRESH_TOKEN] == "still-authorized"
            reload_spy.assert_awaited_once_with(entry.entry_id)
            assert hass.data[DOMAIN]["_reload_requests"] == {}
            tasks = []

            def capture_task(coro, *args, **kwargs):
                task = asyncio.create_task(coro)
                tasks.append(task)
                return task

            with patch.object(hass, "async_create_task", side_effect=capture_task):
                async_request_reload(hass, entry)
                with pytest.raises(OperationNotAllowed):
                    await tasks[0]
            assert reload_spy.await_count == 2
            assert entry.state is ConfigEntryState.FAILED_UNLOAD
            assert hass.data[DOMAIN]["_reload_requests"] == {}
    finally:
        await hass.data[DATA_INSTANCES]["sensor"]._async_reset()
        await coordinator.async_shutdown()


async def test_optional_clock_failure_keeps_telemetry_available_and_diagnostics_network_free(
    hass, real_client_entry, aioclient_mock
):
    entry = real_client_entry
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    state = coordinator.data
    aioclient_mock.clear_requests()
    for path, payload in (
        ("/pet/my", state.pets),
        ("/collar/my", state.collars),
        ("/subscription/my", state.subscription),
        ("/walk/my?page=1&pageSize=30", {"results": []}),
    ):
        aioclient_mock.get(f"{DEFAULT_API_BASE}{path}", json=payload)
    aioclient_mock.get(
        f"{DEFAULT_API_BASE}/system/server-date-time", status=403, text="unavailable"
    )

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert coordinator.data.server_time is None
    battery = _state_for_unique_id(hass, "sensor", "collar-1_battery")
    assert battery.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    calls = list(aioclient_mock.mock_calls)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["server_time"] is None
    assert aioclient_mock.mock_calls == calls
    assert entry.state is ConfigEntryState.LOADED


def _mock_due_token_rotation(entry, aioclient_mock, monkeypatch):
    now = entry.data[CONF_EXPIRES_AT] + 1
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(halo_api, "RETRY_BACKOFF_SECONDS", ())
    aioclient_mock.clear_requests()
    aioclient_mock.post(
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "rotated-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
        },
    )


def _assert_rotated_tokens_persisted(entry, aioclient_mock):
    posts = [call for call in aioclient_mock.mock_calls if call[0].lower() == "post"]
    gets = [call for call in aioclient_mock.mock_calls if call[0].lower() == "get"]
    assert len(posts) == 1
    assert posts[0][2]["refresh_token"] == "refresh"
    assert gets
    assert all(call[3]["Authorization"] == "Bearer rotated-access" for call in gets)
    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh"
    assert entry.data[CONF_ACCESS_TOKEN] == "rotated-access"
    assert entry.data[CONF_EXPIRES_AT] > halo_api.time.time()


async def test_real_token_rotation_persists_after_failed_first_setup(
    hass, aioclient_mock, monkeypatch
):
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    _mock_due_token_rotation(entry, aioclient_mock, monkeypatch)
    aioclient_mock.get(f"{DEFAULT_API_BASE}/pet/my", status=503, text="Unavailable")

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    _assert_rotated_tokens_persisted(entry, aioclient_mock)


async def test_real_token_rotation_persists_after_failed_poll(
    hass, real_client_entry, aioclient_mock, monkeypatch
):
    entry = real_client_entry
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    _mock_due_token_rotation(entry, aioclient_mock, monkeypatch)
    aioclient_mock.get(f"{DEFAULT_API_BASE}/pet/my", status=503, text="Unavailable")

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock:
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert not coordinator.last_update_success
    reload_mock.assert_not_awaited()
    _assert_rotated_tokens_persisted(entry, aioclient_mock)


async def test_real_token_rotation_persists_after_cancelled_poll(
    hass, real_client_entry, aioclient_mock, monkeypatch
):
    entry = real_client_entry
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    _mock_due_token_rotation(entry, aioclient_mock, monkeypatch)
    read_started = asyncio.Event()

    async def wait_for_cancel(method, url, data):
        read_started.set()
        await asyncio.Event().wait()

    aioclient_mock.get(f"{DEFAULT_API_BASE}/pet/my", side_effect=wait_for_cancel)
    task = asyncio.create_task(coordinator.async_refresh())
    try:
        async with asyncio.timeout(5):
            await read_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await hass.async_block_till_done()

    _assert_rotated_tokens_persisted(entry, aioclient_mock)


@pytest.mark.parametrize("release_after_reload", [False, True], ids=["flow-return", "reloaded"])
@pytest.mark.parametrize("refresh_trigger", ["scheduled", "entity-service"])
async def test_reauth_retains_new_tokens_when_refresh_is_in_flight(
    hass, real_client_entry, aioclient_mock, monkeypatch, release_after_reload, refresh_trigger
):
    assert halo_config_flow.HaloApiClient is halo_api.HaloApiClient
    entry = real_client_entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMAIL: "owner@example.com"}
    )
    await hass.async_block_till_done()
    old_client = hass.data[DOMAIN][entry.entry_id]["client"]
    now = entry.data[CONF_EXPIRES_AT] + 1
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    reload_finished = asyncio.Event()
    order = []
    refresh_tasks = []
    service_task = None

    def observe_reload():
        if entry.state is ConfigEntryState.LOADED:
            order.append("real reload completed")
            reload_finished.set()

    remove_state_listener = entry.async_on_state_change(observe_reload)

    async def token_response(method, url, data):
        if data["grant_type"] == "password":
            order.append("new login response")
            return login_response
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh"
        refresh_tasks.append(asyncio.current_task())
        order.append(f"old {refresh_trigger} refresh waiting")
        refresh_started.set()
        try:
            await release_refresh.wait()
        except asyncio.CancelledError:
            order.append("old refresh cancelled")
            refresh_cancelled.set()
            raise
        order.append("old refresh response released")
        return old_response

    old_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "old-rotated-access",
            "refresh_token": "old-rotated-refresh",
            "expires_in": 3600,
        },
        side_effect=token_response,
    )
    login_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "reauth-access",
            "refresh_token": "reauth-refresh",
            "expires_in": 7200,
        },
    )

    try:
        if refresh_trigger == "scheduled":
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
        else:
            assert await async_setup_component(hass, "homeassistant", {})
            entity = _state_for_unique_id(hass, "sensor", "collar-1_battery")
            service_task = asyncio.create_task(
                hass.services.async_call(
                    "homeassistant", "update_entity", {"entity_id": entity.entity_id}, blocking=True
                )
            )
        async with asyncio.timeout(10):
            await refresh_started.wait()
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                data=entry.data,
            )
            assert result["type"] is FlowResultType.FORM
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
            )
            assert result["type"] is FlowResultType.ABORT
            assert result["reason"] == "reauth_successful"
            assert entry.data[CONF_REFRESH_TOKEN] == "reauth-refresh"
            new_credentials = dict(entry.data)
            order.append("reauth flow returned with new credentials")

            if release_after_reload:
                await reload_finished.wait()
                assert entry.state is ConfigEntryState.LOADED
                replacement = hass.data[DOMAIN][entry.entry_id]["client"]
                assert replacement is not old_client
                assert replacement.token_snapshot["refresh_token"] == "reauth-refresh"
            release_refresh.set()
            await asyncio.gather(*refresh_tasks, return_exceptions=True)
            if service_task is not None:
                await service_task
            await hass.async_block_till_done()
            print(f"Reauth event order: {order}; cancelled={refresh_cancelled.is_set()}")

            assert entry.state is ConfigEntryState.LOADED
            assert hass.data[DOMAIN][entry.entry_id]["client"] is not old_client
            assert dict(entry.data) == new_credentials
    finally:
        remove_state_listener()
        release_refresh.set()
        for task in refresh_tasks:
            task.cancel()
        await asyncio.gather(*refresh_tasks, return_exceptions=True)
        if service_task is not None:
            service_task.cancel()
            await asyncio.gather(service_task, return_exceptions=True)


async def test_removed_entry_does_not_accept_in_flight_entity_refresh_tokens(
    hass, real_client_entry, aioclient_mock, monkeypatch, caplog
):
    entry = real_client_entry
    original_data = dict(entry.data)
    now = entry.data[CONF_EXPIRES_AT] + 1
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    order = []

    async def token_response(method, url, data):
        assert data["grant_type"] == "refresh_token"
        order.append("entity-service refresh waiting")
        refresh_started.set()
        try:
            await release_refresh.wait()
        except asyncio.CancelledError:
            order.append("refresh cancelled")
            raise
        order.append("old refresh response released")
        return response

    response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "removed-rotated-access",
            "refresh_token": "removed-rotated-refresh",
            "expires_in": 3600,
        },
        side_effect=token_response,
    )
    assert await async_setup_component(hass, "homeassistant", {})
    entity = _state_for_unique_id(hass, "sensor", "collar-1_battery")
    service_task = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity.entity_id}, blocking=True
        )
    )
    try:
        async with asyncio.timeout(10):
            await refresh_started.wait()
            result = await hass.config_entries.async_remove(entry.entry_id)
            assert result["require_restart"] is False
            assert hass.config_entries.async_get_entry(entry.entry_id) is None
            order.append("entry removed")
            release_refresh.set()
            await service_task
            await hass.async_block_till_done()
            print(f"Removal event order: {order}")
            assert dict(entry.data) == original_data
            assert hass.config_entries.async_get_entry(entry.entry_id) is None
            assert not [
                record
                for record in caplog.records
                if record.name == "custom_components.halo_collar" and record.exc_info
            ]
    finally:
        release_refresh.set()
        service_task.cancel()
        await asyncio.gather(service_task, return_exceptions=True)


async def test_command_token_rotation_persists_before_dispatch_veto(
    hass, real_client_entry, aioclient_mock, monkeypatch
):
    entry = real_client_entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "unrelated_setting": "preserved"}
    )
    await hass.async_block_till_done()
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    _mock_due_token_rotation(entry, aioclient_mock, monkeypatch)
    vetoes = []

    def veto():
        vetoes.append(True)
        assert entry.data[CONF_REFRESH_TOKEN] == client.token_snapshot["refresh_token"]
        assert entry.data[CONF_REFRESH_TOKEN] == "rotated-refresh"
        raise HaloControlError("option revoked during refresh")

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock:
        with pytest.raises(HaloControlError, match="option revoked"):
            await client.async_find_collar("synthetic-collar", pre_dispatch=veto)
        await hass.async_block_till_done()

    assert vetoes == [True]
    assert entry.data[CONF_ACCESS_TOKEN] == "rotated-access"
    assert entry.data["unrelated_setting"] == "preserved"
    assert [call[0].lower() for call in aioclient_mock.mock_calls] == ["post"]
    reload_mock.assert_not_awaited()


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

    reload_mock = AsyncMock()
    with patch.object(hass.config_entries, "async_reload", reload_mock):
        client.commit_tokens(
            {
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_at": 4_102_444_900.0,
            }
        )
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


@pytest.mark.parametrize("same_credentials", [False, True], ids=["different", "identical"])
@pytest.mark.parametrize("release_at", ["before-unload", "after-reload", "failed-unload"])
async def test_reauth_rotation_preserves_entry_runtime_consistency(
    hass, real_client_entry, aioclient_mock, monkeypatch, same_credentials, release_at
):
    entry = real_client_entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_EMAIL: "owner@example.com", "unrelated": "preserved"}
    )
    await hass.async_block_till_done()
    stored = hass.data[DOMAIN][entry.entry_id]
    old_client = stored["client"]
    fields = (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT)
    original = {key: entry.data[key] for key in fields}
    options = dict(entry.options)
    now = original[CONF_EXPIRES_AT] - 600
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    unload_started = asyncio.Event()
    release_unload = asyncio.Event()
    lifecycle_finished = asyncio.Event()
    refresh_requests = []
    aioclient_mock.clear_requests()
    pet_reads = 0

    async def pet_response(method, url, data):
        nonlocal pet_reads
        pet_reads += 1
        return unauthorized if pet_reads == 1 else pets

    unauthorized = aioclient_mock.request(
        "get", f"{DEFAULT_API_BASE}/pet/my", status=401, side_effect=pet_response
    )
    pets = aioclient_mock.request(
        "get", f"{DEFAULT_API_BASE}/pet/my", json=stored["coordinator"].data.pets
    )
    state = stored["coordinator"].data
    for path, payload in (
        ("/collar/my", state.collars),
        ("/subscription/my", state.subscription),
        ("/system/server-date-time", state.server_time),
        ("/walk/my?page=1&pageSize=30", {"results": []}),
    ):
        aioclient_mock.get(f"{DEFAULT_API_BASE}{path}", json=payload)

    async def token_response(method, url, data):
        if data["grant_type"] == "password":
            return login_response
        assert data["grant_type"] == "refresh_token"
        refresh_requests.append(data["refresh_token"])
        if len(refresh_requests) == 1:
            assert data["refresh_token"] == original[CONF_REFRESH_TOKEN]
            refresh_started.set()
            await release_refresh.wait()
            return old_response
        return next_response

    old_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "old-rotated-access",
            "refresh_token": "old-rotated-refresh",
            "expires_in": 3600,
        },
        side_effect=token_response,
    )
    login_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": original[CONF_ACCESS_TOKEN] if same_credentials else "reauth-access",
            "refresh_token": original[CONF_REFRESH_TOKEN] if same_credentials else "reauth-refresh",
            "expires_in": 660 if same_credentials else 7200,
        },
    )
    next_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={"access_token": "next-access", "refresh_token": "next-refresh", "expires_in": 10800},
    )
    real_unload = hass.config_entries.async_unload_platforms

    async def held_unload(*args, **kwargs):
        unload_started.set()
        await release_unload.wait()
        if release_at == "failed-unload":
            return False
        return await real_unload(*args, **kwargs)

    def observe_state():
        if entry.state in (ConfigEntryState.LOADED, ConfigEntryState.FAILED_UNLOAD):
            lifecycle_finished.set()

    remove_listener = entry.async_on_state_change(observe_state)
    assert await async_setup_component(hass, "homeassistant", {})
    entity = _state_for_unique_id(hass, "sensor", "collar-1_battery")
    service_task = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity.entity_id}, blocking=True
        )
    )
    try:
        async with asyncio.timeout(10):
            with patch.object(hass.config_entries, "async_unload_platforms", held_unload):
                await refresh_started.wait()
                flow = await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
                    data=entry.data,
                )
                result = await hass.config_entries.flow.async_configure(
                    flow["flow_id"],
                    {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
                )
                assert result["reason"] == "reauth_successful"
                accepted = {key: entry.data[key] for key in fields}
                if same_credentials:
                    assert accepted == original
                    assert {key: old_client.token_snapshot[key] for key in fields} == original
                else:
                    assert accepted == {
                        CONF_ACCESS_TOKEN: "reauth-access",
                        CONF_REFRESH_TOKEN: "reauth-refresh",
                        CONF_EXPIRES_AT: now + 7140,
                    }
                await unload_started.wait()
                assert hass.data[DOMAIN][entry.entry_id] is stored
                if release_at != "before-unload":
                    release_unload.set()
                    await lifecycle_finished.wait()
                release_refresh.set()
                await service_task
                expected = accepted
                if same_credentials and release_at != "after-reload":
                    expected = {
                        CONF_ACCESS_TOKEN: "old-rotated-access",
                        CONF_REFRESH_TOKEN: "old-rotated-refresh",
                        CONF_EXPIRES_AT: now + 3540,
                    }
                assert {key: entry.data[key] for key in fields} == expected
                if release_at == "before-unload":
                    assert hass.data[DOMAIN][entry.entry_id] is stored
                    release_unload.set()
                    await lifecycle_finished.wait()
                current = hass.data[DOMAIN][entry.entry_id]["client"]
                if release_at == "failed-unload":
                    assert entry.state is ConfigEntryState.FAILED_UNLOAD
                    assert current is old_client
                    if same_credentials:
                        assert {key: current.token_snapshot[key] for key in fields} == expected
                    else:
                        await current.async_refresh_token()
                        assert {key: entry.data[key] for key in fields} == accepted
                        return
                else:
                    assert entry.state is ConfigEntryState.LOADED
                    assert current is not old_client
                    assert {key: current.token_snapshot[key] for key in fields} == expected
                await current.async_refresh_token()
                assert refresh_requests[-1] == expected[CONF_REFRESH_TOKEN]
                assert {key: entry.data[key] for key in fields} == {
                    CONF_ACCESS_TOKEN: "next-access",
                    CONF_REFRESH_TOKEN: "next-refresh",
                    CONF_EXPIRES_AT: now + 10740,
                }
                assert entry.data["unrelated"] == "preserved"
                assert dict(entry.options) == options
            await hass.async_block_till_done()
    finally:
        remove_listener()
        release_refresh.set()
        release_unload.set()
        service_task.cancel()
        await asyncio.gather(service_task, return_exceptions=True)
        if release_at == "failed-unload":
            await stored["coordinator"].async_shutdown()


@pytest.mark.parametrize("lifecycle", ["manual-unload", "failed-unload", "failed-unload-removal"])
async def test_unload_controls_in_flight_token_persistence(
    hass, real_client_entry, aioclient_mock, monkeypatch, caplog, lifecycle
):
    entry = real_client_entry
    stored = hass.data[DOMAIN][entry.entry_id]
    original = dict(entry.data)
    lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)
    cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
    cooldowns["collar-1"] = 123.0
    now = entry.data[CONF_EXPIRES_AT] + 1
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    requests = []

    async def token_response(method, url, data):
        requests.append(data["refresh_token"])
        if len(requests) == 1:
            refresh_started.set()
            await release_refresh.wait()
            return old_response
        return next_response

    old_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={"access_token": "old-access", "refresh_token": "old-refresh", "expires_in": 3600},
        side_effect=token_response,
    )
    next_response = aioclient_mock.request(
        "post",
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={"access_token": "next-access", "refresh_token": "next-refresh", "expires_in": 7200},
    )
    assert await async_setup_component(hass, "homeassistant", {})
    entity = _state_for_unique_id(hass, "sensor", "collar-1_battery")
    task = asyncio.create_task(
        hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": entity.entity_id}, blocking=True
        )
    )
    try:
        async with asyncio.timeout(10):
            await refresh_started.wait()
            if lifecycle == "manual-unload":
                assert await hass.config_entries.async_unload(entry.entry_id)
                assert entry.state is ConfigEntryState.NOT_LOADED
            else:
                with patch.object(
                    hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)
                ):
                    assert not await hass.config_entries.async_unload(entry.entry_id)
                assert entry.state is ConfigEntryState.FAILED_UNLOAD
                assert hass.data[DOMAIN][entry.entry_id] is stored
                if lifecycle == "failed-unload-removal":
                    await hass.config_entries.async_remove(entry.entry_id)
                    assert hass.config_entries.async_get_entry(entry.entry_id) is None
            release_refresh.set()
            await task
            if lifecycle == "failed-unload":
                assert dict(entry.data) == {
                    **original,
                    CONF_ACCESS_TOKEN: "old-access",
                    CONF_REFRESH_TOKEN: "old-refresh",
                    CONF_EXPIRES_AT: now + 3540,
                }
            else:
                assert dict(entry.data) == original
            assert not [
                record
                for record in caplog.records
                if record.name == "custom_components.halo_collar" and record.exc_info
            ]
            if lifecycle == "failed-unload-removal":
                return
            if lifecycle == "manual-unload":
                monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now - 2))
                assert await hass.config_entries.async_setup(entry.entry_id)
            current = hass.data[DOMAIN][entry.entry_id]["client"]
            assert (current is stored["client"]) == (lifecycle == "failed-unload")
            await current.async_refresh_token()
            assert requests == [
                "refresh",
                "old-refresh" if lifecycle == "failed-unload" else "refresh",
            ]
            assert entry.data[CONF_ACCESS_TOKEN] == "next-access"
            assert entry.data[CONF_REFRESH_TOKEN] == "next-refresh"
            assert entry.data[CONF_EXPIRES_AT] == halo_api.time.time() + 7140
            assert control_lock_for(hass.data[DOMAIN], entry.entry_id) is lock
            assert find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id) is cooldowns
            assert cooldowns == {"collar-1": 123.0}
            await hass.async_block_till_done()
    finally:
        release_refresh.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lifecycle != "manual-unload":
            await stored["coordinator"].async_shutdown()


@pytest.mark.parametrize("cancel_setup", [False, True], ids=["failure", "cancellation"])
async def test_incomplete_setup_retires_tokens_and_retry_persists(
    hass, aioclient_mock, monkeypatch, cancel_setup
):
    entry = _entry({CONF_ENABLE_FENCE_CONTROLS: False})
    entry.add_to_hass(hass)
    now = entry.data[CONF_EXPIRES_AT] + 1
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(halo_api, "RETRY_BACKOFF_SECONDS", ())
    read_started = asyncio.Event()
    clients = []
    client_class = halo_api.HaloApiClient

    def build_client(**kwargs):
        client = client_class(**kwargs)
        clients.append(client)
        return client

    async def blocked_read(method, url, data):
        read_started.set()
        await asyncio.Event().wait()

    aioclient_mock.post(
        f"{DEFAULT_AUTH_BASE}/connect/token",
        json={
            "access_token": "committed-access",
            "refresh_token": "committed-refresh",
            "expires_in": 3600,
        },
    )
    aioclient_mock.get(
        f"{DEFAULT_API_BASE}/pet/my",
        status=503,
        **({"side_effect": blocked_read} if cancel_setup else {"text": "Unavailable"}),
    )
    with patch("custom_components.halo_collar.api.HaloApiClient", side_effect=build_client):
        task = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        try:
            async with asyncio.timeout(10):
                if cancel_setup:
                    await read_started.wait()
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert not await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        saved = dict(entry.data)
        assert saved[CONF_ACCESS_TOKEN] == "committed-access"
        assert saved[CONF_REFRESH_TOKEN] == "committed-refresh"
        assert saved[CONF_EXPIRES_AT] == now + 3540
        assert entry.entry_id not in hass.data.get(DOMAIN, {})
        aioclient_mock.clear_requests()
        aioclient_mock.post(
            f"{DEFAULT_AUTH_BASE}/connect/token",
            json={
                "access_token": "retired-access",
                "refresh_token": "retired-refresh",
                "expires_in": 7200,
            },
        )
        await clients[0].async_refresh_token()
        assert dict(entry.data) == saved
        aioclient_mock.clear_requests()
        state = _state()
        for path, payload in (
            ("/pet/my", state.pets),
            ("/collar/my", state.collars),
            ("/subscription/my", state.subscription),
            ("/system/server-date-time", state.server_time),
            ("/walk/my?page=1&pageSize=30", {"results": []}),
        ):
            aioclient_mock.get(f"{DEFAULT_API_BASE}{path}", json=payload)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert len(clients) == 2
        fields = (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_EXPIRES_AT)
        assert {key: clients[1].token_snapshot[key] for key in fields} == {
            key: saved[key] for key in fields
        }
        aioclient_mock.post(
            f"{DEFAULT_AUTH_BASE}/connect/token",
            json={
                "access_token": "retry-access",
                "refresh_token": "retry-refresh",
                "expires_in": 10800,
            },
        )
        await clients[1].async_refresh_token()
        assert entry.data[CONF_ACCESS_TOKEN] == "retry-access"
        assert entry.data[CONF_REFRESH_TOKEN] == "retry-refresh"
        assert entry.data[CONF_EXPIRES_AT] == now + 10740
        assert [
            call[2]["refresh_token"]
            for call in aioclient_mock.mock_calls
            if call[0].lower() == "post"
        ] == ["committed-refresh"]
        await hass.async_block_till_done()


async def test_successive_real_rotations_and_expiry_only_renewal_persist_without_reload(
    hass, real_client_entry, aioclient_mock, monkeypatch
):
    entry = real_client_entry
    hass.config_entries.async_update_entry(entry, data={**entry.data, "unrelated": "preserved"})
    await hass.async_block_till_done()
    options = dict(entry.options)
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    now = entry.data[CONF_EXPIRES_AT] - 600
    monkeypatch.setattr(halo_api, "time", SimpleNamespace(time=lambda: now))
    previous_refresh = "refresh"
    with (
        patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock,
        patch.object(
            hass.config_entries, "async_update_entry", wraps=hass.config_entries.async_update_entry
        ) as update,
    ):
        for index, (access, refresh, expiry) in enumerate(
            (
                ("first-access", "first-refresh", 3600),
                ("second-access", "second-refresh", 7200),
                ("second-access", "second-refresh", 10800),
                ("second-access", "second-refresh", 10800),
            )
        ):
            aioclient_mock.clear_requests()
            aioclient_mock.post(
                f"{DEFAULT_AUTH_BASE}/connect/token",
                json={
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_in": expiry,
                },
            )
            await client.async_refresh_token()
            assert len(aioclient_mock.mock_calls) == 1
            assert aioclient_mock.mock_calls[0][2]["refresh_token"] == previous_refresh
            assert entry.data[CONF_ACCESS_TOKEN] == access
            assert entry.data[CONF_REFRESH_TOKEN] == refresh
            assert entry.data[CONF_EXPIRES_AT] == now + expiry - 60
            assert update.call_count == min(index + 1, 3)
            previous_refresh = refresh
        await hass.async_block_till_done()
    reload_mock.assert_not_awaited()
    assert entry.data["unrelated"] == "preserved"
    assert dict(entry.options) == options


@pytest.mark.parametrize(
    "field,value",
    [
        (CONF_ACCESS_TOKEN, "external-access"),
        (CONF_REFRESH_TOKEN, "external-refresh"),
        (CONF_EXPIRES_AT, 4_102_444_810.0),
    ],
)
async def test_observed_credential_mismatch_permanently_retires_writer(hass, field, value):
    client = FakeHaloClient(_state())
    entry = _entry({})
    entry.add_to_hass(hass)
    await _setup(hass, entry, client)
    original = dict(entry.data)
    hass.config_entries.async_update_entry(entry, data={**original, field: value})
    client.commit_tokens(
        {
            "access_token": "late-access",
            "refresh_token": "late-refresh",
            "expires_at": 4_102_448_400.0,
        }
    )
    assert dict(entry.data) == {**original, field: value}
    hass.config_entries.async_update_entry(entry, data=original)
    client.commit_tokens(
        {
            "access_token": "late-access",
            "refresh_token": "late-refresh",
            "expires_at": 4_102_448_400.0,
        }
    )
    assert dict(entry.data) == original
