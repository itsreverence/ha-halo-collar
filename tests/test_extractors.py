from __future__ import annotations

from datetime import UTC, datetime

from custom_components.halo_collar.helpers import (
    REDACTED,
    active_walk_distance,
    active_walk_duration,
    active_walk_paused,
    active_walk_state,
    activity_value,
    average_connectivity,
    count_goal_progress_attributes,
    count_value,
    current_fence_name,
    fence_configuration_status,
    fence_disable_block_reason,
    firmware_update_available,
    goal_progress_attributes,
    has_active_walk,
    indoors_on_wifi,
    is_online,
    last_telemetry,
    latest_completed_walk,
    next_expected_telemetry,
    pet_fences_enabled,
    pet_for_collar,
    pet_safety_status,
    redact,
    reporting_issue,
    sensor_values,
)


def test_latest_completed_walk_selects_newest_matching_safe_summary():
    walks = [
        {
            "startedAt": "2026-07-06T09:00:00Z",
            "endedAt": "2026-07-06T09:10:00Z",
            "startTrigger": "scheduled_walk",
            "pets": [{"id": "pet-other", "walkedDurationInSeconds": 600}],
        },
        {
            "startedAt": "2026-07-06T11:00:00Z",
            "endedAt": "2026-07-06T11:07:00Z",
            "startTrigger": "button",
            "pets": [
                {
                    "id": "pet-1",
                    "walkedDurationInSeconds": 420,
                    "walkedDistanceInMeters": 125.5,
                }
            ],
        },
        {
            "endedAt": "2026-07-06T10:30:00Z",
            "pets": [{"id": "pet-1", "walkedDurationInSeconds": 120, "walkedDistanceInMeters": 40}],
        },
    ]

    assert latest_completed_walk(walks, "pet-1") == {
        "ended_at": datetime(2026, 7, 6, 11, 7, tzinfo=UTC),
        "started_at": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
        "duration": 420,
        "distance": 125.5,
        "start_trigger": "Button",
    }


def test_latest_completed_walk_omits_unknown_start_trigger() -> None:
    summary = latest_completed_walk(
        [
            {
                "endedAt": "2026-07-06T11:07:00Z",
                "startTrigger": "PRIVATE_LOCATION_SENTINEL",
                "pets": [{"id": "pet-1"}],
            }
        ],
        "pet-1",
    )

    assert summary is not None
    assert summary["start_trigger"] is None


def test_latest_completed_walk_rejects_ambiguous_or_malformed_entries_without_raising():
    invalid_walks = [
        {
            "endedAt": "2026-07-06T11:00:00Z",
            "pets": [{"id": "pet-1"}, {"id": "pet-1"}],
        },
        {"endedAt": "not-a-time", "pets": [{"id": "pet-1"}]},
        {"endedAt": "2026-07-06T11:00:00Z", "pets": "invalid"},
    ]

    assert latest_completed_walk(invalid_walks, "pet-1") is None


def test_latest_completed_walk_keeps_newest_structural_record_when_optional_fields_are_invalid():
    summary = latest_completed_walk(
        [
            {
                "startedAt": "2026-07-06T11:30:00Z",
                "endedAt": "2026-07-06T11:00:00Z",
                "startTrigger": "PRIVATE_LOCATION_SENTINEL",
                "pets": [
                    {
                        "id": "pet-1",
                        "walkedDurationInSeconds": True,
                        "walkedDistanceInMeters": float("inf"),
                    }
                ],
            },
            {
                "startedAt": "2026-07-06T10:00:00Z",
                "endedAt": "2026-07-06T10:15:00Z",
                "startTrigger": "button",
                "pets": [
                    {
                        "id": "pet-1",
                        "walkedDurationInSeconds": 900,
                        "walkedDistanceInMeters": 150,
                    }
                ],
            },
        ],
        "pet-1",
    )

    assert summary == {
        "ended_at": datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
        "started_at": None,
        "duration": None,
        "distance": None,
        "start_trigger": None,
    }


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


def test_pet_mapping_fails_closed_when_two_collars_claim_one_pet():
    pets = [{"id": "pet-1", "telemetry": {"mode": {"fencesOn": True}}}]
    collars = [
        {"id": "collar-1", "petInfo": {"id": "pet-1"}},
        {"id": "collar-2", "petInfo": {"id": "pet-1"}},
    ]

    assert pet_for_collar(pets, collars[0], collars) is None
    assert pet_for_collar(pets, collars[1], collars) is None


def test_active_walk_state_distinguishes_present_null_from_unknown_walk_telemetry():
    pet = {"collarInfo": {"telemetry": {"walk": {"synthetic": True}}}}
    assert active_walk_state(pet, {}) is True
    assert active_walk_state({}, {"telemetry": {"walk": "invalid"}}) is None
    idle_pet = {"collarInfo": {"telemetry": {"walk": None}}}
    assert active_walk_state(idle_pet, {"telemetry": {"walk": None}}) is False
    assert active_walk_state({}, {"telemetry": {"walk": None}}) is None
    assert (
        active_walk_state({"collarInfo": {"telemetry": []}}, {"telemetry": {"walk": None}}) is None
    )


def test_active_walk_state_fails_safe_on_conflicting_or_malformed_snapshots():
    active_pet = {"collarInfo": {"telemetry": {"walk": {"synthetic": True}}}}
    idle_collar = {"telemetry": {"walk": None}}
    assert active_walk_state(active_pet, idle_collar) is True

    malformed_pet = {"collarInfo": {"telemetry": {"walk": "invalid"}}}
    assert active_walk_state(malformed_pet, idle_collar) is None


def test_has_active_walk_fails_closed_when_walk_telemetry_is_unknown():
    active_pet = {"collarInfo": {"telemetry": {"walk": {"synthetic": True}}}}
    idle_pet = {"collarInfo": {"telemetry": {"walk": None}}}
    assert has_active_walk(active_pet, {}) is True
    assert has_active_walk(idle_pet, {"telemetry": {"walk": None}}) is False
    assert has_active_walk({}, {"telemetry": {"walk": None}}) is True
    assert has_active_walk({"collarInfo": {"telemetry": []}}, {"telemetry": {"walk": None}}) is True


def test_active_walk_detail_extractors_prefer_direct_collar_snapshot_and_fail_soft():
    pet_walk = {
        "durationFromStartInSeconds": 50,
        "walkedDistance": 100,
        "isPaused": False,
        "id": "PRIVATE_WALK_ID",
    }
    collar_walk = {
        "durationFromStartInSeconds": 60,
        "walkedDistance": 125.5,
        "isPaused": True,
        "location": "PRIVATE_LOCATION_SENTINEL",
    }
    pet = {"collarInfo": {"telemetry": {"walk": pet_walk}}}
    collar = {"telemetry": {"walk": collar_walk}}

    assert active_walk_duration(pet, collar) == 60
    assert active_walk_distance(pet, collar) == 125.5
    assert active_walk_paused(pet, collar) is True

    idle_pet = {"collarInfo": {"telemetry": {"walk": None}}}
    idle_collar = {"telemetry": {"walk": None}}
    assert active_walk_duration(idle_pet, idle_collar) is None
    assert active_walk_distance(idle_pet, idle_collar) is None
    assert active_walk_paused(idle_pet, idle_collar) is False

    fallback_collar = {"telemetry": {"walk": None}}
    assert active_walk_duration(pet, fallback_collar) == 50
    assert active_walk_distance(pet, fallback_collar) == 100
    assert active_walk_paused(pet, fallback_collar) is False

    assert active_walk_duration({}, {}) is None
    assert active_walk_distance({}, {}) is None
    assert active_walk_paused({}, {}) is None


def test_active_walk_detail_extractors_reject_malformed_provider_values():
    idle_pet = {"collarInfo": {"telemetry": {"walk": None}}}
    for malformed in (True, "bad", -1, float("nan"), float("inf")):
        collar = {
            "telemetry": {
                "walk": {
                    "durationFromStartInSeconds": malformed,
                    "walkedDistance": malformed,
                    "isPaused": False,
                }
            }
        }
        assert active_walk_duration(idle_pet, collar) is None
        assert active_walk_distance(idle_pet, collar) is None

    for malformed in (0, 1, "false", None, [], {}):
        collar = {"telemetry": {"walk": {"isPaused": malformed}}}
        assert active_walk_paused(idle_pet, collar) is None


def test_activity_and_goal_extractors_are_fail_soft_and_bounded():
    pet = {
        "metrics": {
            "activityDurationInSec": 900,
            "activityDurationGoalInSec": 600,
            "outdoorTimeInSec": 120,
            "outdoorTimeGoalInSec": 0,
            "traveledDistance": 1234.5,
            "traveledDistanceGoal": 2000,
            "walksCount": 2,
            "walksCountGoal": 4,
        }
    }

    assert activity_value(pet, "activityDurationInSec") == 900
    assert goal_progress_attributes(pet, "activityDurationInSec", "activityDurationGoalInSec") == {
        "goal": 600,
        "progress_percent": 100,
    }
    assert goal_progress_attributes(pet, "outdoorTimeInSec", "outdoorTimeGoalInSec") == {"goal": 0}
    assert goal_progress_attributes(pet, "traveledDistance", "traveledDistanceGoal") == {
        "goal": 2000,
        "progress_percent": 61.7,
    }
    assert goal_progress_attributes(pet, "walksCount", "walksCountGoal") == {
        "goal": 4,
        "progress_percent": 50,
    }
    assert count_value(pet, "walksCount") == 2
    assert count_goal_progress_attributes(pet, "walksCount", "walksCountGoal") == {
        "goal": 4,
        "progress_percent": 50,
    }

    for malformed in (True, "bad", float("nan"), float("inf"), -1):
        assert activity_value({"metrics": {"value": malformed}}, "value") is None
        assert (
            goal_progress_attributes({"metrics": {"value": 1, "goal": malformed}}, "value", "goal")
            == {}
        )

    for malformed in (True, -1, -1.0, 1.5, float("nan"), float("inf"), "bad", "1.5"):
        assert count_value({"metrics": {"value": malformed}}, "value") is None
    assert count_value({"metrics": {"value": "2"}}, "value") == 2


def test_insight_extractors_expose_only_allowlisted_values_and_fail_soft():
    collar = {
        "diagnostics": {"averageConnectivityPercent": 99.94},
        "issues": {"hasReportingIssue": True},
        "hasFirmwareUpdatesAvailable": False,
        "telemetry": {
            "manifest": {"timestamp": "2026-07-06T12:00:00Z"},
            "secondsToNextTelemetry": 300,
        },
    }
    pet = {
        "fencesState": "upToDate",
        "telemetry": {"geoFence": {"name": "Synthetic Fence", "id": "do-not-expose"}},
    }

    assert current_fence_name(pet) == "Synthetic Fence"
    assert fence_configuration_status(pet) == "Up to date"
    assert fence_configuration_status({"fencesState": "allApplied"}) == "All applied"
    assert average_connectivity(collar) == 99.9
    assert next_expected_telemetry(collar) == datetime(2026, 7, 6, 12, 5, tzinfo=UTC)
    assert reporting_issue(collar) is True
    assert firmware_update_available(collar) is False

    malformed = {"telemetry": {"secondsToNextTelemetry": -1}}
    assert current_fence_name({"telemetry": {"geoFence": {"name": 3}}}) is None
    assert fence_configuration_status({"fencesState": {}}) is None
    assert average_connectivity({"diagnostics": {"averageConnectivityPercent": True}}) is None
    assert next_expected_telemetry(malformed) is None
    assert reporting_issue({"issues": {"hasReportingIssue": "true"}}) is None
    assert firmware_update_available({"hasFirmwareUpdatesAvailable": 1}) is None


def test_next_expected_telemetry_returns_none_for_finite_overflowing_intervals():
    assert (
        next_expected_telemetry(
            {
                "telemetry": {
                    "manifest": {"timestamp": "2026-07-06T12:00:00Z"},
                    "secondsToNextTelemetry": 1e300,
                }
            }
        )
        is None
    )
    assert (
        next_expected_telemetry(
            {
                "telemetry": {
                    "manifest": {"timestamp": "9999-12-31T23:59:59+00:00"},
                    "secondsToNextTelemetry": 1,
                }
            }
        )
        is None
    )


def test_fence_disable_preflight_requires_fresh_synchronized_reported_state():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}, "walk": None}}
    pet = {
        "collarInfo": {"telemetry": {"walk": None}},
        "isFencesSynchronized": True,
        "telemetry": {"mode": {"fencesOn": True}},
    }

    assert fence_disable_block_reason(pet, collar, stale_after=900) is None
    assert (
        fence_disable_block_reason({**pet, "isFencesSynchronized": False}, collar, stale_after=900)
        == "Halo has not confirmed synchronized fence state"
    )
    assert (
        fence_disable_block_reason({**pet, "telemetry": {}}, collar, stale_after=900)
        == "Halo has not reported current fence mode"
    )
    assert (
        fence_disable_block_reason(
            {
                **pet,
                "collarInfo": {"telemetry": {"walk": {"id": "walk-1"}}},
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


def test_fence_disable_preflight_rejects_malformed_non_null_walk_telemetry():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}, "walk": None}}
    pet = {
        "collarInfo": {"telemetry": {"walk": None}},
        "isFencesSynchronized": True,
        "telemetry": {"mode": {"fencesOn": True}},
    }

    for pet_walk, collar_walk in (("malformed", None), (None, [])):
        blocked_pet = {**pet, "collarInfo": {"telemetry": {"walk": pet_walk}}}
        blocked_collar = {**collar, "telemetry": {**collar["telemetry"], "walk": collar_walk}}
        assert (
            fence_disable_block_reason(blocked_pet, blocked_collar, stale_after=900)
            == "Halo fences cannot be disabled during an active walk"
        )


def test_fence_disable_preflight_blocks_when_walk_telemetry_is_missing_or_malformed():
    collar = {"telemetry": {"manifest": {"timestamp": datetime.now(UTC).isoformat()}}}
    pet = {
        "isFencesSynchronized": True,
        "telemetry": {"mode": {"fencesOn": True}},
    }

    assert (
        fence_disable_block_reason(pet, collar, stale_after=900)
        == "Halo fences cannot be disabled during an active walk"
    )


def test_malformed_telemetry_timestamps_fail_closed_without_exceptions():
    for value in (12345, {}, [], "not-a-date", "2026-07-15T12:00:00"):
        collar = {"telemetry": {"manifest": {"timestamp": value}}}
        assert last_telemetry(collar) is None
        assert is_online(collar) is False
        assert (
            fence_disable_block_reason(
                {
                    "isFencesSynchronized": True,
                    "telemetry": {"mode": {"fencesOn": True}},
                },
                collar,
                stale_after=900,
            )
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


def test_online_allows_small_clock_skew_but_rejects_far_future_telemetry():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    modest_future = {"telemetry": {"manifest": {"timestamp": "2026-07-06T12:04:00Z"}}}
    far_future = {"telemetry": {"manifest": {"timestamp": "2026-07-07T12:00:00Z"}}}

    assert is_online(modest_future, now=now) is True
    assert is_online(far_future, now=now) is False


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
