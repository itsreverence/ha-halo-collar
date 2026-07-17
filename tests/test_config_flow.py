from __future__ import annotations

# Home Assistant is intentionally checked before its optional test dependencies.
# ruff: noqa: E402, I001

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_collar.api import HaloApiError, HaloAuthError
from custom_components.halo_collar.const import (
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
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_SECRET,
    DOMAIN,
    MAX_SCAN_INTERVAL_SECONDS,
    MAX_STALE_AFTER_SECONDS,
    MIN_SCAN_INTERVAL_SECONDS,
    MIN_STALE_AFTER_SECONDS,
)


def _client(*, error: Exception | None = None):
    client = AsyncMock()
    if error is not None:
        client.async_login.side_effect = error
    client.token_snapshot = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": 4_102_444_800.0,
    }
    return client


def _user_input(**overrides):
    return {
        CONF_EMAIL: "Owner@Example.COM ",
        CONF_PASSWORD: "synthetic-password",
        CONF_NAME: "Synthetic Halo",
        CONF_CLIENT_ID: "",
        CONF_CLIENT_SECRET: "",
        **overrides,
    }


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Halo Collar",
        unique_id="owner@example.com",
        data={
            CONF_EMAIL: "owner@example.com",
            CONF_ACCESS_TOKEN: "old-access",
            CONF_REFRESH_TOKEN: "old-refresh",
            CONF_EXPIRES_AT: 1.0,
            CONF_CLIENT_ID: DEFAULT_CLIENT_ID,
            CONF_CLIENT_SECRET: DEFAULT_CLIENT_SECRET,
        },
        options={
            CONF_SCAN_INTERVAL: 300,
            CONF_STALE_AFTER: 900,
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ENABLE_FIND_COLLAR: False,
            CONF_ALLOW_FENCE_DISABLE: True,
        },
    )


async def _start_user_flow(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_flow_creates_normalized_passwordless_entry(hass):
    initial = await _start_user_flow(hass)
    client = _client()

    with patch(
        "custom_components.halo_collar.config_flow._client", return_value=client
    ) as client_factory:
        result = await hass.config_entries.flow.async_configure(
            initial["flow_id"],
            _user_input(),
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Synthetic Halo"
    assert result["data"] == {
        CONF_EMAIL: "owner@example.com",
        CONF_ACCESS_TOKEN: "new-access",
        CONF_REFRESH_TOKEN: "new-refresh",
        CONF_EXPIRES_AT: 4_102_444_800.0,
        CONF_CLIENT_ID: DEFAULT_CLIENT_ID,
        CONF_CLIENT_SECRET: DEFAULT_CLIENT_SECRET,
    }
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == "owner@example.com"
    client_factory.assert_called_once()
    client.async_login.assert_awaited_once_with(
        "owner@example.com",
        "synthetic-password",
        scope="openid email offline_access api.dogpark",
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HaloAuthError("bad credentials"), "invalid_auth"),
        (HaloApiError("provider unavailable"), "cannot_connect"),
    ],
)
async def test_user_flow_classifies_login_failures(hass, error, expected):
    initial = await _start_user_flow(hass)
    with patch(
        "custom_components.halo_collar.config_flow._client",
        return_value=_client(error=error),
    ):
        result = await hass.config_entries.flow.async_configure(initial["flow_id"], _user_input())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


async def test_user_flow_aborts_duplicate_normalized_email_before_login(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    initial = await _start_user_flow(hass)

    with patch("custom_components.halo_collar.config_flow._client") as client_factory:
        result = await hass.config_entries.flow.async_configure(initial["flow_id"], _user_input())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    client_factory.assert_not_called()


async def test_reauth_replaces_tokens_preserves_other_data_and_never_password(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch("custom_components.halo_collar.config_flow._client", return_value=client):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: " Replacement@Example.COM ",
                CONF_PASSWORD: "replacement-password",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_EMAIL] == "replacement@example.com"
    assert entry.data[CONF_ACCESS_TOKEN] == "new-access"
    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert entry.data[CONF_EXPIRES_AT] == 4_102_444_800.0
    assert entry.data[CONF_CLIENT_ID] == DEFAULT_CLIENT_ID
    assert entry.data[CONF_CLIENT_SECRET] == DEFAULT_CLIENT_SECRET
    assert CONF_PASSWORD not in entry.data
    client.async_login.assert_awaited_once_with(
        "replacement@example.com",
        "replacement-password",
        scope="openid email offline_access api.dogpark",
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (HaloAuthError("bad credentials"), "invalid_auth"),
        (HaloApiError("provider unavailable"), "cannot_connect"),
    ],
)
async def test_reauth_classifies_failures_without_changing_entry(hass, error, expected):
    entry = _entry()
    entry.add_to_hass(hass)
    original = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=entry.data,
    )

    with patch(
        "custom_components.halo_collar.config_flow._client",
        return_value=_client(error=error),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: "owner@example.com",
                CONF_PASSWORD: "replacement-password",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert entry.data == original


async def test_options_flow_disabling_controls_revokes_disable_permission(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 600,
            CONF_STALE_AFTER: 1200,
            CONF_ENABLE_FENCE_CONTROLS: False,
            CONF_ENABLE_FIND_COLLAR: True,
            CONF_ALLOW_FENCE_DISABLE: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SCAN_INTERVAL: 600,
        CONF_STALE_AFTER: 1200,
        CONF_ENABLE_FENCE_CONTROLS: False,
        CONF_ENABLE_FIND_COLLAR: True,
        CONF_ALLOW_FENCE_DISABLE: False,
    }


async def test_options_schema_enforces_documented_bounds(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    base = {
        CONF_SCAN_INTERVAL: 300,
        CONF_STALE_AFTER: 900,
        CONF_ENABLE_FENCE_CONTROLS: False,
        CONF_ENABLE_FIND_COLLAR: False,
        CONF_ALLOW_FENCE_DISABLE: False,
    }

    for field, invalid_values in (
        (
            CONF_SCAN_INTERVAL,
            (MIN_SCAN_INTERVAL_SECONDS - 1, MAX_SCAN_INTERVAL_SECONDS + 1),
        ),
        (
            CONF_STALE_AFTER,
            (MIN_STALE_AFTER_SECONDS - 1, MAX_STALE_AFTER_SECONDS + 1),
        ),
    ):
        for value in invalid_values:
            with pytest.raises(vol.Invalid):
                schema({**base, field: value})

    assert (
        schema(
            {
                **base,
                CONF_SCAN_INTERVAL: MIN_SCAN_INTERVAL_SECONDS,
                CONF_STALE_AFTER: MAX_STALE_AFTER_SECONDS,
            }
        )[CONF_SCAN_INTERVAL]
        == MIN_SCAN_INTERVAL_SECONDS
    )
