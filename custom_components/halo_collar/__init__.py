from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, NamedTuple

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    DEFAULT_CLIENT_ID,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)


def _scan_interval(entry) -> timedelta:
    seconds = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS)
    return timedelta(seconds=int(seconds))


def _controls_signature(entry) -> tuple[bool, bool, bool]:
    return (
        bool(entry.options.get(CONF_ENABLE_FENCE_CONTROLS, False)),
        bool(entry.options.get(CONF_ENABLE_FIND_COLLAR, False)),
        bool(entry.options.get(CONF_ALLOW_FENCE_DISABLE, False)),
    )


async def async_setup_entry(hass, entry) -> bool:
    from homeassistant.const import Platform
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
    from homeassistant.util.ssl import client_context

    from .api import HaloApiClient, HaloApiError, HaloAuthError
    from .transport import HaloWriteTransport

    session = async_get_clientsession(hass)
    ssl_context = await hass.async_add_executor_job(client_context)
    data = dict(entry.data)
    credentials = _credentials(data)
    writer = _TokenWriter(hass, entry, credentials)
    setup_complete = False
    try:
        entry.async_on_unload(writer.retire)
        client = HaloApiClient(
            session=session,
            access_token=credentials.access_token,
            refresh_token=credentials.refresh_token,
            expires_at=credentials.expires_at,
            client_id=data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
            client_secret=data.get(CONF_CLIENT_SECRET, ""),
            api_base=DEFAULT_API_BASE,
            auth_base=DEFAULT_AUTH_BASE,
            write_transport=HaloWriteTransport(
                ssl_context=ssl_context,
                default_headers=session.headers,
                trust_env=session.trust_env,
            ),
            on_token_update=writer.persist,
        )

        async def _async_update():
            try:
                state = await client.async_fetch_state()
            except HaloAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except HaloApiError as err:
                raise UpdateFailed(str(err)) from err
            return state

        coordinator = DataUpdateCoordinator(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_method=_async_update,
            update_interval=_scan_interval(entry),
        )
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "client": client,
            "coordinator": coordinator,
            "controls_signature": _controls_signature(entry),
        }
        # Applied in place rather than via reload: the entry is also updated when
        # refreshed tokens are persisted, and a reload there would loop.
        entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
        await hass.config_entries.async_forward_entry_setups(
            entry,
            [Platform(p) for p in PLATFORMS],
        )

        known_collar_ids = {
            collar.get("id")
            for collar in coordinator.data.collars
            if isinstance(collar.get("id"), str) and collar.get("id")
        }
        inventory_reload_pending = False

        def _reload_for_new_collars() -> None:
            nonlocal inventory_reload_pending
            if inventory_reload_pending:
                return
            current_collar_ids = {
                collar.get("id")
                for collar in coordinator.data.collars
                if isinstance(collar.get("id"), str) and collar.get("id")
            }
            if current_collar_ids - known_collar_ids:
                inventory_reload_pending = True
                hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))

        entry.async_on_unload(coordinator.async_add_listener(_reload_for_new_collars))
        setup_complete = True
        return True
    finally:
        # Minimum HA does not run unload callbacks for every incomplete setup.
        if not setup_complete:
            writer.retire()


async def _async_entry_updated(hass, entry) -> None:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is None:
        return
    coordinator = stored["coordinator"]
    controls_signature = _controls_signature(entry)
    if controls_signature != stored["controls_signature"]:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    interval = _scan_interval(entry)
    if coordinator.update_interval != interval:
        coordinator.update_interval = interval
    # Entities read options (e.g. staleness threshold) at evaluation time; push a
    # state refresh so option changes apply without waiting for the next poll.
    coordinator.async_update_listeners()


class _Credentials(NamedTuple):
    access_token: str
    refresh_token: str
    expires_at: float


def _credentials(data: Mapping[str, Any]) -> _Credentials:
    return _Credentials(
        data.get(CONF_ACCESS_TOKEN, ""),
        data[CONF_REFRESH_TOKEN],
        float(data.get(CONF_EXPIRES_AT, 0) or 0),
    )


class _TokenWriter:
    def __init__(self, hass, entry, credentials: _Credentials) -> None:
        self._hass = hass
        self._entry = entry
        self._head = credentials
        self._live = True

    def retire(self) -> None:
        self._live = False

    def persist(self, snapshot: Mapping[str, Any]) -> None:
        entry = self._entry
        if not self._live or self._hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            return
        if _credentials(entry.data) != self._head:
            self.retire()
            return
        rotated = _credentials(snapshot)
        if rotated != self._head:
            self._hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_ACCESS_TOKEN: rotated.access_token,
                    CONF_REFRESH_TOKEN: rotated.refresh_token,
                    CONF_EXPIRES_AT: rotated.expires_at,
                },
            )
            self._head = rotated


async def async_remove_entry(hass, entry) -> None:
    """Remove durable control state after permanent config-entry deletion."""
    from .controls import remove_control_lock

    remove_control_lock(hass.data.get(DOMAIN, {}), entry.entry_id)


async def async_unload_entry(hass, entry) -> bool:
    from homeassistant.const import Platform

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        [Platform(p) for p in PLATFORMS],
    )
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
