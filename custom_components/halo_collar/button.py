from __future__ import annotations

from typing import cast

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .api import HaloApiError, HaloState
from .const import (
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_ENABLE_RECALL_WHISTLE,
    DOMAIN,
)
from .control_status import find_status_signal
from .controls import (
    HaloControlError,
    async_find_collar,
    async_set_fence_mode,
    control_lock_for,
    control_stale_after,
    find_collar_cooldown_remaining,
    find_collar_cooldowns_for,
    live_runtime_check,
)
from .entity import HaloDogEntity, HaloEntity
from .helpers import has_active_walk, is_online, subscription_feature_enabled
from .recall_runtime import (
    RECALL_BUTTON_SUFFIX,
    recall_dogs,
    recall_owner_for,
    recall_status_signal,
)


async def async_setup_entry(hass, entry, async_add_entities):
    fence_controls = entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False)
    find_collar = entry.options.get(CONF_ENABLE_FIND_COLLAR, False)
    recall_whistle = entry.options.get(CONF_ENABLE_RECALL_WHISTLE, False)
    if not fence_controls and not find_collar and not recall_whistle:
        return
    stored = hass.data[DOMAIN][entry.entry_id]
    coordinator = stored["coordinator"]
    control_lock = control_lock_for(hass.data[DOMAIN], entry.entry_id)
    is_current = live_runtime_check(hass.data[DOMAIN], entry.entry_id)
    entities = []
    if fence_controls:
        entities.extend(
            HaloEnableFencesButton(
                coordinator,
                entry,
                stored["client"],
                collar,
                control_lock,
                is_current,
            )
            for collar in coordinator.data.collars
        )
    if find_collar:
        cooldowns = find_collar_cooldowns_for(hass.data[DOMAIN], entry.entry_id)
        entities.extend(
            HaloFindCollarButton(
                coordinator,
                entry,
                stored["client"],
                collar,
                control_lock,
                is_current,
                cooldowns,
            )
            for collar in coordinator.data.collars
        )
    if recall_whistle:
        recall = recall_owner_for(hass, entry)
        entities.extend(
            HaloRecallWhistleButton(
                coordinator, entry, stored["client"], collar, pet_id, recall, is_current
            )
            for pet_id, collar in recall_dogs(hass, entry, coordinator.data).items()
        )
    async_add_entities(entities)


class HaloEnableFencesButton(HaloEntity, ButtonEntity):
    """Fail-safe, idempotent control that can only enable fence enforcement."""

    _attr_translation_key = "enable_fences"
    _attr_icon = "mdi:shield-check"

    def __init__(self, coordinator, entry, client, collar, control_lock, is_current) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._control_lock = control_lock
        self._is_current = is_current
        self._attr_unique_id = f"{self._collar_id}_enable_fences"

    @property
    def available(self) -> bool:
        collar = self.collar
        pet = self.pet
        stale_after = control_stale_after(self._entry)
        return (
            super().available
            and collar is not None
            and pet is not None
            and is_online(collar, stale_after=float(stale_after))
        )

    async def async_press(self) -> None:
        try:
            await async_set_fence_mode(
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                control_lock=self._control_lock,
                is_current=self._is_current,
                state_getter=lambda: (self.pet, self.collar),
                enabled=True,
            )
        except (HaloApiError, HaloControlError) as err:
            raise HomeAssistantError(f"Could not enable Halo fences: {err}") from err


class HaloFindCollarButton(HaloEntity, ButtonEntity):
    """Explicitly opted-in physical sound-and-light command."""

    _attr_translation_key = "find_collar"
    _attr_icon = "mdi:bell-ring"

    def __init__(
        self,
        coordinator,
        entry,
        client,
        collar,
        control_lock,
        is_current,
        cooldowns,
    ) -> None:
        super().__init__(coordinator, entry, collar)
        self._client = client
        self._control_lock = control_lock
        self._is_current = is_current
        self._cooldowns = cooldowns
        self._cancel_cooldown_update = None
        self._attr_unique_id = f"{self._collar_id}_find_collar"

    async def async_added_to_hass(self) -> None:
        """Register coordinator updates and restore any in-flight cooldown timer."""
        await super().async_added_to_hass()
        self._schedule_cooldown_update(write_state=False)

    @callback
    def _schedule_cooldown_update(self, *, write_state: bool = True) -> None:
        """Update availability exactly when the monotonic cooldown expires."""
        if self._cancel_cooldown_update is not None:
            self._cancel_cooldown_update()
            self._cancel_cooldown_update = None

        remaining = find_collar_cooldown_remaining(self._cooldowns, self._collar_id)
        if remaining > 0:
            self._cancel_cooldown_update = async_call_later(
                self.hass,
                remaining,
                self._async_cooldown_finished,
            )
            self.async_on_remove(self._cancel_cooldown_update)
        if write_state:
            self.async_write_ha_state()

    @callback
    def _async_cooldown_finished(self, _now) -> None:
        """Recheck the monotonic deadline and make the button available promptly."""
        self._cancel_cooldown_update = None
        if find_collar_cooldown_remaining(self._cooldowns, self._collar_id) > 0:
            self._schedule_cooldown_update(write_state=False)
            return
        self.async_write_ha_state()

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        collar = self.collar
        pet = self.pet
        state = cast(HaloState, self.coordinator.data)
        return (
            self.coordinator.last_update_success
            and collar is not None
            and pet is not None
            and is_online(collar, stale_after=control_stale_after(self._entry))
            and not has_active_walk(pet, collar)
            and subscription_feature_enabled(state.subscription, "findcollar")
            and find_collar_cooldown_remaining(self._cooldowns, self._collar_id) <= 0
        )

    async def async_press(self) -> None:
        try:
            await async_find_collar(
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                control_lock=self._control_lock,
                is_current=self._is_current,
                cooldowns=self._cooldowns,
                state_getter=lambda: (
                    self.pet,
                    self.collar,
                    cast(HaloState, self.coordinator.data).subscription,
                ),
            )
        except (HaloApiError, HaloControlError) as err:
            raise HomeAssistantError(f"Could not find Halo collar: {err}") from err
        finally:
            async_dispatcher_send(
                self.hass, find_status_signal(self._entry.entry_id, self._collar_id)
            )
            self._schedule_cooldown_update()


class HaloRecallWhistleButton(HaloDogEntity, ButtonEntity):
    _attr_translation_key = "recall_whistle"
    _attr_icon = "mdi:whistle"

    def __init__(self, coordinator, entry, client, collar, pet_id: str, recall, is_current) -> None:
        super().__init__(coordinator, entry, collar, pet_id)
        self._client = client
        self._recall = recall
        self._is_current = is_current
        self._attr_unique_id = f"{pet_id}{RECALL_BUTTON_SUFFIX}"

    @property
    def available(self) -> bool:  # pyright: ignore[reportIncompatibleVariableOverride]
        # Home Assistant skips unavailable entities in button.press without an error,
        # so an automation's whistle would vanish silently. Every refusal raises instead,
        # and the Recall whistle status sensor previews readiness.
        return True

    async def async_press(self) -> None:
        try:
            await self._recall.async_press(
                self._pet_id,
                coordinator=self.coordinator,
                client=self._client,
                entry=self._entry,
                is_current=self._is_current,
            )
        except (HaloApiError, HaloControlError) as err:
            raise HomeAssistantError(f"Could not play the Halo Recall whistle: {err}") from err
        finally:
            async_dispatcher_send(
                self.hass, recall_status_signal(self._entry.entry_id, self._pet_id)
            )
