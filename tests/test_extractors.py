from __future__ import annotations

from datetime import UTC, datetime

from custom_components.halo_collar.helpers import (
    indoors_on_wifi,
    is_online,
    last_telemetry,
    sensor_values,
)


def test_sensor_extractors_cover_live_payload_shape():
    collar = {
        "firmware": {"formattedVersion": "03.06.64"},
        "petInfo": {"telemetry": {"gpsAccuracyInMeters": 10.01, "gpsAccuracyStatus": "indoors"}},
        "telemetry": {
            "batteryChargePercent": 80,
            "batteryStatus": "notcharged",
            "remainingBatteryLifetimeInSeconds": 117411,
            "currentAdapter": "wifi",
            "wiFi": {"status": "socketconnected", "signalStrength": -70},
            "cellular": {"status": "disconnected", "signalStrength": -87},
        },
    }
    values = sensor_values(collar)

    assert values["battery"] == 80
    assert values["battery_status"] == "Not charging"
    assert values["remaining_battery_lifetime"] == 32.6
    assert values["current_adapter"] == "Wi-Fi"
    assert values["wifi_status"] == "Connected"
    assert values["cellular_signal"] == -87
    assert values["gps_accuracy_status"] == "Indoors"
    assert values["firmware"] == "03.06.64"


def test_online_uses_manifest_timestamp_freshness():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}}}

    assert is_online(collar) is True


def test_last_telemetry_parses_manifest_timestamp():
    collar = {"telemetry": {"manifest": {"timestamp": "2026-07-06T12:00:00Z"}}}

    assert last_telemetry(collar) == datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    assert sensor_values(collar)["last_telemetry"] == datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    assert last_telemetry({}) is None


def test_indoors_on_wifi_requires_both_signals():
    indoors_wifi = {
        "petInfo": {"telemetry": {"gpsAccuracyStatus": "indoors"}},
        "telemetry": {"currentAdapter": "wifi"},
    }
    indoors_cellular = {
        "petInfo": {"telemetry": {"gpsAccuracyStatus": "indoors"}},
        "telemetry": {"currentAdapter": "cellular"},
    }
    outdoors_wifi = {
        "petInfo": {"telemetry": {"gpsAccuracyStatus": "outdoors"}},
        "telemetry": {"currentAdapter": "wifi"},
    }

    assert indoors_on_wifi(indoors_wifi) is True
    assert indoors_on_wifi(indoors_cellular) is False
    assert indoors_on_wifi(outdoors_wifi) is False
    assert indoors_on_wifi({}) is False
