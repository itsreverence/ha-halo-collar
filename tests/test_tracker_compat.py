from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

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
