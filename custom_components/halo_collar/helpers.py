from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

STALE_AFTER_SECONDS = 900


def telemetry(collar: dict[str, Any], key: str) -> Any:
    return collar.get("telemetry", {}).get(key)


def nested(collar: dict[str, Any], *keys: str) -> Any:
    value: Any = collar
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_online(collar: dict[str, Any], *, now: datetime | None = None) -> bool:
    timestamp = parse_timestamp(nested(collar, "telemetry", "manifest", "timestamp"))
    if timestamp is None:
        return False
    return ((now or datetime.now(UTC)) - timestamp).total_seconds() <= STALE_AFTER_SECONDS


def sensor_values(collar: dict[str, Any]) -> dict[str, Any]:
    return {
        "battery": telemetry(collar, "batteryChargePercent"),
        "battery_status": telemetry(collar, "batteryStatus"),
        "remaining_battery_lifetime": telemetry(collar, "remainingBatteryLifetimeInSeconds"),
        "current_adapter": telemetry(collar, "currentAdapter"),
        "wifi_status": nested(collar, "telemetry", "wiFi", "status"),
        "wifi_signal": nested(collar, "telemetry", "wiFi", "signalStrength"),
        "cellular_status": nested(collar, "telemetry", "cellular", "status"),
        "cellular_signal": nested(collar, "telemetry", "cellular", "signalStrength"),
        "gps_accuracy": nested(collar, "petInfo", "telemetry", "gpsAccuracyInMeters"),
        "gps_accuracy_status": nested(collar, "petInfo", "telemetry", "gpsAccuracyStatus"),
        "safety_status": nested(collar, "petInfo", "safetyStatus"),
        "firmware": nested(collar, "firmware", "formattedVersion"),
    }
