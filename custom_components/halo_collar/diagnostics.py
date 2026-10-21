from __future__ import annotations

from typing import Any

from .const import DOMAIN
from .helpers import redact

_WALK_DIAGNOSTIC_KEYS = ("walkedDurationInSeconds", "walkedDistanceInMeters", "feedbacksCount")


def _walk_history_diagnostics(walks: list[Any]) -> list[dict[str, Any]]:
    """Keep only per-dog scalar walk metrics: no IDs, timestamps, routes, or locations."""
    return [
        {
            "pets": [
                {key: pet[key] for key in _WALK_DIAGNOSTIC_KEYS if key in pet}
                for pet in walk.get("pets") or []
                if isinstance(pet, dict)
            ]
        }
        for walk in walks
        if isinstance(walk, dict) and isinstance(walk.get("pets") or [], list)
    ]


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    stored = hass.data[DOMAIN][entry.entry_id]
    state = stored["coordinator"].data
    return {
        "entry": {"data": redact(dict(entry.data)), "options": dict(entry.options)},
        "pets": redact(state.pets),
        "collars": redact(state.collars),
        "subscription": redact(state.subscription),
        "server_time": state.server_time,
        "walk_history_status": state.walk_history_status,
        "walk_history": _walk_history_diagnostics(state.walks),
    }
