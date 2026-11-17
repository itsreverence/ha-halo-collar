from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry, frame
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from custom_components.halo_collar import recall as recall_module
from custom_components.halo_collar.api import HaloRecallAccepted, HaloState
from custom_components.halo_collar.button import HaloFindCollarButton, HaloRecallWhistleButton
from custom_components.halo_collar.const import CONF_ENABLE_FIND_COLLAR, CONF_ENABLE_RECALL_WHISTLE
from custom_components.halo_collar.control_status import find_status_signal
from custom_components.halo_collar.controls import HaloControlError
from custom_components.halo_collar.recall import RecallOwner
from custom_components.halo_collar.recall_runtime import RecallFileStore
from custom_components.halo_collar.sensor import HaloControlStatusSensor, HaloRecallStatusSensor


@pytest.mark.parametrize("rejected", [False, True])
async def test_minimum_ha_dispatcher_isolates_status_listener_from_real_button_result(
    tmp_path, rejected
):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    if hasattr(frame, "async_setup"):
        frame.async_setup(hass)
    if hasattr(device_registry, "async_setup"):
        device_registry.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
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
        return deepcopy(state)

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
        lambda: True,
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


async def test_minimum_ha_recall_press_saves_its_number_and_reports_cooldown(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    if hasattr(frame, "async_setup"):
        frame.async_setup(hass)
    if hasattr(device_registry, "async_setup"):
        device_registry.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    clock = [1000.0]
    monkeypatch.setattr(recall_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    state = HaloState(
        pets=[{"id": "pet-1", "collarInfo": {"id": "collar-1", "telemetry": {"walk": None}}}],
        collars=[
            {
                "id": "collar-1",
                "serialNumber": "serial-1",
                "petInfo": {"id": "pet-1"},
                "firmware": {"features": ["CorrectionMethodsWithIdAndExpiration"]},
                "telemetry": {
                    "manifest": {"timestamp": datetime.now(UTC).isoformat()},
                    "walk": None,
                },
            }
        ],
        subscription={},
        server_time="2026-11-17T12:00:00Z",
        server_time_monotonic=clock[0],
    )
    sent = []

    async def fetch():
        fresh = deepcopy(state)
        fresh.server_time_monotonic = clock[0]
        return fresh

    async def window():
        return timedelta(minutes=1)

    async def send(pet_id, command, *, pre_dispatch):
        pre_dispatch()
        sent.append((pet_id, command.command_number))
        return HaloRecallAccepted()

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), config_entry=None, name="synthetic", update_method=fetch
    )
    coordinator.async_set_updated_data(state)
    entry = SimpleNamespace(entry_id="entry-1", options={CONF_ENABLE_RECALL_WHISTLE: True})
    store = RecallFileStore(hass, entry.entry_id)
    await store.async_save(
        {
            "version": 1,
            "account": "account-binding",
            "registration": {"state": "registered", "mobile_id": 7, "next_by_serial": {}},
        }
    )
    recall = RecallOwner(store=store, account="account-binding", lock=asyncio.Lock())
    await recall.async_load()
    clock[0] += 60
    client = SimpleNamespace(async_get_recall_window=window, async_send_recall_whistle=send)
    sensor = HaloRecallStatusSensor(coordinator, entry, state.collars[0], "pet-1", recall)
    button = HaloRecallWhistleButton(
        coordinator, entry, client, state.collars[0], "pet-1", recall, lambda: True
    )
    sensor_component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    button_component = EntityComponent(logging.getLogger(__name__), "button", hass)
    await sensor_component.async_setup({})
    await button_component.async_setup({})
    await sensor_component.async_add_entities([sensor])
    await button_component.async_add_entities([button])
    try:
        assert hass.states.get(sensor.entity_id).state == "ready"
        await button.async_press()
        await hass.async_block_till_done()
        assert sent == [("pet-1", 0)]
        assert hass.states.get(sensor.entity_id).state == "cooldown"
        saved = json.loads(Path(tmp_path, ".storage", "halo_collar.recall.entry-1").read_text())
        assert saved["registration"]["next_by_serial"] == {"serial-1": 1}
        with pytest.raises(HomeAssistantError, match="cooldown"):
            await button.async_press()
        assert sent == [("pet-1", 0)]
    finally:
        await button_component._async_reset()
        await sensor_component._async_reset()
        await coordinator.async_shutdown()
        await hass.async_stop(force=True)
