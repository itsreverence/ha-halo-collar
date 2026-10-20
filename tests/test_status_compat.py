from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.halo_collar.api import HaloState
from custom_components.halo_collar.button import HaloFindCollarButton
from custom_components.halo_collar.const import CONF_ENABLE_FIND_COLLAR
from custom_components.halo_collar.control_status import find_status_signal
from custom_components.halo_collar.controls import HaloControlError
from custom_components.halo_collar.sensor import HaloControlStatusSensor


@pytest.mark.parametrize("rejected", [False, True])
async def test_minimum_ha_dispatcher_isolates_status_listener_from_real_button_result(
    tmp_path, rejected
):
    hass = HomeAssistant(str(tmp_path))
    if hasattr(device_registry, "async_setup"):
        device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    state = HaloState(
        pets=[
            {
                "id": "pet-1",
                "collarInfo": {"id": "collar-1", "telemetry": {"walk": None}},
            }
        ],
        collars=[
            {
                "id": "collar-1",
                "petInfo": {"id": "pet-1"},
                "telemetry": {
                    "manifest": {"timestamp": datetime.now(UTC).isoformat()},
                    "walk": None,
                },
            }
        ],
        subscription={"features": [{"id": "findcollar", "isEnabled": True}]},
        server_time=None,
    )
    fetches = []
    dispatches = []

    async def fetch():
        fetches.append(True)
        return state

    async def find(collar_id, *, pre_dispatch):
        if rejected:
            raise HaloControlError("synthetic rejection")
        pre_dispatch()
        dispatches.append(collar_id)

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), config_entry=None, name="synthetic", update_method=fetch
    )
    coordinator.async_set_updated_data(state)
    entry = SimpleNamespace(entry_id="entry-1", options={CONF_ENABLE_FIND_COLLAR: True})
    cooldowns = {}
    sensor = HaloControlStatusSensor(coordinator, entry, state.collars[0], "find_collar", cooldowns)
    button = HaloFindCollarButton(
        coordinator,
        entry,
        SimpleNamespace(async_find_collar=find),
        state.collars[0],
        asyncio.Lock(),
        cooldowns,
    )
    sensor_component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    button_component = EntityComponent(logging.getLogger(__name__), "button", hass)
    await sensor_component.async_setup({})
    await button_component.async_setup({})
    await sensor_component.async_add_entities([sensor])
    await button_component.async_add_entities([button])

    @callback
    def broken_listener():
        raise RuntimeError("synthetic advisory failure")

    remove = async_dispatcher_connect(
        hass, find_status_signal(entry.entry_id, "collar-1"), broken_listener
    )
    try:
        assert hass.states.get(sensor.entity_id).state == "ready"
        if rejected:
            with pytest.raises(HomeAssistantError, match="synthetic rejection"):
                await button.async_press()
        else:
            await button.async_press()
        await hass.async_block_till_done()
        assert dispatches == ([] if rejected else ["collar-1"])
        assert len(fetches) == (1 if rejected else 2)
        assert hass.states.get(sensor.entity_id).state == ("ready" if rejected else "cooldown")
    finally:
        remove()
        await button_component._async_reset()
        await sensor_component._async_reset()
        await coordinator.async_shutdown()
        await hass.async_stop(force=True)
