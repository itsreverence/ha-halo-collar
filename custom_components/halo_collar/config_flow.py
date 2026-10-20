from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import async_request_reload
from .api import HaloApiClient, HaloApiError, HaloAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ALLOW_FENCE_DISABLE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EMAIL,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
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
    DEFAULT_ENABLE_FIND_COLLAR,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    DEFAULT_TOKEN_SCOPE,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_STALE_AFTER_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_STALE_AFTER_SECONDS,
)

_ACCOUNT_RECOVERY_URL = (
    "https://github.com/itsreverence/ha-halo-collar/blob/main/SUPPORT.md#recover-authentication"
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


def _reauth_context(entry: config_entries.ConfigEntry) -> tuple[str, str | None, str, str] | None:
    email = entry.data.get(CONF_EMAIL)
    if not isinstance(email, str) or not email.strip():
        return None
    unique_id = entry.unique_id
    if unique_id is not None and unique_id.strip().lower() != email.strip().lower():
        return None
    return (
        email,
        unique_id,
        entry.data.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID,
        entry.data.get(CONF_CLIENT_SECRET) or DEFAULT_CLIENT_SECRET,
    )


class HaloCollarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
        email = user_input[CONF_EMAIL].strip().lower()
        await client.async_login(
            email,
            user_input[CONF_PASSWORD],
            scope=DEFAULT_TOKEN_SCOPE,
        )
        snapshot = client.token_snapshot
        return {
            CONF_EMAIL: email,
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
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_NAME, default="Halo Collar"): str,
                    vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
                    vol.Optional(CONF_CLIENT_SECRET, default=DEFAULT_CLIENT_SECRET): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None or (login_context := _reauth_context(entry)) is None:
            return self.async_abort(
                reason="reauth_account_unavailable",
                description_placeholders={"recovery_url": _ACCOUNT_RECOVERY_URL},
            )
        saved_email, _, client_id, client_secret = login_context
        email = saved_email.strip().lower()
        if user_input is not None:
            if user_input[CONF_EMAIL].strip().lower() != email:
                errors[CONF_EMAIL] = "account_mismatch"
            else:
                try:
                    data = await self._async_validate(
                        {
                            CONF_EMAIL: email,
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_CLIENT_ID: client_id,
                            CONF_CLIENT_SECRET: client_secret,
                        }
                    )
                except HaloAuthError:
                    errors["base"] = "invalid_auth"
                except HaloApiError:
                    errors["base"] = "cannot_connect"
                else:
                    current = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                    if current is None or _reauth_context(current) != login_context:
                        return self.async_abort(
                            reason="reauth_entry_changed",
                            description_placeholders={"recovery_url": _ACCOUNT_RECOVERY_URL},
                        )
                    self.hass.config_entries.async_update_entry(
                        current,
                        data={**current.data, **data},
                    )
                    async_request_reload(self.hass, current)
                    return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=saved_email): str,
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
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
                        CONF_ENABLE_FIND_COLLAR,
                        default=options.get(CONF_ENABLE_FIND_COLLAR, DEFAULT_ENABLE_FIND_COLLAR),
                    ): bool,
                    vol.Required(
                        CONF_ALLOW_FENCE_DISABLE,
                        default=options.get(CONF_ALLOW_FENCE_DISABLE, DEFAULT_ALLOW_FENCE_DISABLE),
                    ): bool,
                }
            ),
        )
