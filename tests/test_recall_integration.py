from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from custom_components.halo_collar.api import (
    HaloApiError,
    HaloRecallAccepted,
    HaloState,
    HaloWriteOutcomeUnknown,
)
from custom_components.halo_collar.config_flow import _REGISTRATION_ERRORS
from custom_components.halo_collar.const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_CONFIRM_RECALL_REGISTRATION,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_ENABLE_RECALL_WHISTLE,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    DOMAIN,
)
from custom_components.halo_collar.controls import control_lock_for
from custom_components.halo_collar.diagnostics import async_get_config_entry_diagnostics
from custom_components.halo_collar.recall_runtime import RecallFileStore, existing_recall_owner

OPTIONS = {
    CONF_SCAN_INTERVAL: 300,
    CONF_STALE_AFTER: 900,
    CONF_ENABLE_FENCE_CONTROLS: False,
    CONF_ENABLE_FIND_COLLAR: False,
    CONF_ALLOW_FENCE_DISABLE: False,
}


def _dog(number: int) -> tuple[dict, dict]:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    pet = {
        "id": f"pet-{number}",
        "name": f"Synthetic {number}",
        "collarInfo": {"id": f"collar-{number}", "telemetry": {"walk": None}},
    }
    collar = {
        "id": f"collar-{number}",
        "serialNumber": f"serial-{number}",
        "petInfo": {"id": f"pet-{number}", "name": f"Synthetic {number}"},
        "firmware": {"features": ["correctionmethodswithidandexpiration"]},
        "telemetry": {"manifest": {"timestamp": now}, "walk": None},
    }
    return pet, collar


def _state(dogs: int = 2) -> HaloState:
    pets, collars = zip(*(_dog(number) for number in range(1, dogs + 1)), strict=True)
    return HaloState(
        pets=list(pets),
        collars=list(collars),
        subscription={},
        server_time="2026-11-17T12:00:00Z",
    )


class FakeRecallClient:
    def __init__(self, state: HaloState, clock: list[float]) -> None:
        self.state = state
        self.clock = clock
        self.fetch_error: Exception | None = None
        self.sent: list[tuple[str, int]] = []
        self.registrations: list[tuple[object, str]] = []
        self.registration_error: Exception | None = None
        self.on_token_update = None
        self.token_snapshot = {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 4_102_444_800.0,
        }

    async def async_fetch_state(self) -> HaloState:
        if self.fetch_error is not None:
            raise self.fetch_error
        state = deepcopy(self.state)
        state.server_time_monotonic = self.clock[0]
        return state

    async def async_get_recall_window(self) -> timedelta:
        return timedelta(seconds=30)

    async def async_send_recall_whistle(self, pet_id, command, *, pre_dispatch):
        pre_dispatch()
        self.sent.append((pet_id, command.command_number))
        return HaloRecallAccepted()

    async def async_register_recall_device(self, installation_id, *, version_string, pre_dispatch):
        pre_dispatch()
        self.registrations.append((installation_id, version_string))
        if self.registration_error is not None:
            raise self.registration_error
        return 7


@pytest.fixture(autouse=True)
def _private_config_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture
def clock(monkeypatch):
    value = [1_000.0]
    monkeypatch.setattr(
        "custom_components.halo_collar.recall.time",
        SimpleNamespace(monotonic=lambda: value[0]),
    )
    return value


@pytest.fixture
def client(clock):
    value = FakeRecallClient(_state(), clock)

    def build_client(**kwargs):
        value.on_token_update = kwargs["on_token_update"]
        return value

    with patch("custom_components.halo_collar.api.HaloApiClient", side_effect=build_client):
        yield value


def _entry(**options) -> MockConfigEntry:
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
        options={**OPTIONS, **options},
    )


async def _setup(hass, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _enable_recall(hass, entry, *, confirm: bool = True):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, CONF_ENABLE_RECALL_WHISTLE: True}
    )
    if result["type"] is not FlowResultType.FORM:
        return result
    assert result["step_id"] == "recall_registration"
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_RECALL_REGISTRATION: confirm}
    )


async def _registered(hass) -> MockConfigEntry:
    entry = _entry()
    await _setup(hass, entry)
    result = await _enable_recall(hass, entry)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return entry


def _entity_id(hass, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def _state_file(hass, entry) -> Path:
    return Path(hass.config.path(".storage", f"{DOMAIN}.recall.{entry.entry_id}"))


async def _press(hass, pet_id: str = "pet-1") -> None:
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": _entity_id(hass, "button", f"{pet_id}_recall_whistle")},
        blocking=True,
    )
    await hass.async_block_till_done()


def _status(hass, pet_id: str = "pet-1") -> str:
    return hass.states.get(_entity_id(hass, "sensor", f"{pet_id}_recall_whistle_status")).state


async def test_default_install_has_no_recall_surface_requests_or_state(hass, client):
    entry = _entry()
    await _setup(hass, entry)

    assert _entity_id(hass, "button", "pet-1_recall_whistle") is None
    assert _entity_id(hass, "sensor", "pet-1_recall_whistle_status") is None
    assert existing_recall_owner(hass, entry) is None
    assert not _state_file(hass, entry).exists()
    assert (client.sent, client.registrations) == ([], [])


async def test_registration_needs_confirmation_then_adds_one_button_per_dog(hass, client):
    entry = _entry()
    await _setup(hass, entry)

    result = await _enable_recall(hass, entry, confirm=False)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "registration_not_confirmed"}
    assert client.registrations == []

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_RECALL_REGISTRATION: True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ENABLE_RECALL_WHISTLE] is True
    assert [version for _, version in client.registrations] == [HA_VERSION]
    saved = json.loads(_state_file(hass, entry).read_text())
    assert saved["registration"] == {"state": "registered", "mobile_id": 7, "next_by_serial": {}}
    assert "owner@example.com" not in json.dumps(saved)
    devices = dr.async_get(hass)
    for number in (1, 2):
        button = _entity_id(hass, "button", f"pet-{number}_recall_whistle")
        assert hass.states.get(button).state != STATE_UNAVAILABLE
        device = devices.async_get(er.async_get(hass).async_get(button).device_id)
        assert device.identifiers == {(DOMAIN, f"collar-{number}")}
        assert _status(hass, f"pet-{number}") == "ready"


async def test_press_sends_one_whistle_and_cools_down_per_collar(hass, client, clock):
    entry = await _registered(hass)

    await _press(hass)

    assert client.sent == [("pet-1", 0)]
    assert _status(hass) == "cooldown"
    assert _status(hass, "pet-2") == "ready"
    saved = json.loads(_state_file(hass, entry).read_text())
    assert saved["registration"]["next_by_serial"] == {"serial-1": 1}
    with pytest.raises(HomeAssistantError, match="cooldown"):
        await _press(hass)

    clock[0] += 61
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert _status(hass) == "ready"

    await _press(hass)
    assert client.sent == [("pet-1", 0), ("pet-1", 1)]


async def test_refusals_raise_while_the_button_stays_callable(hass, client):
    entry = await _registered(hass)
    button = _entity_id(hass, "button", "pet-1_recall_whistle")

    client.state.collars[0]["telemetry"]["walk"] = {"startTime": "now"}
    with pytest.raises(HomeAssistantError, match="active walk"):
        await _press(hass)
    assert hass.states.get(button).state != STATE_UNAVAILABLE

    client.fetch_error = HaloApiError("synthetic outage")
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(button).state != STATE_UNAVAILABLE
    assert _status(hass) == "cloud_unavailable"
    with pytest.raises(HomeAssistantError, match="Could not refresh"):
        await _press(hass)

    assert client.sent == []


@pytest.mark.parametrize("both_sides", [False, True], ids=["pet_side", "both_sides"])
async def test_dog_that_lost_its_collar_keeps_a_button_that_raises(hass, client, both_sides):
    entry = await _registered(hass)
    button = _entity_id(hass, "button", "pet-1_recall_whistle")
    client.state.pets[0].pop("collarInfo")
    if both_sides:
        client.state.collars[0].pop("petInfo")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(button).state != STATE_UNAVAILABLE
    assert er.async_get(hass).async_get(button).device_id is None
    assert _status(hass) == "mapping_unavailable"
    with pytest.raises(HomeAssistantError, match="mapping is unavailable"):
        await _press(hass)
    assert client.sent == []


async def test_collar_device_targets_the_dog_wearing_it_after_a_swap(hass, client):
    entry = await _registered(hass)
    registry = er.async_get(hass)

    def device_of(pet: int) -> str:
        return registry.async_get(_entity_id(hass, "button", f"pet-{pet}_recall_whistle")).device_id

    collar_device = {1: device_of(1), 2: device_of(2)}
    pet_1, pet_2 = client.state.pets
    collar_1, collar_2 = client.state.collars
    pet_1["collarInfo"], pet_2["collarInfo"] = pet_2["collarInfo"], pet_1["collarInfo"]
    collar_1["petInfo"], collar_2["petInfo"] = collar_2["petInfo"], collar_1["petInfo"]

    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()
    await hass.services.async_call(
        "button", "press", {"device_id": collar_device[1]}, blocking=True
    )

    assert client.sent == [("pet-2", 0)]
    assert (device_of(1), device_of(2)) == (collar_device[2], collar_device[1])


def _start_walk(client, number: int = 1) -> None:
    client.state.pets[number - 1]["collarInfo"]["telemetry"]["walk"] = {"startTime": "now"}
    client.state.collars[number - 1]["telemetry"]["walk"] = {"startTime": "now"}


async def test_press_paused_across_a_reload_sends_nothing(hass, client, monkeypatch):
    entry = await _registered(hass)
    saving = asyncio.Event()
    release = asyncio.Event()
    save = RecallFileStore.async_save

    async def slow_number_save(self, data):
        if data["registration"].get("next_by_serial"):
            saving.set()
            await release.wait()
        await save(self, data)

    monkeypatch.setattr(RecallFileStore, "async_save", slow_number_save)
    press = asyncio.create_task(_press(hass))
    try:
        await asyncio.wait_for(saving.wait(), 5)
        assert await hass.config_entries.async_reload(entry.entry_id)
        _start_walk(client)
    finally:
        release.set()

    with pytest.raises(HomeAssistantError, match="reloaded"):
        await press
    assert client.sent == []


async def test_press_queued_across_a_reload_sends_nothing(hass, client):
    entry = await _registered(hass)
    lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)

    await lock.acquire()
    try:
        press = asyncio.create_task(_press(hass, "pet-2"))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not press.done()
        assert await hass.config_entries.async_reload(entry.entry_id)
        _start_walk(client, 2)
    finally:
        lock.release()

    with pytest.raises(HomeAssistantError, match="reloaded"):
        await press
    assert client.sent == []


async def test_ambiguous_registration_keeps_recall_off_and_is_never_resent(hass, client):
    entry = _entry()
    await _setup(hass, entry)
    client.registration_error = HaloWriteOutcomeUnknown("lost reply")

    result = await _enable_recall(hass, entry)
    assert result["errors"] == {"base": "registration_uncertain"}
    assert CONF_ENABLE_RECALL_WHISTLE not in entry.options
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONFIRM_RECALL_REGISTRATION: True}
    )

    client.registration_error = None
    result = await _enable_recall(hass, entry)

    assert result["errors"] == {"base": "registration_uncertain"}
    assert len(client.registrations) == 1
    assert json.loads(_state_file(hass, entry).read_text())["registration"]["state"] == (
        "uncertain"
    )


@pytest.mark.parametrize("content", ["null", "[]", '"registered"', "7", "", "{"])
async def test_unreadable_state_file_never_permits_a_registration(hass, client, content):
    entry = _entry()
    _state_file(hass, entry).parent.mkdir(parents=True, exist_ok=True)
    _state_file(hass, entry).write_text(content, encoding="utf-8")
    await _setup(hass, entry)

    result = await _enable_recall(hass, entry)

    assert result["errors"] == {"base": "recall_storage_unavailable"}
    assert client.registrations == []
    assert _state_file(hass, entry).read_text(encoding="utf-8") == content


async def test_registration_needs_a_loaded_entry(hass, client):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await _enable_recall(hass, entry)

    assert result["errors"] == {"base": "integration_not_loaded"}
    assert client.registrations == []


async def test_reenabling_a_registered_entry_skips_registration(hass, client):
    entry = await _registered(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS, CONF_ENABLE_RECALL_WHISTLE: False}
    )
    await hass.async_block_till_done()
    disabled = hass.states.get(_entity_id(hass, "button", "pet-1_recall_whistle"))
    assert (disabled.state, disabled.attributes.get("restored")) == (STATE_UNAVAILABLE, True)

    result = await _enable_recall(hass, entry)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(client.registrations) == 1
    assert _status(hass) == "ready"


async def test_diagnostics_show_only_the_registration_state(hass, client):
    entry = await _registered(hass)
    await _press(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["recall_whistle_registration"] == "registered"
    serialized = json.dumps(diagnostics)
    assert "mobile" not in serialized.lower()
    assert "serial-1" not in serialized
    assert "next_by_serial" not in serialized


async def test_entry_removal_forgets_local_recall_state(hass, client):
    entry = await _registered(hass)
    assert _state_file(hass, entry).exists()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert not _state_file(hass, entry).exists()
    assert existing_recall_owner(hass, entry) is None


async def test_dog_newly_bound_to_a_collar_gets_recall_entities_after_one_reload(hass, client):
    unbound_pet, unbound_collar = _dog(2)
    unbound_pet.pop("collarInfo")
    unbound_collar.pop("petInfo")
    client.state.pets[1], client.state.collars[1] = unbound_pet, unbound_collar
    entry = await _registered(hass)
    assert _entity_id(hass, "button", "pet-2_recall_whistle") is None

    client.state.pets[1], client.state.collars[1] = _dog(2)
    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()

    assert _status(hass, "pet-2") == "ready"


def test_every_registration_error_is_translated():
    root = Path(__file__).resolve().parents[1] / "custom_components" / "halo_collar"
    errors = json.loads((root / "strings.json").read_text())["options"]["error"]
    flow_errors = {*_REGISTRATION_ERRORS.values(), "registration_not_confirmed"}
    assert flow_errors | {"integration_not_loaded"} == set(errors)
