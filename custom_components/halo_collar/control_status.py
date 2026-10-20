from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from .api import HaloState
from .const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    DOMAIN,
)
from .helpers import (
    active_walk_state,
    is_online,
    last_telemetry,
    nested,
    pet_fences_enabled,
    pet_for_collar,
    subscription_feature_enabled,
)

ControlAction = Literal["enable_fences", "disable_fences", "find_collar"]


class ControlReason(StrEnum):
    READY = "ready"
    DISABLED = "disabled"
    CLOUD_UNAVAILABLE = "cloud_unavailable"
    MAPPING_UNAVAILABLE = "mapping_unavailable"
    REPORT_UNKNOWN = "report_unknown"
    REPORT_STALE = "report_stale"
    FENCE_SYNC_UNKNOWN = "fence_sync_unknown"
    FENCES_NOT_SYNCHRONIZED = "fences_not_synchronized"
    FENCE_MODE_UNKNOWN = "fence_mode_unknown"
    WALK_UNKNOWN = "walk_unknown"
    WALK_ACTIVE = "walk_active"
    ENTITLEMENT_UNCONFIRMED = "entitlement_unconfirmed"
    COOLDOWN = "cooldown"


def find_status_signal(entry_id: str, collar_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_{collar_id}_find_status"


def enabled_control_actions(options: Mapping[str, Any]) -> tuple[ControlAction, ...]:
    actions: list[ControlAction] = []
    if options.get(CONF_ENABLE_FENCE_CONTROLS, False):
        actions.append("enable_fences")
        if options.get(CONF_ALLOW_FENCE_DISABLE, False):
            actions.append("disable_fences")
    if options.get(CONF_ENABLE_FIND_COLLAR, False):
        actions.append("find_collar")
    return tuple(actions)


def control_status_reason(
    state: HaloState,
    *,
    action: ControlAction,
    collar_id: str,
    options: Mapping[str, Any],
    cloud_success: bool,
    stale_after: float,
    cooldown_remaining: float,
    now: datetime,
) -> ControlReason:
    if action not in enabled_control_actions(options):
        return ControlReason.DISABLED
    if not cloud_success:
        return ControlReason.CLOUD_UNAVAILABLE
    collar = next((item for item in state.collars if item.get("id") == collar_id), None)
    pet = pet_for_collar(state.pets, collar, state.collars) if collar is not None else None
    if collar is None or pet is None or not isinstance(pet.get("id"), str) or not pet["id"]:
        return ControlReason.MAPPING_UNAVAILABLE
    if action == "find_collar" and (
        nested(collar, "petInfo", "id") != pet["id"] or nested(pet, "collarInfo", "id") != collar_id
    ):
        return ControlReason.MAPPING_UNAVAILABLE
    if last_telemetry(collar) is None:
        return ControlReason.REPORT_UNKNOWN
    if not is_online(collar, now=now, stale_after=stale_after):
        return ControlReason.REPORT_STALE
    if action == "enable_fences":
        return ControlReason.READY
    if action == "disable_fences":
        synchronized = pet.get("isFencesSynchronized")
        if not isinstance(synchronized, bool):
            return ControlReason.FENCE_SYNC_UNKNOWN
        if not synchronized:
            return ControlReason.FENCES_NOT_SYNCHRONIZED
        if pet_fences_enabled(pet) is None:
            return ControlReason.FENCE_MODE_UNKNOWN
    walking = active_walk_state(pet, collar)
    if walking is None:
        return ControlReason.WALK_UNKNOWN
    if walking:
        return ControlReason.WALK_ACTIVE
    if action == "find_collar":
        if not subscription_feature_enabled(state.subscription, "findcollar"):
            return ControlReason.ENTITLEMENT_UNCONFIRMED
        if cooldown_remaining > 0:
            return ControlReason.COOLDOWN
    return ControlReason.READY
