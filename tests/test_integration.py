from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

if sys.version_info < (3, 14, 2):
    pytest.skip(
        "Home Assistant 2026.7.2 requires Python 3.14.2+",
        allow_module_level=True,
    )

if sys.version_info >= (3, 14, 2):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.const import STATE_ON, STATE_UNAVAILABLE
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
    timestamp = datetime.now(UTC).isoformat()
    pet = {
        "id": "pet-1",
        "name": "Cowboy",
        "collarInfo": {"id": "collar-1"},
        "isFencesSynchronized": True,
        "telemetry": {
            "mode": {"fencesOn": True},
            "walk": None,
            "safetyStatus": "safe",
        },
    }
    collar = {
        "id": "collar-1",
        "type": "Halo 4",
        "serialNumber": "serial-1",
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
            "batteryChargePercent": 90,
            "currentAdapter": "wifi",
        },
    }
    return HaloState(
        pets=[pet],
        collars=[collar],
        subscription={"accessLevel": "basic"},
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
