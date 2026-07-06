from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    DEFAULT_CLIENT_ID,
    DOMAIN,
)


def _parse_token_bundle(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_json") from exc
    if not isinstance(data, dict) or not data.get(CONF_REFRESH_TOKEN):
        raise ValueError("missing_refresh_token")
    return data


class HaloCollarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                token = _parse_token_bundle(user_input["token_json"])
            except ValueError as exc:
                errors["token_json"] = str(exc)
            else:
                title = user_input.get(CONF_NAME) or "Halo Collar"
                data = {
                    CONF_ACCESS_TOKEN: token.get(CONF_ACCESS_TOKEN, ""),
                    CONF_REFRESH_TOKEN: token[CONF_REFRESH_TOKEN],
                    CONF_EXPIRES_AT: token.get(CONF_EXPIRES_AT, 0),
                    CONF_CLIENT_ID: user_input.get(CONF_CLIENT_ID) or DEFAULT_CLIENT_ID,
                    CONF_CLIENT_SECRET: user_input.get(CONF_CLIENT_SECRET, ""),
                }
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default="Halo Collar"): str,
                    vol.Required("token_json"): str,
                    vol.Optional(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): str,
                    vol.Required(CONF_CLIENT_SECRET): str,
                }
            ),
            errors=errors,
        )
