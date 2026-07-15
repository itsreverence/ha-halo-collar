from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
)
from custom_components.halo_collar.controls import HaloControlError, async_set_fence_mode


def _collar(*, fresh: bool = True, walk=None):
    timestamp = datetime.now(UTC).isoformat() if fresh else "2026-01-01T00:00:00+00:00"
    return {
        "id": "collar-1",
        "petInfo": {"id": "pet-1"},
        "telemetry": {"manifest": {"timestamp": timestamp}, "walk": walk},
    }


def _pet(*, fences_on: bool = True, synchronized: bool = True, walk=None):
    return {
        "id": "pet-1",
        "collarInfo": {"id": "collar-1"},
        "isFencesSynchronized": synchronized,
        "telemetry": {"mode": {"fencesOn": fences_on}, "walk": walk},
    }


class FakeCoordinator:
    def __init__(self, states, successes=None):
        self.states = list(states)
        self.successes = list(successes or [True] * len(self.states))
        self.index = -1
        self.last_update_success = True
        self.refreshes = 0

    async def async_request_refresh(self):
        self.refreshes += 1
        self.index = min(self.index + 1, len(self.states) - 1)
        self.last_update_success = self.successes[self.index]

    @property
    def state(self):
        return self.states[self.index]


class FakeClient:
    def __init__(self):
        self.writes = []

    async def async_set_fences_enabled(self, pet_id, *, enabled):
        self.writes.append((pet_id, enabled))
        return {"ok": True}


def _entry(*, enable=True, allow_disable=False):
    return SimpleNamespace(
        options={
            CONF_ENABLE_FENCE_CONTROLS: enable,
            CONF_ALLOW_FENCE_DISABLE: allow_disable,
        }
    )


async def _execute(coordinator, client, entry, *, enabled):
    await async_set_fence_mode(
        coordinator=coordinator,
        client=client,
        entry=entry,
        state_getter=lambda: coordinator.state,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_direct_enable_call_revalidates_option_before_refresh_or_write():
    coordinator = FakeCoordinator([(_pet(), _collar())])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="controls are disabled"):
        await _execute(coordinator, client, _entry(enable=False), enabled=True)

    assert coordinator.refreshes == 0
    assert client.writes == []


@pytest.mark.asyncio
async def test_direct_disable_call_revalidates_option_before_refresh_or_write():
    coordinator = FakeCoordinator([(_pet(), _collar())])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="not allowed"):
        await _execute(coordinator, client, _entry(allow_disable=False), enabled=False)

    assert coordinator.refreshes == 0
    assert client.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pet", "collar", "message"),
    [
        (_pet(synchronized=False), _collar(), "synchronized"),
        (_pet(walk={"id": "walk-1"}), _collar(), "active walk"),
        (_pet(), _collar(fresh=False), "stale"),
    ],
)
async def test_direct_disable_call_fails_closed_after_fresh_read(pet, collar, message):
    coordinator = FakeCoordinator([(pet, collar)])
    client = FakeClient()

    with pytest.raises(HaloControlError, match=message):
        await _execute(coordinator, client, _entry(allow_disable=True), enabled=False)

    assert coordinator.refreshes == 1
    assert client.writes == []


@pytest.mark.asyncio
async def test_refresh_failure_blocks_write():
    coordinator = FakeCoordinator([(_pet(), _collar())], successes=[False])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="Could not refresh"):
        await _execute(coordinator, client, _entry(), enabled=True)

    assert client.writes == []


@pytest.mark.asyncio
async def test_enable_command_rejects_stale_state_before_write():
    coordinator = FakeCoordinator([(_pet(), _collar(fresh=False))])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="stale"):
        await _execute(coordinator, client, _entry(), enabled=True)

    assert client.writes == []


@pytest.mark.asyncio
async def test_enable_command_refreshes_writes_once_and_confirms_reported_state():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=False), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = FakeClient()

    await _execute(coordinator, client, _entry(), enabled=True)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]


@pytest.mark.asyncio
async def test_post_write_refresh_failure_reports_ambiguous_outcome_without_retry():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=False), _collar()), (_pet(fences_on=False), _collar())],
        successes=[True, False],
    )
    client = FakeClient()

    with pytest.raises(HaloControlError, match="command was sent"):
        await _execute(coordinator, client, _entry(), enabled=True)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]


@pytest.mark.asyncio
async def test_unconfirmed_result_reports_ambiguous_outcome_without_second_write():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=True), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = FakeClient()

    with pytest.raises(HaloControlError, match="did not confirm"):
        await _execute(coordinator, client, _entry(allow_disable=True), enabled=False)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", False)]
