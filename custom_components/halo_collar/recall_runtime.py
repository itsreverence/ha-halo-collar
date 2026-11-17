from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util.file import write_utf8_file_atomic

from .api import HaloState
from .const import CONF_EMAIL, DOMAIN
from .controls import control_lock_for
from .recall import RecallOwner, RecallStorageError, recall_account_binding, recall_wearers

_RECALL_OWNERS = "_recall_owners"
RECALL_BUTTON_SUFFIX = "_recall_whistle"


def recall_status_signal(entry_id: str, pet_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_{pet_id}_recall_status"


def recall_dogs(hass, entry, state: HaloState) -> dict[str, dict[str, Any] | None]:
    """Dogs that get Recall entities, each with the collar it wears exactly, or None.

    A dog that already has a Recall button keeps it while it wears no collar, so an
    automation that presses it gets an error. Home Assistant skips a missing entity
    without one.
    """
    known = {
        item.unique_id.removesuffix(RECALL_BUTTON_SUFFIX)
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == "button" and item.unique_id.endswith(RECALL_BUTTON_SUFFIX)
    }
    return {
        pet_id: collar
        for pet_id, collar in recall_wearers(state).items()
        if collar is not None or pet_id in known
    }


class RecallFileStore:
    def __init__(self, hass, entry_id: str) -> None:
        self._hass = hass
        self._path = Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}.recall.{entry_id}"))

    async def async_load(self) -> dict[str, Any] | None:
        return await self._hass.async_add_executor_job(self._load)

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._hass.async_add_executor_job(self._save, json.dumps(data))

    async def async_remove(self) -> None:
        await self._hass.async_add_executor_job(self._remove)

    def _load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as err:
            raise RecallStorageError("Recall state could not be read") from err
        if not isinstance(data, dict):
            raise RecallStorageError("Recall state is not a saved document")
        return data

    def _save(self, text: str) -> None:
        # Store.async_save can log a write error and return normally, so write directly.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_utf8_file_atomic(str(self._path), text, private=True)
        except (OSError, HomeAssistantError) as err:
            raise RecallStorageError("Recall state could not be written") from err

    def _remove(self) -> None:
        self._path.unlink(missing_ok=True)


def recall_owner_for(hass, entry) -> RecallOwner:
    domain_data = hass.data.setdefault(DOMAIN, {})
    owners: dict[str, RecallOwner] = domain_data.setdefault(_RECALL_OWNERS, {})
    if entry.entry_id not in owners:
        owners[entry.entry_id] = RecallOwner(
            store=RecallFileStore(hass, entry.entry_id),
            account=recall_account_binding(entry.unique_id or entry.data.get(CONF_EMAIL)),
            lock=control_lock_for(domain_data, entry.entry_id),
        )
    return owners[entry.entry_id]


def existing_recall_owner(hass, entry) -> RecallOwner | None:
    return hass.data.get(DOMAIN, {}).get(_RECALL_OWNERS, {}).get(entry.entry_id)


async def async_remove_recall_state(hass, entry) -> None:
    hass.data.get(DOMAIN, {}).get(_RECALL_OWNERS, {}).pop(entry.entry_id, None)
    await RecallFileStore(hass, entry.entry_id).async_remove()
