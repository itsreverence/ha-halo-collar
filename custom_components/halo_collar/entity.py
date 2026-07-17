from __future__ import annotations

from typing import Any

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .helpers import pet_for_collar


class HaloEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, collar: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._collar_id = collar["id"]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._collar_id)},
            "name": collar.get("petInfo", {}).get("name") or "Halo Collar",
            "manufacturer": "Halo Collar",
            "model": collar.get("type"),
            "sw_version": collar.get("firmware", {}).get("version"),
            "serial_number": collar.get("serialNumber"),
        }

    @property
    def collar(self) -> dict[str, Any] | None:
        for collar in self.coordinator.data.collars:
            if collar.get("id") == self._collar_id:
                return collar
        return None

    @property
    def pet(self) -> dict[str, Any] | None:
        collar = self.collar
        if collar is None:
            return None
        return pet_for_collar(
            self.coordinator.data.pets,
            collar,
            self.coordinator.data.collars,
        )

    @property
    def available(self) -> bool:
        return super().available and self.collar is not None
