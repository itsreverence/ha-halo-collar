from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.device_tracker import TrackerEntity

from custom_components.halo_collar.device_tracker import HaloPetTracker


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
