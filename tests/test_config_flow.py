from __future__ import annotations

# Home Assistant is intentionally checked before its optional test dependencies.
# ruff: noqa: E402, I001

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol
from voluptuous_serialize import convert

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.halo_collar.api import HaloApiError, HaloAuthError
from custom_components.halo_collar.config_flow import HaloCollarConfigFlow
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
    password_field = convert(
        initial["data_schema"].schema[CONF_PASSWORD], custom_serializer=cv.custom_serializer
    )
    assert password_field["selector"]["text"]["type"] == "password"
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
    password_field = convert(
        result["data_schema"].schema[CONF_PASSWORD], custom_serializer=cv.custom_serializer
    )
    assert password_field["selector"]["text"]["type"] == "password"


async def test_user_flow_aborts_duplicate_normalized_email_before_login(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    initial = await _start_user_flow(hass)

    with patch("custom_components.halo_collar.config_flow._client") as client_factory:
        result = await hass.config_entries.flow.async_configure(initial["flow_id"], _user_input())

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    client_factory.assert_not_called()


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
    password_field = convert(
        result["data_schema"].schema[CONF_PASSWORD], custom_serializer=cv.custom_serializer
    )
    assert password_field["selector"]["text"]["type"] == "password"


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


async def test_reauth_rejects_different_account_without_login_or_entry_changes(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    original_options = dict(entry.options)
    original_unique_id = entry.unique_id
    client = _client()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    assert result["type"] is FlowResultType.FORM

    with (
        patch("custom_components.halo_collar.config_flow._client", return_value=client) as factory,
        patch.object(hass.config_entries, "async_reload", return_value=True) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: " Other@Example.COM ", CONF_PASSWORD: "synthetic-password"},
        )
        await hass.async_block_till_done()

    assert {
        "login_calls": client.async_login.call_count,
        "reload_calls": reload_entry.call_count,
        "data": dict(entry.data),
        "options": dict(entry.options),
        "unique_id": entry.unique_id,
    } == {
        "login_calls": 0,
        "reload_calls": 0,
        "data": original_data,
        "options": original_options,
        "unique_id": original_unique_id,
    }
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EMAIL: "account_mismatch"}
    factory.assert_not_called()
    password_field = convert(
        result["data_schema"].schema[CONF_PASSWORD], custom_serializer=cv.custom_serializer
    )
    assert password_field["selector"]["text"]["type"] == "password"


@pytest.mark.parametrize("unique_id", ["owner@example.com", " Owner@Example.COM ", None])
async def test_reauth_normalized_same_account_preserves_identity_and_control_options(
    hass, unique_id
):
    entry = _entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, unique_id=unique_id, data={**entry.data, CONF_EMAIL: " Owner@Example.COM "}
    )
    original_data = dict(entry.data)
    original_options = dict(entry.options)
    original_unique_id = entry.unique_id
    client = _client()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )

    password_field = convert(
        result["data_schema"].schema[CONF_PASSWORD], custom_serializer=cv.custom_serializer
    )
    assert password_field["selector"]["text"]["type"] == "password"

    with (
        patch("custom_components.halo_collar.config_flow._client", return_value=client) as factory,
        patch.object(hass.config_entries, "async_reload", return_value=True) as reload_entry,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_EMAIL: " Owner@Example.COM ",
                CONF_PASSWORD: "synthetic-password",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        **original_data,
        CONF_EMAIL: "owner@example.com",
        CONF_ACCESS_TOKEN: "new-access",
        CONF_REFRESH_TOKEN: "new-refresh",
        CONF_EXPIRES_AT: 4_102_444_800.0,
    }
    assert entry.options == original_options
    assert entry.unique_id == original_unique_id
    reload_entry.assert_awaited_once_with(entry.entry_id)
    client.async_login.assert_awaited_once_with(
        "owner@example.com",
        "synthetic-password",
        scope="openid email offline_access api.dogpark",
    )
    assert CONF_PASSWORD not in entry.data
    factory.assert_called_once_with(hass, DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET)


@pytest.mark.parametrize("email", [None, "", "  ", 123, "other@example.com"])
async def test_reauth_aborts_unusable_saved_identity_before_login(hass, email):
    entry = _entry()
    entry.add_to_hass(hass)
    data = {**entry.data, CONF_EMAIL: email}
    if email is None:
        del data[CONF_EMAIL]
    hass.config_entries.async_update_entry(entry, data=data)
    options = dict(entry.options)

    with (
        patch("custom_components.halo_collar.config_flow._client") as factory,
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_account_unavailable"
    assert result["description_placeholders"] == {
        "recovery_url": (
            "https://github.com/itsreverence/ha-halo-collar/blob/main/SUPPORT.md#recover-authentication"
        )
    }
    factory.assert_not_called()
    reload_entry.assert_not_called()
    assert entry.data == data
    assert entry.options == options
    assert entry.unique_id == "owner@example.com"


async def test_reauth_missing_entry_aborts_without_client(hass):
    flow = HaloCollarConfigFlow()
    flow.hass = hass
    flow.context = {"source": config_entries.SOURCE_REAUTH, "entry_id": "removed-entry"}
    with patch("custom_components.halo_collar.config_flow._client") as factory:
        result = await flow.async_step_reauth_confirm(
            {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_account_unavailable"
    factory.assert_not_called()


@pytest.mark.parametrize("change", ["remove", "email", "unique_id", "both"])
async def test_reauth_checks_live_identity_before_client_construction(hass, change):
    entry = _entry()
    entry.add_to_hass(hass)
    initial = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    if change == "remove":
        await hass.config_entries.async_remove(entry.entry_id)
    else:
        updates = {}
        if change in ("email", "both"):
            updates["data"] = {**entry.data, CONF_EMAIL: "other@example.com"}
        if change in ("unique_id", "both"):
            updates["unique_id"] = "other@example.com"
        hass.config_entries.async_update_entry(entry, **updates)
    before = (dict(entry.data), dict(entry.options), entry.unique_id)

    with (
        patch("custom_components.halo_collar.config_flow._client") as factory,
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        if change == "remove":
            with pytest.raises(UnknownFlow):
                await hass.config_entries.flow.async_configure(
                    initial["flow_id"],
                    {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
                )
        else:
            result = await hass.config_entries.flow.async_configure(
                initial["flow_id"],
                {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
            )
        await hass.async_block_till_done()

    if change == "both":
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_EMAIL: "account_mismatch"}
    elif change != "remove":
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_account_unavailable"
    factory.assert_not_called()
    reload_entry.assert_not_called()
    assert (dict(entry.data), dict(entry.options), entry.unique_id) == before


@pytest.mark.parametrize("change", ["remove", "email", "unique_id", "client_id", "client_secret"])
async def test_reauth_discards_login_if_identity_or_oauth_changes(hass, change):
    entry = _entry()
    entry.add_to_hass(hass)
    initial = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    entered = asyncio.Event()
    resume = asyncio.Event()
    client = _client()

    async def login(*args, **kwargs):
        entered.set()
        await resume.wait()

    client.async_login.side_effect = login
    with (
        patch("custom_components.halo_collar.config_flow._client", return_value=client),
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        task = asyncio.create_task(
            hass.config_entries.flow.async_configure(
                initial["flow_id"],
                {CONF_EMAIL: "owner@example.com", CONF_PASSWORD: "synthetic-password"},
            )
        )
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                if change == "remove":
                    await hass.config_entries.async_remove(entry.entry_id)
                elif change == "unique_id":
                    hass.config_entries.async_update_entry(entry, unique_id=None)
                else:
                    hass.config_entries.async_update_entry(
                        entry, data={**entry.data, change: "changed"}
                    )
                before = (dict(entry.data), dict(entry.options), entry.unique_id)
                resume.set()
                result = await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_entry_changed"
    assert result["description_placeholders"] == {
        "recovery_url": (
            "https://github.com/itsreverence/ha-halo-collar/blob/main/SUPPORT.md#recover-authentication"
        )
    }
    reload_entry.assert_not_called()
    assert (dict(entry.data), dict(entry.options), entry.unique_id) == before


async def test_reauth_uses_live_effective_oauth_and_preserves_concurrent_updates(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    flow = HaloCollarConfigFlow()
    flow.hass = hass
    flow.context = {"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}
    assert (await flow.async_step_reauth(entry.data))["type"] is FlowResultType.FORM
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CLIENT_ID: "custom-client", CONF_CLIENT_SECRET: ""}
    )
    client = _client()
    options = {**entry.options, CONF_ENABLE_FIND_COLLAR: True}

    async def login(*args, **kwargs):
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_CLIENT_SECRET: DEFAULT_CLIENT_SECRET,
                CONF_REFRESH_TOKEN: "concurrent-rotation",
                "unrelated_setting": "preserved",
            },
            options=options,
        )
        await asyncio.sleep(0)

    client.async_login.side_effect = login
    with (
        patch("custom_components.halo_collar.config_flow._client", return_value=client) as factory,
        patch.object(hass.config_entries, "async_reload", return_value=True) as reload_entry,
    ):
        result = await flow.async_step_reauth_confirm(
            {
                CONF_EMAIL: "owner@example.com",
                CONF_PASSWORD: "synthetic-password",
                CONF_CLIENT_ID: "untrusted-override",
                CONF_CLIENT_SECRET: "untrusted-secret",
                "unknown_key": "must-not-persist",
            }
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    factory.assert_called_once_with(hass, "custom-client", DEFAULT_CLIENT_SECRET)
    assert entry.data == {
        CONF_EMAIL: "owner@example.com",
        CONF_ACCESS_TOKEN: "new-access",
        CONF_REFRESH_TOKEN: "new-refresh",
        CONF_EXPIRES_AT: 4_102_444_800.0,
        CONF_CLIENT_ID: "custom-client",
        CONF_CLIENT_SECRET: DEFAULT_CLIENT_SECRET,
        "unrelated_setting": "preserved",
    }
    assert entry.options == options
    assert entry.unique_id == "owner@example.com"
    reload_entry.assert_awaited_once_with(entry.entry_id)
