from __future__ import annotations

import asyncio
import ssl
from inspect import signature
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.device_tracker import TrackerEntity

from custom_components.halo_collar.config_flow import HaloCollarConfigFlow
from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
)
from custom_components.halo_collar.device_tracker import HaloPetTracker
from custom_components.halo_collar.transport import HaloWriteTransport


def _tracker(*, indoors: bool) -> HaloPetTracker:
    status = "indoors" if indoors else "outdoors"
    collar = {
        "id": "collar-1",
        "petInfo": {
            "id": "pet-1",
            "telemetry": {"gpsAccuracyStatus": status},
        },
        "telemetry": {"currentAdapter": "wifi"},
    }
    tracker = object.__new__(HaloPetTracker)
    tracker._collar_id = collar["id"]
    tracker.coordinator = SimpleNamespace(data=SimpleNamespace(collars=[collar]))
    return tracker


def test_indoor_home_mapping_matches_installed_tracker_api():
    indoor = _tracker(indoors=True)
    outdoor = _tracker(indoors=False)

    if hasattr(TrackerEntity, "in_zones"):
        assert indoor.in_zones == ["zone.home"]
        assert outdoor.in_zones is None
        assert "location_name" not in HaloPetTracker.__dict__
    else:
        assert indoor.location_name == "home"
        assert outdoor.location_name is None
        assert "in_zones" not in HaloPetTracker.__dict__


def test_options_flow_opens_against_minimum_ha_api():
    entry = SimpleNamespace(entry_id="entry-1", options={})
    flow = HaloCollarConfigFlow.async_get_options_flow(entry)
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None
        )
    )
    flow.handler = entry.entry_id
    flow.context = {}

    result = asyncio.run(flow.async_step_init())

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert flow.config_entry is entry
    assert result["data_schema"]({}) == {
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL_SECONDS,
        CONF_STALE_AFTER: DEFAULT_STALE_AFTER_SECONDS,
        CONF_ENABLE_FENCE_CONTROLS: False,
        CONF_ENABLE_FIND_COLLAR: False,
        CONF_ALLOW_FENCE_DISABLE: False,
    }


async def test_write_transport_accepts_home_assistant_verified_tls_context():
    from homeassistant.util.ssl import client_context

    context = await asyncio.to_thread(client_context)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    HaloWriteTransport(ssl_context=context, default_headers={}, trust_env=False)


@pytest.mark.parametrize("unique_id", ["owner@example.com", None])
async def test_reauth_account_guard_uses_minimum_ha_flow_api(unique_id):
    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id=unique_id,
        title="Halo Collar",
        data={"email": " Owner@Example.COM "},
    )
    flow = HaloCollarConfigFlow()
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: entry,
            async_get_known_entry=lambda entry_id: entry,
        )
    )
    flow.context = {"entry_id": entry.entry_id, "source": "reauth"}

    assert (await flow.async_step_reauth(entry.data))["type"] == "form"
    with patch("custom_components.halo_collar.config_flow._client") as factory:
        result = await flow.async_step_reauth_confirm(
            {"email": "other@example.com", "password": "synthetic-password"}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"email": "account_mismatch"}
    factory.assert_not_called()
    assert entry.unique_id == unique_id


@pytest.mark.parametrize("failure_stage", ["first-refresh", "platform-forwarding"])
@pytest.mark.parametrize("cancel_setup", [False, True], ids=["failure", "cancellation"])
async def test_setup_explicitly_retires_writer_without_framework_cleanup(
    monkeypatch, failure_stage, cancel_setup
):
    from homeassistant.config_entries import ConfigEntry

    from custom_components.halo_collar import async_setup_entry

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="halo_collar",
        title="Halo Collar",
        source="user",
        unique_id="owner@example.com",
        discovery_keys={},
        **(
            {"subentries_data": {}}
            if "subentries_data" in signature(ConfigEntry).parameters
            else {}
        ),
        options={},
        data={
            "access_token": "initial-access",
            "refresh_token": "initial-refresh",
            "expires_at": 100.0,
        },
    )
    clients = []
    reached_failure = asyncio.Event()
    fail_setup = True
    updates = []

    def save(current, *, data):
        assert current is entry
        updates.append(data)
        object.__setattr__(entry, "data", data)

    async def fail():
        reached_failure.set()
        if cancel_setup:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic setup failure")

    def build_client(**kwargs):
        snapshot = {key: kwargs[key] for key in ("access_token", "refresh_token", "expires_at")}
        clients.append(SimpleNamespace(snapshot=snapshot, persist=kwargs["on_token_update"]))
        return clients[-1]

    def build_coordinator(*args, **kwargs):
        client = clients[-1]

        async def first_refresh():
            client.persist(
                {
                    "access_token": "committed-access",
                    "refresh_token": "committed-refresh",
                    "expires_at": 200.0,
                }
            )
            if fail_setup and failure_stage == "first-refresh":
                await fail()

        return SimpleNamespace(
            async_config_entry_first_refresh=first_refresh,
            data=SimpleNamespace(collars=[]),
            async_add_listener=lambda listener: lambda: None,
        )

    async def forward(*args):
        if fail_setup and failure_stage == "platform-forwarding":
            await fail()

    async def executor(*args):
        return await asyncio.to_thread(ssl.create_default_context)

    session = SimpleNamespace(headers={}, trust_env=False)
    hass = SimpleNamespace(
        data={},
        async_add_executor_job=executor,
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: entry,
            async_update_entry=save,
            async_forward_entry_setups=forward,
        ),
    )
    monkeypatch.setattr(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession", lambda hass: session
    )
    monkeypatch.setattr(
        "homeassistant.helpers.update_coordinator.DataUpdateCoordinator", build_coordinator
    )
    monkeypatch.setattr("custom_components.halo_collar.api.HaloApiClient", build_client)
    task = asyncio.create_task(async_setup_entry(hass, entry))
    try:
        async with asyncio.timeout(10):
            await reached_failure.wait()
            if cancel_setup:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(RuntimeError, match="synthetic setup failure"):
                    await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    saved = dict(entry.data)
    assert saved == {
        "access_token": "committed-access",
        "refresh_token": "committed-refresh",
        "expires_at": 200.0,
    }
    clients[0].persist(
        {"access_token": "late-access", "refresh_token": "late-refresh", "expires_at": 300.0}
    )
    assert dict(entry.data) == saved
    assert len(updates) == 1
    fail_setup = False
    assert await async_setup_entry(hass, entry)
    assert clients[1].snapshot == saved
    clients[1].persist(
        {"access_token": "retry-access", "refresh_token": "retry-refresh", "expires_at": 400.0}
    )
    assert dict(entry.data) == {
        "access_token": "retry-access",
        "refresh_token": "retry-refresh",
        "expires_at": 400.0,
    }
