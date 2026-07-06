from __future__ import annotations

from datetime import UTC, datetime

from custom_components.halo_collar.helpers import is_online, sensor_values


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
    assert values["wifi_status"] == "socketconnected"
    assert values["cellular_signal"] == -87
    assert values["gps_accuracy_status"] == "indoors"
    assert values["firmware"] == "03.06.64"


def test_online_uses_manifest_timestamp_freshness():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}}}

    assert is_online(collar) is True
