from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

REDACTED = "**REDACTED**"

REDACT_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "email",
        "token",
        "serialnumber",
        "latitude",
        "longitude",
        "location",
        "lastlocation",
        "address",
        "phone",
        "phonenumber",
        "name",
        "firstname",
        "lastname",
        "userid",
        "accountid",
        "deviceid",
        "imei",
        "iccid",
        "ssid",
        "macaddress",
        "wifiname",
    }
)

STALE_AFTER_SECONDS = 900
MAX_FUTURE_TELEMETRY_SKEW_SECONDS = 300

_STATUS_LABELS = {
    "notcharged": "Not charging",
    "charging": "Charging",
    "charged": "Charged",
    "fullycharged": "Fully charged",
    "wifi": "Wi-Fi",
    "cellular": "Cellular",
    "bluetooth": "Bluetooth",
    "ble": "Bluetooth",
    "socketconnected": "Connected",
    "connected": "Connected",
    "disconnected": "Disconnected",
    "indoors": "Indoors",
    "outdoors": "Outdoors",
    "uptodate": "Up to date",
    "allapplied": "All applied",
    "unknown": "Unknown",
    "noissue": "No issue",
}

_WALK_START_TRIGGER_LABELS = {
    "button": "Button",
}


class WalkSummary(TypedDict):
    ended_at: datetime
    started_at: datetime | None
    duration: int | None
    distance: float | None
    start_trigger: str | None


def redact(data: Any, keys: frozenset[str] = REDACT_KEYS) -> Any:
    """Recursively replace values of sensitive keys with '**REDACTED**'.

    A matching key is redacted wholesale even when its value is a container
    (e.g. ``location: {...}``), so unknown nested key names cannot leak.
    """
    if isinstance(data, dict):
        return {
            key: (REDACTED if isinstance(key, str) and key.lower() in keys else redact(value, keys))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item, keys) for item in data]
    return data


def telemetry(collar: dict[str, Any], key: str) -> Any:
    return collar.get("telemetry", {}).get(key)


def nested(collar: dict[str, Any], *keys: str) -> Any:
    value: Any = collar
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def pet_for_collar(
    pets: list[dict[str, Any]],
    collar: dict[str, Any],
    collars: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Match one pet to one collar, failing closed on snapshot-wide conflicts."""
    pet_id = nested(collar, "petInfo", "id")
    collar_id = collar.get("id")

    if collars is not None:
        if not collar_id or sum(item.get("id") == collar_id for item in collars) != 1:
            return None
        if pet_id:
            claimants = [item for item in collars if nested(item, "petInfo", "id") == pet_id]
            if len(claimants) != 1 or claimants[0].get("id") != collar_id:
                return None

    if pet_id:
        pet_matches = [pet for pet in pets if pet.get("id") == pet_id]
        if len(pet_matches) != 1:
            return None
        pet_match = pet_matches[0]
        linked_collar_id = nested(pet_match, "collarInfo", "id")
        if linked_collar_id and collar_id and linked_collar_id != collar_id:
            return None
        conflicting = any(
            pet.get("id") != pet_id and collar_id and nested(pet, "collarInfo", "id") == collar_id
            for pet in pets
        )
        return None if conflicting else pet_match

    if not collar_id:
        return None
    collar_matches = [pet for pet in pets if nested(pet, "collarInfo", "id") == collar_id]
    if len(collar_matches) != 1:
        return None
    pet_match = collar_matches[0]
    if collars is not None and pet_match.get("id"):
        claimants = [
            item for item in collars if nested(item, "petInfo", "id") == pet_match.get("id")
        ]
        if claimants and (len(claimants) != 1 or claimants[0].get("id") != collar_id):
            return None
    return pet_match


def pet_fences_enabled(pet: dict[str, Any] | None) -> bool | None:
    """Return only the collar-reported fence mode, never desired state."""
    reported = nested(pet or {}, "telemetry", "mode", "fencesOn")
    return reported if isinstance(reported, bool) else None


def pet_safety_status(pet: dict[str, Any] | None) -> Any:
    """Return current pet safety status from the live pet telemetry payload."""
    return pretty_status(nested(pet or {}, "telemetry", "safetyStatus"))


def active_walk_state(pet: dict[str, Any] | None, collar: dict[str, Any] | None) -> bool | None:
    """Return the two authoritative collar walk snapshots as a tri-state value."""
    payloads = (
        nested(pet or {}, "collarInfo", "telemetry"),
        nested(collar or {}, "telemetry"),
    )
    walks = [
        payload.get("walk")
        for payload in payloads
        if isinstance(payload, dict) and "walk" in payload
    ]
    if any(isinstance(walk, dict) for walk in walks):
        return True
    if any(walk is not None for walk in walks):
        return None
    if len(walks) == len(payloads):
        return False
    return None


def has_active_walk(pet: dict[str, Any] | None, collar: dict[str, Any] | None) -> bool:
    """Fail closed unless both provider payloads explicitly report no active walk."""
    return active_walk_state(pet, collar) is not False


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def latest_completed_walk(walks: list[dict[str, Any]], pet_id: Any) -> WalkSummary | None:
    """Return the newest privacy-safe completed-walk summary for one pet."""
    if not isinstance(pet_id, str) or not pet_id or not isinstance(walks, list):
        return None

    newest: WalkSummary | None = None
    for walk in walks:
        if not isinstance(walk, dict):
            continue
        ended_at = parse_timestamp(walk.get("endedAt"))
        pets = walk.get("pets")
        if ended_at is None or not isinstance(pets, list):
            continue
        matches = [item for item in pets if isinstance(item, dict) and item.get("id") == pet_id]
        if len(matches) != 1:
            continue
        pet = matches[0]

        started_at = parse_timestamp(walk.get("startedAt"))
        if started_at is not None and started_at > ended_at:
            started_at = None

        duration = non_negative_integer(pet.get("walkedDurationInSeconds"))

        distance = non_negative_number(pet.get("walkedDistanceInMeters"))

        raw_trigger = walk.get("startTrigger")
        trigger = (
            _WALK_START_TRIGGER_LABELS.get(raw_trigger) if isinstance(raw_trigger, str) else None
        )

        summary: WalkSummary = {
            "ended_at": ended_at,
            "started_at": started_at,
            "duration": duration,
            "distance": distance,
            "start_trigger": trigger,
        }
        if newest is None or ended_at > newest["ended_at"]:
            newest = summary
    return newest


def last_telemetry(collar: dict[str, Any]) -> datetime | None:
    """Return when the collar last reported telemetry to the Halo cloud."""
    return parse_timestamp(nested(collar, "telemetry", "manifest", "timestamp"))


def is_online(
    collar: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_after: float = STALE_AFTER_SECONDS,
) -> bool:
    timestamp = last_telemetry(collar)
    if timestamp is None:
        return False
    age = ((now or datetime.now(UTC)) - timestamp).total_seconds()
    return -MAX_FUTURE_TELEMETRY_SKEW_SECONDS <= age <= stale_after


def fence_disable_block_reason(
    pet: dict[str, Any] | None,
    collar: dict[str, Any] | None,
    *,
    stale_after: float,
) -> str | None:
    """Fail-closed preflight for the containment-disabling command."""
    if pet is None or collar is None:
        return "Halo pet/collar mapping is unavailable"
    if not is_online(collar, stale_after=stale_after):
        return "Halo collar telemetry is stale"
    if pet.get("isFencesSynchronized") is not True:
        return "Halo has not confirmed synchronized fence state"
    if pet_fences_enabled(pet) is None:
        return "Halo has not reported current fence mode"
    if has_active_walk(pet, collar):
        return "Halo fences cannot be disabled during an active walk"
    return None


def indoors_on_wifi(collar: dict[str, Any]) -> bool:
    """Return True when the collar reports it is indoors on its configured Wi-Fi.

    GPS is unreliable indoors, so this is used by the device tracker to pin the
    pet to Home instead of drifting on a jittery fix.
    """
    status = nested(collar, "petInfo", "telemetry", "gpsAccuracyStatus")
    adapter = telemetry(collar, "currentAdapter")
    return (
        isinstance(status, str)
        and status.strip().lower() == "indoors"
        and isinstance(adapter, str)
        and adapter.strip().lower() == "wifi"
    )


def pretty_status(value: Any) -> Any:
    """Return human-readable labels for compact Halo API status strings."""
    if not isinstance(value, str):
        return value
    compact = value.strip()
    if not compact:
        return None
    return _STATUS_LABELS.get(compact.lower(), compact.replace("_", " ").title())


def seconds_to_hours(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / 3600, 1)
    except (TypeError, ValueError):
        return None


def finite_number(value: Any) -> float | None:
    """Return a finite numeric value, rejecting booleans and malformed input."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def non_negative_number(value: Any) -> float | None:
    """Return a finite non-negative numeric value."""
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


def non_negative_integer(value: Any) -> int | None:
    """Return a non-negative count, rejecting booleans and ambiguous numeric values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdecimal():
            return int(normalized)
    return None


def activity_value(pet: dict[str, Any] | None, key: str) -> float | None:
    """Extract a non-negative current-period activity metric from a pet payload."""
    return non_negative_number(nested(pet or {}, "metrics", key))


def count_value(pet: dict[str, Any] | None, key: str) -> int | None:
    """Extract a non-negative integral current-period count from a pet payload."""
    return non_negative_integer(nested(pet or {}, "metrics", key))


def goal_progress_attributes(
    pet: dict[str, Any] | None, value_key: str, goal_key: str
) -> dict[str, float]:
    """Return safe goal and bounded progress attributes for an activity metric."""
    goal = activity_value(pet, goal_key)
    if goal is None:
        return {}
    attributes: dict[str, float] = {"goal": goal}
    value = activity_value(pet, value_key)
    if value is not None and goal > 0:
        attributes["progress_percent"] = round(min(100, value / goal * 100), 1)
    return attributes


def count_goal_progress_attributes(
    pet: dict[str, Any] | None, value_key: str, goal_key: str
) -> dict[str, int | float]:
    """Return safe integral count goals and bounded progress for a count metric."""
    goal = count_value(pet, goal_key)
    if goal is None:
        return {}
    attributes: dict[str, int | float] = {"goal": goal}
    value = count_value(pet, value_key)
    if value is not None and goal > 0:
        attributes["progress_percent"] = round(min(100, value / goal * 100), 1)
    return attributes


def current_fence_name(pet: dict[str, Any] | None) -> str | None:
    """Return the current fence name only; never expose its provider identifier."""
    value = nested(pet or {}, "telemetry", "geoFence", "name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def fence_configuration_status(pet: dict[str, Any] | None) -> str | None:
    """Return the human-readable reported fence configuration status."""
    value = pretty_status(nested(pet or {}, "fencesState"))
    return value if isinstance(value, str) else None


def average_connectivity(collar: dict[str, Any]) -> float | None:
    """Return the reported average connectivity percentage when valid."""
    value = non_negative_number(nested(collar, "diagnostics", "averageConnectivityPercent"))
    return min(100, round(value, 1)) if value is not None else None


def next_expected_telemetry(collar: dict[str, Any]) -> datetime | None:
    """Calculate the next expected report from the last report and provider interval."""
    timestamp = last_telemetry(collar)
    seconds = non_negative_number(nested(collar, "telemetry", "secondsToNextTelemetry"))
    if timestamp is None or seconds is None:
        return None
    try:
        return timestamp + timedelta(seconds=seconds)
    except OverflowError:
        return None


def authoritative_bool(value: Any) -> bool | None:
    """Return a provider flag only when it is explicitly boolean."""
    return value if isinstance(value, bool) else None


def reporting_issue(collar: dict[str, Any]) -> bool | None:
    return authoritative_bool(nested(collar, "issues", "hasReportingIssue"))


def firmware_update_available(collar: dict[str, Any]) -> bool | None:
    return authoritative_bool(nested(collar, "hasFirmwareUpdatesAvailable"))


def sensor_values(collar: dict[str, Any]) -> dict[str, Any]:
    return {
        "battery": telemetry(collar, "batteryChargePercent"),
        "battery_status": pretty_status(telemetry(collar, "batteryStatus")),
        "remaining_battery_lifetime": seconds_to_hours(
            telemetry(collar, "remainingBatteryLifetimeInSeconds")
        ),
        "current_adapter": pretty_status(telemetry(collar, "currentAdapter")),
        "wifi_status": pretty_status(nested(collar, "telemetry", "wiFi", "status")),
        "wifi_signal": nested(collar, "telemetry", "wiFi", "signalStrength"),
        "cellular_status": pretty_status(nested(collar, "telemetry", "cellular", "status")),
        "cellular_signal": nested(collar, "telemetry", "cellular", "signalStrength"),
        "gps_accuracy": nested(collar, "petInfo", "telemetry", "gpsAccuracyInMeters"),
        "gps_accuracy_status": pretty_status(
            nested(collar, "petInfo", "telemetry", "gpsAccuracyStatus")
        ),
        "safety_status": pretty_status(nested(collar, "petInfo", "safetyStatus")),
        "firmware": nested(collar, "firmware", "formattedVersion"),
        "last_telemetry": last_telemetry(collar),
    }
