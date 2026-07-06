from __future__ import annotations

from typing import Any

from .const import DOMAIN
from .helpers import redact


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    stored = hass.data[DOMAIN][entry.entry_id]
    state = stored["coordinator"].data
    return {
        "entry": {"data": redact(dict(entry.data)), "options": dict(entry.options)},
        "pets": redact(state.pets),
        "collars": redact(state.collars),
        "subscription": redact(state.subscription),
        "server_time": state.server_time,
    }
