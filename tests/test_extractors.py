from __future__ import annotations

from datetime import UTC, datetime

from custom_components.halo_collar.helpers import (
    REDACTED,
    fence_disable_block_reason,
    has_active_walk,
    indoors_on_wifi,
    is_online,
    last_telemetry,
    pet_fences_enabled,
    pet_for_collar,
    pet_safety_status,
    redact,
    sensor_values,
)


def test_pet_mapping_and_control_state_use_live_payload_relationships():
    pets = [
        {
            "id": "pet-1",
            "collarInfo": {"id": "collar-1"},
            "telemetry": {
                "mode": {"fencesOn": True},
                "safetyStatus": "safe",
            },
            "desiredMode": {"fencesOn": True},
            "isFencesSynchronized": True,
        }
    ]
    collar = {"id": "collar-1", "petInfo": {"id": "pet-1"}}

    pet = pet_for_collar(pets, collar)

    assert pet is pets[0]
    assert pet_fences_enabled(pet) is True
    assert pet_safety_status(pet) == "Safe"


def test_pet_mapping_and_control_state_fail_closed_when_unknown():
    assert pet_for_collar([], {"id": "collar-1"}) is None
    assert pet_fences_enabled(None) is None
    assert pet_fences_enabled({"desiredMode": {"fencesOn": True}}) is None
    assert pet_safety_status(None) is None


def test_pet_mapping_fails_closed_on_conflicting_relationships():
    pets = [
        {"id": "wrong-pet", "collarInfo": {"id": "collar-1"}},
        {"id": "pet-1", "collarInfo": {"id": "collar-1"}},
    ]
    collar = {"id": "collar-1", "petInfo": {"id": "pet-1"}}

    assert pet_for_collar(pets, collar) is None


def test_pet_mapping_fails_closed_on_duplicate_pet_ids():
    pets = [
        {"id": "pet-1", "collarInfo": {"id": "collar-1"}},
        {"id": "pet-1", "collarInfo": {"id": "collar-2"}},
    ]
    collar = {"id": "collar-1", "petInfo": {"id": "pet-1"}}

    assert pet_for_collar(pets, collar) is None


def test_active_walk_is_detected_from_either_payload():
    assert has_active_walk({"telemetry": {"walk": {"id": "walk-1"}}}, {}) is True
    assert has_active_walk({}, {"telemetry": {"walk": {"id": "walk-1"}}}) is True
    assert has_active_walk({"telemetry": {"walk": None}}, {}) is False


def test_fence_disable_preflight_requires_fresh_synchronized_reported_state():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}, "walk": None}}
    pet = {
        "isFencesSynchronized": True,
        "telemetry": {"mode": {"fencesOn": True}, "walk": None},
    }

    assert fence_disable_block_reason(pet, collar, stale_after=900) is None
    assert (
        fence_disable_block_reason({**pet, "isFencesSynchronized": False}, collar, stale_after=900)
        == "Halo has not confirmed synchronized fence state"
    )
    assert (
        fence_disable_block_reason({**pet, "telemetry": {"walk": None}}, collar, stale_after=900)
        == "Halo has not reported current fence mode"
    )
    assert (
        fence_disable_block_reason(
            {
                **pet,
                "telemetry": {"mode": {"fencesOn": True}, "walk": {"id": "walk-1"}},
            },
            collar,
            stale_after=900,
        )
        == "Halo fences cannot be disabled during an active walk"
    )
    stale_collar = {"telemetry": {"manifest": {"timestamp": "2026-01-01T00:00:00+00:00"}}}
    assert (
        fence_disable_block_reason(pet, stale_collar, stale_after=900)
        == "Halo collar telemetry is stale"
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


def test_online_honors_custom_stale_after_threshold():
    reported = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    now = datetime(2026, 7, 6, 12, 30, tzinfo=UTC)  # 1800 seconds later
    collar = {"telemetry": {"manifest": {"timestamp": reported.isoformat()}}}

    assert is_online(collar, now=now) is False  # default 900s threshold
    assert is_online(collar, now=now, stale_after=3600) is True


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


def test_redact_masks_sensitive_collar_payload_without_mutating_input():
    collar = {
        "serialNumber": "FAKE-SERIAL-001",
        "deviceId": "fake-device-uuid",
        "petInfo": {
            "name": "FakePet",
            "location": {"latitude": 40.7128, "longitude": -74.0060},
            "lastLocation": {"latitude": 40.7130, "longitude": -74.0058},
        },
        "telemetry": {
            "batteryChargePercent": 72,
            "currentAdapter": "wifi",
            "wiFi": {"ssid": "FakeHomeWiFi", "wifiName": "FakeHomeWiFi", "signalStrength": -65},
        },
        "contacts": [
            {"firstName": "Fake", "lastName": "Owner", "phoneNumber": "555-0100"},
            {"email": "fake.owner@example.test", "address": "123 Fake St"},
        ],
    }
    original = {
        "serialNumber": collar["serialNumber"],
        "deviceId": collar["deviceId"],
        "petInfo": {
            "name": collar["petInfo"]["name"],
            "location": dict(collar["petInfo"]["location"]),
            "lastLocation": dict(collar["petInfo"]["lastLocation"]),
        },
        "telemetry": {
            "batteryChargePercent": collar["telemetry"]["batteryChargePercent"],
            "currentAdapter": collar["telemetry"]["currentAdapter"],
            "wiFi": dict(collar["telemetry"]["wiFi"]),
        },
        "contacts": [
            dict(collar["contacts"][0]),
            dict(collar["contacts"][1]),
        ],
    }

    result = redact(collar)

    assert result["serialNumber"] == REDACTED
    assert result["deviceId"] == REDACTED
    assert result["petInfo"]["name"] == REDACTED
    # Keys matching the redaction list are masked wholesale, containers included.
    assert result["petInfo"]["location"] == REDACTED
    assert result["petInfo"]["lastLocation"] == REDACTED
    assert result["contacts"][0]["firstName"] == REDACTED
    assert result["contacts"][0]["phoneNumber"] == REDACTED
    assert result["contacts"][1]["email"] == REDACTED
    assert result["contacts"][1]["address"] == REDACTED
    assert result["telemetry"]["batteryChargePercent"] == 72
    assert result["telemetry"]["currentAdapter"] == "wifi"
    assert result["telemetry"]["wiFi"]["signalStrength"] == -65
    assert result["telemetry"]["wiFi"]["ssid"] == REDACTED
    assert result["telemetry"]["wiFi"]["wifiName"] == REDACTED
    assert collar == original


def test_redact_is_case_insensitive_on_keys():
    payload = {
        "ACCESS_TOKEN": "fake-access-token",
        "Refresh_Token": "fake-refresh-token",
        "CLIENT_SECRET": "fake-client-secret",
        "scan_interval": 300,
    }

    result = redact(payload)

    assert result["ACCESS_TOKEN"] == REDACTED
    assert result["Refresh_Token"] == REDACTED
    assert result["CLIENT_SECRET"] == REDACTED
    assert result["scan_interval"] == 300
