from __future__ import annotations

from datetime import timedelta

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    DEFAULT_CLIENT_ID,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DOMAIN,
    PLATFORMS,
)


async def async_setup_entry(hass, entry) -> bool:
    import logging

    from homeassistant.const import Platform
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from .api import HaloApiClient

    session = async_get_clientsession(hass)
    data = dict(entry.data)
    options = dict(entry.options)
    client = HaloApiClient(
        session=session,
        access_token=data.get(CONF_ACCESS_TOKEN, ""),
        refresh_token=data[CONF_REFRESH_TOKEN],
        expires_at=float(data.get(CONF_EXPIRES_AT, 0) or 0),
        client_id=options.get(CONF_CLIENT_ID, data.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID)),
        client_secret=options.get(CONF_CLIENT_SECRET, data.get(CONF_CLIENT_SECRET, "")),
        api_base=DEFAULT_API_BASE,
        auth_base=DEFAULT_AUTH_BASE,
    )
    coordinator = DataUpdateCoordinator(
        hass,
        logger=logging.getLogger(__name__),
        name=DOMAIN,
        update_method=client.async_fetch_state,
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
    )
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(
        entry,
        [Platform(p) for p in PLATFORMS],
    )
    return True


async def async_unload_entry(hass, entry) -> bool:
    from homeassistant.const import Platform

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry,
        [Platform(p) for p in PLATFORMS],
    )
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
