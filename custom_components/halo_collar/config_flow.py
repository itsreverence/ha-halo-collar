from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import HaloApiClient, HaloApiError, HaloAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EMAIL,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_EXPIRES_AT,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_STALE_AFTER,
    DEFAULT_ALLOW_FENCE_DISABLE,
    DEFAULT_API_BASE,
    DEFAULT_AUTH_BASE,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DEFAULT_ENABLE_FENCE_CONTROLS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    DEFAULT_TOKEN_SCOPE,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_STALE_AFTER_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_STALE_AFTER_SECONDS,
)


def _client(hass, client_id: str, client_secret: str) -> HaloApiClient:
    return HaloApiClient(
        session=async_get_clientsession(hass),
        access_token="",
        refresh_token="",
        expires_at=0,
        client_id=client_id or DEFAULT_CLIENT_ID,
        client_secret=client_secret or DEFAULT_CLIENT_SECRET,
        api_base=DEFAULT_API_BASE,
        auth_base=DEFAULT_AUTH_BASE,
    )


class HaloCollarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> HaloCollarOptionsFlow:
        return HaloCollarOptionsFlow()

    async def _async_validate(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Sign in with the given credentials and return a token/data payload."""
        client = _client(
            self.hass,
            user_input.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
            user_input.get(CONF_CLIENT_SECRET, DEFAULT_CLIENT_SECRET),
        )
        await client.async_login(
            user_input[CONF_EMAIL],
            user_input[CONF_PASSWORD],
            scope=DEFAULT_TOKEN_SCOPE,
        )
        snapshot = client.token_snapshot
        return {
            CONF_EMAIL: user_input[CONF_EMAIL],
            CONF_ACCESS_TOKEN: snapshot["access_token"],
            CONF_REFRESH_TOKEN: snapshot["refresh_token"],
            CONF_EXPIRES_AT: snapshot["expires_at"],
            CONF_CLIENT_ID: user_input.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID,
            CONF_CLIENT_SECRET: user_input.get(CONF_CLIENT_SECRET) or DEFAULT_CLIENT_SECRET,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].strip().lower())
            self._abort_if_unique_id_configured()
            try:
                data = await self._async_validate(user_input)
            except HaloAuthError:
                errors["base"] = "invalid_auth"
            except HaloApiError:
                errors["base"] = "cannot_connect"
            else:
                title = user_input.get(CONF_NAME) or "Halo Collar"
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_NAME, default="Halo Collar"): str,
                    vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
                    vol.Optional(CONF_CLIENT_SECRET, default=DEFAULT_CLIENT_SECRET): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None
        existing = self._reauth_entry.data
        if user_input is not None:
            merged = {
                CONF_EMAIL: existing.get(CONF_EMAIL, ""),
                CONF_CLIENT_ID: existing.get(CONF_CLIENT_ID, DEFAULT_CLIENT_ID),
                CONF_CLIENT_SECRET: existing.get(CONF_CLIENT_SECRET, DEFAULT_CLIENT_SECRET),
                **user_input,
            }
            try:
                data = await self._async_validate(merged)
            except HaloAuthError:
                errors["base"] = "invalid_auth"
            except HaloApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data={**existing, **data},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=existing.get(CONF_EMAIL, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class HaloCollarOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if not user_input.get(CONF_ENABLE_FENCE_CONTROLS):
                user_input[CONF_ALLOW_FENCE_DISABLE] = False
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_SECONDS),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL_SECONDS, max=MAX_SCAN_INTERVAL_SECONDS),
                    ),
                    vol.Required(
                        CONF_STALE_AFTER,
                        default=options.get(CONF_STALE_AFTER, DEFAULT_STALE_AFTER_SECONDS),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_STALE_AFTER_SECONDS, max=MAX_STALE_AFTER_SECONDS),
                    ),
                    vol.Required(
                        CONF_ENABLE_FENCE_CONTROLS,
                        default=options.get(
                            CONF_ENABLE_FENCE_CONTROLS, DEFAULT_ENABLE_FENCE_CONTROLS
                        ),
                    ): bool,
                    vol.Required(
                        CONF_ALLOW_FENCE_DISABLE,
                        default=options.get(CONF_ALLOW_FENCE_DISABLE, DEFAULT_ALLOW_FENCE_DISABLE),
                    ): bool,
                }
            ),
        )
