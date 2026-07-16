from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
    "unknown": "Unknown",
    "noissue": "No issue",
}


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


def has_active_walk(pet: dict[str, Any] | None, collar: dict[str, Any] | None) -> bool:
    """Return whether either Halo payload reports a current walk."""
    return (
        nested(pet or {}, "telemetry", "walk") is not None
        or nested(collar or {}, "telemetry", "walk") is not None
    )


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


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
