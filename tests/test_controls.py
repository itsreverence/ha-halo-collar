from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.halo_collar.api import HaloWriteOutcomeUnknown
from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_STALE_AFTER,
)
from custom_components.halo_collar.controls import (
    HaloControlError,
    async_set_fence_mode,
    control_lock_for,
    remove_control_lock,
)


def _collar(*, fresh: bool = True, walk=None, timestamp=None):
    timestamp = timestamp or (
        datetime.now(UTC).isoformat() if fresh else "2026-01-01T00:00:00+00:00"
    )
    return {
        "id": "collar-1",
        "petInfo": {"id": "pet-1"},
        "telemetry": {"manifest": {"timestamp": timestamp}, "walk": walk},
    }


def _pet(*, fences_on: bool = True, synchronized: bool = True, walk=None):
    return {
        "id": "pet-1",
        "collarInfo": {"id": "collar-1", "telemetry": {"walk": walk}},
        "isFencesSynchronized": synchronized,
        "telemetry": {"mode": {"fencesOn": fences_on}},
    }


class FakeCoordinator:
    def __init__(self, states, successes=None):
        self.states = list(states)
        self.successes = list(successes or [True] * len(self.states))
        self.index = -1
        self.last_update_success = True
        self.refreshes = 0

    async def async_refresh(self):
        self.refreshes += 1
        self.index = min(self.index + 1, len(self.states) - 1)
        self.last_update_success = self.successes[self.index]

    async def async_request_refresh(self):
        raise AssertionError("control transactions must bypass the debouncer")

    @property
    def state(self):
        return self.states[self.index]


class FakeClient:
    def __init__(self):
        self.writes = []

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        return {"ok": True}


class RevokingAtDispatchClient(FakeClient):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        self.entry.options[CONF_ALLOW_FENCE_DISABLE] = False
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        return {"ok": True}


class MutatingAtDispatchClient(FakeClient):
    def __init__(self, mutate):
        super().__init__()
        self.mutate = mutate

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        self.mutate()
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        return {"ok": True}


class UnknownOutcomeClient(FakeClient):
    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        raise HaloWriteOutcomeUnknown("simulated dispatched write failure")


class BlockingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_set_fences_enabled(self, pet_id, *, enabled, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        self.writes.append((pet_id, enabled))
        self.started.set()
        await self.release.wait()
        return {"ok": True}


def _entry(*, enable=True, allow_disable=False):
    return SimpleNamespace(
        options={
            CONF_ENABLE_FENCE_CONTROLS: enable,
            CONF_ALLOW_FENCE_DISABLE: allow_disable,
        }
    )


async def _execute(coordinator, client, entry, *, enabled, control_lock=None):
    await async_set_fence_mode(
        coordinator=coordinator,
        client=client,
        entry=entry,
        control_lock=control_lock or asyncio.Lock(),
        state_getter=lambda: coordinator.state,
        enabled=enabled,
    )


def test_control_lock_is_shared_by_all_entities_and_survives_reload():
    domain_data = {}

    old_setup_lock = control_lock_for(domain_data, "entry-1")
    new_setup_lock = control_lock_for(domain_data, "entry-1")

    assert old_setup_lock is new_setup_lock
    assert control_lock_for(domain_data, "entry-2") is not old_setup_lock

    remove_control_lock(domain_data, "entry-1")
    assert control_lock_for(domain_data, "entry-1") is not old_setup_lock


def test_control_lock_survives_exact_entry_payload_pop_used_by_unload():
    domain_data = {"entry-1": {"coordinator": object()}}
    old_setup_lock = control_lock_for(domain_data, "entry-1")

    domain_data.pop("entry-1")
    domain_data["entry-1"] = {"coordinator": object()}

    assert control_lock_for(domain_data, "entry-1") is old_setup_lock


@pytest.mark.asyncio
async def test_reloaded_entity_cannot_enter_while_old_setup_holds_entry_lock():
    domain_data = {}
    old_setup_lock = control_lock_for(domain_data, "entry-1")
    await old_setup_lock.acquire()

    new_setup_lock = control_lock_for(domain_data, "entry-1")
    entered = asyncio.Event()

    async def new_entity_action():
        async with new_setup_lock:
            entered.set()

    action_task = asyncio.create_task(new_entity_action())
    await asyncio.sleep(0)

    assert entered.is_set() is False
    assert new_setup_lock is old_setup_lock

    old_setup_lock.release()
    await action_task
    assert entered.is_set() is True


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
        (_pet(walk="malformed"), _collar(), "active walk"),
        (
            {**_pet(), "collarInfo": {"id": "collar-1", "telemetry": {}}},
            _collar(),
            "active walk",
        ),
        (_pet(), _collar(walk={"id": "walk-1"}), "active walk"),
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
async def test_option_revocation_during_preflight_blocks_pending_write():
    entry = _entry(allow_disable=True)
    coordinator = FakeCoordinator([(_pet(), _collar())])
    original_refresh = coordinator.async_refresh

    async def refresh_and_revoke():
        await original_refresh()
        entry.options[CONF_ALLOW_FENCE_DISABLE] = False

    coordinator.async_refresh = refresh_and_revoke
    client = FakeClient()

    with pytest.raises(HaloControlError, match="not allowed"):
        await _execute(coordinator, client, entry, enabled=False)

    assert coordinator.refreshes == 1
    assert client.writes == []


@pytest.mark.asyncio
async def test_option_revocation_at_actual_dispatch_boundary_blocks_put():
    entry = _entry(allow_disable=True)
    coordinator = FakeCoordinator([(_pet(), _collar())])
    client = RevokingAtDispatchClient(entry)

    with pytest.raises(HaloControlError, match="not allowed"):
        await _execute(coordinator, client, entry, enabled=False)

    assert coordinator.refreshes == 1
    assert client.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "replacement", "message"),
    [
        (True, (_pet() | {"id": "pet-2"}, _collar()), "mapping changed"),
        (True, (_pet(), _collar() | {"id": "collar-2"}), "mapping changed"),
        (False, (_pet(walk={"id": "walk-2"}), _collar()), "active walk"),
        (False, (_pet(synchronized=False), _collar()), "synchronized"),
        (False, (_pet(), _collar(fresh=False)), "stale"),
    ],
)
async def test_dispatch_boundary_revalidates_identity_and_disable_safety(
    enabled, replacement, message
):
    coordinator = FakeCoordinator([(_pet(), _collar())])

    def mutate():
        coordinator.states[coordinator.index] = replacement

    client = MutatingAtDispatchClient(mutate)

    with pytest.raises(HaloControlError, match=message):
        await _execute(
            coordinator,
            client,
            _entry(allow_disable=True),
            enabled=enabled,
        )

    assert coordinator.refreshes == 1
    assert client.writes == []


@pytest.mark.asyncio
async def test_dispatch_boundary_preserves_strictest_freshness_threshold():
    timestamp = (datetime.now(UTC) - timedelta(seconds=500)).isoformat()
    coordinator = FakeCoordinator([(_pet(), _collar(timestamp=timestamp))])
    entry = _entry(allow_disable=True)
    entry.options[CONF_STALE_AFTER] = 900

    def tighten_threshold():
        entry.options[CONF_STALE_AFTER] = 120

    client = MutatingAtDispatchClient(tighten_threshold)

    with pytest.raises(HaloControlError, match="stale"):
        await _execute(coordinator, client, entry, enabled=False)

    assert coordinator.refreshes == 1
    assert client.writes == []


@pytest.mark.asyncio
async def test_looser_midflight_threshold_does_not_weaken_confirmation():
    fresh = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    newly_stale = (datetime.now(UTC) - timedelta(seconds=500)).isoformat()
    coordinator = FakeCoordinator(
        [
            (_pet(fences_on=False), _collar(timestamp=fresh)),
            (_pet(fences_on=True), _collar(timestamp=newly_stale)),
        ]
    )
    entry = _entry()
    entry.options[CONF_STALE_AFTER] = 120

    def loosen_threshold():
        entry.options[CONF_STALE_AFTER] = 900

    client = MutatingAtDispatchClient(loosen_threshold)

    with pytest.raises(HaloControlError, match="did not confirm"):
        await _execute(coordinator, client, entry, enabled=True)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]


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
@pytest.mark.parametrize("enabled", [True, False])
async def test_commands_reject_far_future_telemetry_before_write(enabled):
    coordinator = FakeCoordinator([(_pet(), _collar(timestamp="2999-01-01T00:00:00+00:00"))])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="stale"):
        await _execute(
            coordinator,
            client,
            _entry(allow_disable=True),
            enabled=enabled,
        )

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
async def test_unknown_write_outcome_reconciles_to_confirmed_state_without_retry():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=False), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = UnknownOutcomeClient()

    await _execute(coordinator, client, _entry(), enabled=True)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]


@pytest.mark.asyncio
async def test_unknown_write_outcome_refreshes_then_reports_ambiguous_state():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=True), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = UnknownOutcomeClient()

    with pytest.raises(HaloControlError, match="outcome is unknown"):
        await _execute(coordinator, client, _entry(allow_disable=True), enabled=False)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", False)]


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


@pytest.mark.asyncio
async def test_post_write_confirmation_rejects_changed_target_identity():
    changed_pet = _pet(fences_on=True) | {"id": "pet-2"}
    coordinator = FakeCoordinator([(_pet(fences_on=False), _collar()), (changed_pet, _collar())])
    client = FakeClient()

    with pytest.raises(HaloControlError, match="did not confirm"):
        await _execute(coordinator, client, _entry(), enabled=True)

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_waits_for_reconciliation_and_lock_release():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=False), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = BlockingClient()
    control_lock = asyncio.Lock()

    action_task = asyncio.create_task(
        _execute(
            coordinator,
            client,
            _entry(),
            enabled=True,
            control_lock=control_lock,
        )
    )
    await client.started.wait()

    action_task.cancel()
    await asyncio.sleep(0)

    assert control_lock.locked() is True
    assert coordinator.refreshes == 1
    assert client.writes == [("pet-1", True)]

    client.release.set()
    with pytest.raises(asyncio.CancelledError):
        await action_task

    assert coordinator.refreshes == 2
    assert control_lock.locked() is False
    assert client.writes == [("pet-1", True)]


@pytest.mark.asyncio
async def test_contradictory_transactions_share_one_collar_lock():
    coordinator = FakeCoordinator(
        [(_pet(fences_on=False), _collar()), (_pet(fences_on=True), _collar())]
    )
    client = BlockingClient()
    entry = _entry(allow_disable=True)
    control_lock = asyncio.Lock()

    enable_task = asyncio.create_task(
        _execute(
            coordinator,
            client,
            entry,
            enabled=True,
            control_lock=control_lock,
        )
    )
    await client.started.wait()

    disable_task = asyncio.create_task(
        _execute(
            coordinator,
            client,
            entry,
            enabled=False,
            control_lock=control_lock,
        )
    )
    await asyncio.sleep(0)

    assert coordinator.refreshes == 1
    assert client.writes == [("pet-1", True)]

    disable_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disable_task
    client.release.set()
    await enable_task

    assert coordinator.refreshes == 2
    assert client.writes == [("pet-1", True)]
