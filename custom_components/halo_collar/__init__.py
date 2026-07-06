from __future__ import annotations

import logging
from datetime import timedelta

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
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


async def async_setup_entry(hass, entry) -> bool:
    from homeassistant.const import Platform
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

    from .api import HaloApiClient, HaloApiError, HaloAuthError

    session = async_get_clientsession(hass)
    data = dict(entry.data)
    client = HaloApiClient(
        session=session,
        access_token=data.get(CONF_ACCESS_TOKEN, ""),
        refresh_token=data[CONF_REFRESH_TOKEN],
        expires_at=float(data.get(CONF_EXPIRES_AT, 0) or 0),
        client_id=data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
        client_secret=data.get(CONF_CLIENT_SECRET, ""),
        api_base=DEFAULT_API_BASE,
        auth_base=DEFAULT_AUTH_BASE,
    )

    async def _async_update():
        try:
            state = await client.async_fetch_state()
        except HaloAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HaloApiError as err:
            raise UpdateFailed(str(err)) from err
        _persist_tokens(hass, entry, client)
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
    }
    # Applied in place rather than via reload: the entry is also updated when
    # refreshed tokens are persisted, and a reload there would loop.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(
        entry,
        [Platform(p) for p in PLATFORMS],
    )
    return True


async def _async_entry_updated(hass, entry) -> None:
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if stored is None:
        return
    coordinator = stored["coordinator"]
    interval = _scan_interval(entry)
    if coordinator.update_interval != interval:
        coordinator.update_interval = interval
    # Entities read options (e.g. staleness threshold) at evaluation time; push a
    # state refresh so option changes apply without waiting for the next poll.
    coordinator.async_update_listeners()


def _persist_tokens(hass, entry, client) -> None:
    """Write refreshed tokens back to the config entry so they survive restarts."""
    snapshot = client.token_snapshot
    if snapshot["access_token"] == entry.data.get(CONF_ACCESS_TOKEN) and snapshot[
        "refresh_token"
    ] == entry.data.get(CONF_REFRESH_TOKEN):
        return
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_ACCESS_TOKEN: snapshot["access_token"],
            CONF_REFRESH_TOKEN: snapshot["refresh_token"],
            CONF_EXPIRES_AT: snapshot["expires_at"],
        },
    )


async def async_unload_entry(hass, entry) -> bool:
    from homeassistant.const import Platform

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        [Platform(p) for p in PLATFORMS],
    )
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
