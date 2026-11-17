from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.halo_collar import recall
from custom_components.halo_collar.api import (
    HALO_RECALL_MAX_COMMAND_NUMBER,
    HaloApiError,
    HaloRecallAccepted,
    HaloRecallExpired,
    HaloRecallStaleNumber,
    HaloState,
    HaloWriteOutcomeUnknown,
)
from custom_components.halo_collar.const import CONF_ENABLE_RECALL_WHISTLE, CONF_STALE_AFTER
from custom_components.halo_collar.controls import HaloControlError
from custom_components.halo_collar.helpers import recall_command_support
from custom_components.halo_collar.recall import (
    REFUSALS,
    RecallOwner,
    RecallReason,
    RecallStorageError,
    Registered,
    RegistrationPending,
    RegistrationUncertain,
    Unregistered,
    recall_account_binding,
)

ACCOUNT = recall_account_binding("owner@example.com")
SERVER_TIME = "2026-11-17T12:00:00.750000Z"
PET = "pet-1"
SERIAL = "serial-1"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(recall, "time", SimpleNamespace(monotonic=value.monotonic))
    return value


class MemoryStore:
    def __init__(self, data=None) -> None:
        self.data = deepcopy(data)
        self.fail_load = False
        self.fail_saves = 0
        self.saves: list[dict] = []
        self.gate: asyncio.Event | None = None

    async def async_load(self):
        if self.fail_load:
            raise RecallStorageError("unreadable")
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_saves:
            self.fail_saves -= 1
            raise RecallStorageError("disk full")
        self.data = deepcopy(data)
        self.saves.append(deepcopy(data))

    @property
    def registration(self):
        return self.data["registration"] if self.data else None


def _saved(mobile_id=7, next_by_serial=None, *, state="registered", account=ACCOUNT):
    registration = {"state": state}
    if state == "registered":
        registration |= {"mobile_id": mobile_id, "next_by_serial": next_by_serial or {}}
    elif state in ("pending", "uncertain"):
        registration["installation_id"] = str(uuid.UUID(int=9))
    return {"version": 1, "account": account, "registration": registration}


def _state(clock: Clock | None = None) -> HaloState:
    now = datetime.now(UTC).isoformat()
    return HaloState(
        pets=[{"id": PET, "collarInfo": {"id": "collar-1", "telemetry": {"walk": None}}}],
        collars=[
            {
                "id": "collar-1",
                "serialNumber": SERIAL,
                "petInfo": {"id": PET},
                "firmware": {"features": ["telemetry", "correctionmethodswithidandexpiration"]},
                "telemetry": {"manifest": {"timestamp": now}, "walk": None},
            }
        ],
        subscription={},
        server_time=SERVER_TIME,
        server_time_monotonic=clock.now if clock else 1000.0,
    )


class FakeCoordinator:
    def __init__(self, state: HaloState, clock: Clock) -> None:
        self.data = state
        self.clock = clock
        self.last_update_success = True
        self.refreshes = 0
        self.fail = False
        self.on_refresh = None
        # Home Assistant's refresh after unload: no fetch, old data and success flag kept.
        self.shut_down = False

    async def async_refresh(self) -> None:
        self.refreshes += 1
        if self.shut_down:
            return
        if self.fail:
            self.last_update_success = False
            return
        self.data = deepcopy(self.data)
        self.data.server_time_monotonic = self.clock.now
        if self.on_refresh is not None:
            self.on_refresh(self.data)
        self.last_update_success = True


class FakeClient:
    def __init__(self) -> None:
        self.window = timedelta(minutes=1)
        self.window_reads = 0
        self.sent: list[tuple[str, object]] = []
        self.registrations: list[uuid.UUID] = []
        self.reply = HaloRecallAccepted()
        self.mobile_id = 7
        self.before_dispatch = None
        self.on_dispatch = None
        self.reply_gate: asyncio.Event | None = None

    async def async_get_recall_window(self):
        self.window_reads += 1
        if isinstance(self.window, Exception):
            raise self.window
        return self.window

    async def _dispatch(self, pre_dispatch, record):
        if self.before_dispatch is not None:
            self.before_dispatch()
        pre_dispatch()
        record()
        if self.on_dispatch is not None:
            self.on_dispatch()
        if self.reply_gate is not None:
            await self.reply_gate.wait()

    async def async_send_recall_whistle(self, pet_id, command, *, pre_dispatch):
        await self._dispatch(pre_dispatch, lambda: self.sent.append((pet_id, command)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    async def async_register_recall_device(self, installation_id, *, version_string, pre_dispatch):
        await self._dispatch(pre_dispatch, lambda: self.registrations.append(installation_id))
        if isinstance(self.mobile_id, Exception):
            raise self.mobile_id
        return self.mobile_id


class Rig:
    def __init__(self, clock: Clock, saved=None, *, enabled=True) -> None:
        self.clock = clock
        self.store = MemoryStore(saved)
        self.lock = asyncio.Lock()
        self.owner = RecallOwner(store=self.store, account=ACCOUNT, lock=self.lock)
        self.client = FakeClient()
        self.coordinator = FakeCoordinator(_state(clock), clock)
        self.entry = SimpleNamespace(options={CONF_ENABLE_RECALL_WHISTLE: enabled})
        self.live = True

    async def load(self, *, settle_hold=True) -> None:
        await self.owner.async_load()
        if settle_hold:
            self.clock.now += 60

    async def press(self) -> None:
        await self.owner.async_press(
            PET,
            coordinator=self.coordinator,
            client=self.client,
            entry=self.entry,
            is_current=lambda: self.live,
        )

    async def register(self, *, is_current=lambda: True) -> None:
        await self.owner.async_register(
            client=self.client, version_string="2026.11.1", is_current=is_current
        )

    def status(self) -> RecallReason:
        return self.owner.status(self.coordinator.data, PET, entry=self.entry, cloud_success=True)


@pytest.fixture
async def rig(clock):
    value = Rig(clock, _saved())
    await value.load()
    return value


def test_every_refusal_reason_has_a_message():
    assert set(REFUSALS) == set(RecallReason) - {RecallReason.READY}


def test_every_status_state_is_translated():
    root = Path(__file__).resolve().parents[1] / "custom_components" / "halo_collar"
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations" / "en.json").read_text())
    states = strings["entity"]["sensor"]["recall_whistle_status"]["state"]
    assert set(states) == set(RecallReason)
    assert states["ready"] == "Ready to try"


async def test_uncontested_press_spends_the_number_before_one_request(rig):
    durable_at_dispatch = []
    rig.client.on_dispatch = lambda: durable_at_dispatch.append(rig.store.registration)

    await rig.press()

    assert len(rig.client.sent) == 1
    pet_id, command = rig.client.sent[0]
    assert pet_id == PET
    assert (command.mobile_id, command.command_number) == (7, 0)
    assert command.expires_at == datetime(2026, 11, 17, 12, 0, 30, tzinfo=UTC)
    assert durable_at_dispatch[0]["next_by_serial"] == {SERIAL: 1}
    assert rig.owner.cooldown_remaining(SERIAL) == 60
    assert rig.status() is RecallReason.COOLDOWN


async def test_next_press_waits_for_the_cooldown_then_sends_the_next_number(rig):
    await rig.press()
    with pytest.raises(HaloControlError, match="cooldown"):
        await rig.press()
    assert len(rig.client.sent) == 1

    rig.clock.now += 60
    await rig.press()

    assert [command.command_number for _, command in rig.client.sent] == [0, 1]
    assert rig.store.registration["next_by_serial"] == {SERIAL: 2}


@pytest.mark.parametrize(
    ("window", "expected_seconds"),
    [(timedelta(seconds=10), 10), (timedelta(minutes=5), 30), (timedelta(seconds=29), 29)],
)
async def test_expiry_uses_the_shorter_of_halo_window_and_thirty_seconds(
    rig, window, expected_seconds
):
    rig.client.window = window
    await rig.press()
    expected = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=expected_seconds)
    assert rig.client.sent[0][1].expires_at == expected


def _set_walk(state, walk):
    state.collars[0]["telemetry"]["walk"] = walk


REFUSED_SNAPSHOTS = {
    "walk_active": (lambda s: _set_walk(s, {"startTime": "now"}), RecallReason.WALK_ACTIVE),
    "walk_unknown": (lambda s: s.collars[0]["telemetry"].pop("walk"), RecallReason.WALK_UNKNOWN),
    "report_stale": (
        lambda s: s.collars[0]["telemetry"]["manifest"].update(timestamp="2026-01-01T00:00:00Z"),
        RecallReason.REPORT_STALE,
    ),
    "report_unknown": (
        lambda s: s.collars[0]["telemetry"].pop("manifest"),
        RecallReason.REPORT_UNKNOWN,
    ),
    "firmware_unknown": (lambda s: s.collars[0].pop("firmware"), RecallReason.FIRMWARE_UNKNOWN),
    "firmware_unsupported": (
        lambda s: s.collars[0]["firmware"].update(features=["telemetry"]),
        RecallReason.FIRMWARE_UNSUPPORTED,
    ),
    "one_sided_mapping": (
        lambda s: s.collars[0]["petInfo"].update(id="pet-2"),
        RecallReason.MAPPING_UNAVAILABLE,
    ),
    "pet_missing": (lambda s: s.pets.clear(), RecallReason.MAPPING_UNAVAILABLE),
    "serial_missing": (
        lambda s: s.collars[0].pop("serialNumber"),
        RecallReason.MAPPING_UNAVAILABLE,
    ),
    "serial_shared": (
        lambda s: s.collars.append({"id": "collar-2", "serialNumber": SERIAL}),
        RecallReason.MAPPING_UNAVAILABLE,
    ),
    "conflicting_claimant": (
        lambda s: s.pets.append({"id": "pet-2", "collarInfo": {"id": "collar-1"}}),
        RecallReason.MAPPING_UNAVAILABLE,
    ),
}


@pytest.mark.parametrize("case", sorted(REFUSED_SNAPSHOTS))
async def test_fresh_snapshot_refusals_send_nothing_and_reserve_nothing(rig, case):
    mutate, reason = REFUSED_SNAPSHOTS[case]
    rig.coordinator.on_refresh = mutate

    with pytest.raises(HaloControlError) as raised:
        await rig.press()

    assert str(raised.value) == REFUSALS[reason]
    assert rig.status() is reason
    assert rig.client.sent == []
    assert rig.store.saves == []
    assert rig.owner.cooldown_remaining(SERIAL) == 0


async def test_disabled_option_refuses_before_any_halo_request(clock):
    value = Rig(clock, _saved(), enabled=False)
    await value.load()
    with pytest.raises(HaloControlError, match="disabled"):
        await value.press()
    assert (value.client.window_reads, value.coordinator.refreshes) == (0, 0)
    assert value.status() is RecallReason.DISABLED


@pytest.mark.parametrize(
    ("saved", "reason"),
    [
        (None, RecallReason.REGISTRATION_REQUIRED),
        (_saved(state="pending"), RecallReason.REGISTRATION_UNCERTAIN),
        (_saved(state="uncertain"), RecallReason.REGISTRATION_UNCERTAIN),
        (_saved(account="another-account"), RecallReason.STORAGE_UNAVAILABLE),
        (_saved(mobile_id=2**15), RecallReason.STORAGE_UNAVAILABLE),
        (_saved(next_by_serial={SERIAL: -1}), RecallReason.STORAGE_UNAVAILABLE),
        ({"version": 2}, RecallReason.STORAGE_UNAVAILABLE),
        ([], RecallReason.STORAGE_UNAVAILABLE),
    ],
)
async def test_unusable_registration_refuses_before_any_halo_request(clock, saved, reason):
    value = Rig(clock, saved)
    await value.load()

    with pytest.raises(HaloControlError) as raised:
        await value.press()

    assert str(raised.value) == REFUSALS[reason]
    assert value.status() is reason
    assert (value.client.window_reads, value.coordinator.refreshes) == (0, 0)


async def test_unreadable_storage_never_falls_back_to_a_new_registration(clock):
    value = Rig(clock, None)
    value.store.fail_load = True
    await value.load()

    with pytest.raises(HaloControlError, match="could not be read"):
        await value.register()

    assert value.client.registrations == []
    assert value.status() is RecallReason.STORAGE_UNAVAILABLE


async def test_cloud_refresh_failure_refuses(rig):
    rig.coordinator.fail = True
    with pytest.raises(HaloControlError, match="Could not refresh"):
        await rig.press()
    assert rig.client.sent == []


async def test_missing_halo_window_refuses_before_refresh(rig):
    rig.client.window = HaloApiError("Halo did not report a valid Recall command window")
    with pytest.raises(HaloApiError, match="window"):
        await rig.press()
    assert (rig.coordinator.refreshes, rig.client.sent) == (0, [])


async def test_refresh_that_fetched_nothing_refuses(rig):
    rig.coordinator.shut_down = True

    with pytest.raises(HaloControlError) as raised:
        await rig.press()

    assert str(raised.value) == REFUSALS[RecallReason.CLOUD_UNAVAILABLE]
    assert (rig.client.sent, rig.store.saves) == ([], [])


async def test_replaced_runtime_sends_nothing(rig):
    def reload():
        rig.live = False

    rig.client.before_dispatch = reload

    with pytest.raises(HaloControlError, match="reloaded"):
        await rig.press()

    assert rig.client.sent == []
    assert rig.owner.cooldown_remaining(SERIAL) == 0


async def test_missing_server_clock_refuses(rig):
    rig.coordinator.on_refresh = lambda s: setattr(s, "server_time", None)
    with pytest.raises(HaloControlError, match="clock"):
        await rig.press()
    assert rig.client.sent == []


async def test_too_little_lifetime_at_dispatch_refuses_after_reserving(rig):
    def slow_token_refresh():
        rig.clock.now += 28.5

    rig.client.before_dispatch = slow_token_refresh

    with pytest.raises(HaloControlError, match="lifetime"):
        await rig.press()

    assert rig.client.sent == []
    assert rig.owner.cooldown_remaining(SERIAL) == 0
    assert rig.store.registration["next_by_serial"] == {SERIAL: 1}


@pytest.mark.parametrize(
    "change",
    ["option_revoked", "walk_active", "serial_changed", "rebound", "stale_after_tightened"],
)
async def test_dispatch_boundary_rechecks_current_state(rig, change):
    def at_boundary():
        state = rig.coordinator.data
        if change == "option_revoked":
            rig.entry.options = {CONF_ENABLE_RECALL_WHISTLE: False}
        elif change == "walk_active":
            _set_walk(state, {"startTime": "now"})
        elif change == "serial_changed":
            state.collars[0]["serialNumber"] = "serial-2"
        elif change == "rebound":
            state.pets[0]["id"] = "pet-2"
            state.collars[0]["petInfo"]["id"] = "pet-2"
        else:
            rig.entry.options = {**rig.entry.options, CONF_STALE_AFTER: 0}

    rig.client.before_dispatch = at_boundary

    with pytest.raises(HaloControlError):
        await rig.press()

    assert rig.client.sent == []
    assert rig.owner.cooldown_remaining(SERIAL) == 0


@pytest.mark.parametrize(
    ("saved_next", "current", "expected_next"),
    [(0, 40, 41), (0, None, 1), (50, 3, 51), (0, HALO_RECALL_MAX_COMMAND_NUMBER, 2**31)],
)
async def test_old_command_number_resyncs_without_retrying(
    clock, saved_next, current, expected_next
):
    value = Rig(clock, _saved(next_by_serial={SERIAL: saved_next}))
    await value.load()
    value.client.reply = HaloRecallStaleNumber(current)

    with pytest.raises(HaloControlError, match="old Recall command number"):
        await value.press()

    assert len(value.client.sent) == 1
    assert value.store.registration["next_by_serial"] == {SERIAL: expected_next}
    assert value.owner.cooldown_remaining(SERIAL) == 60


@pytest.mark.parametrize(
    "reply",
    [HaloRecallExpired(), HaloWriteOutcomeUnknown("lost")],
    ids=["expired", "unknown"],
)
async def test_unsuccessful_reply_is_an_error_and_keeps_number_and_cooldown(rig, reply):
    rig.client.reply = reply
    with pytest.raises(HaloControlError):
        await rig.press()
    assert len(rig.client.sent) == 1
    assert rig.store.registration["next_by_serial"] == {SERIAL: 1}
    assert rig.owner.cooldown_remaining(SERIAL) == 60


async def test_last_command_number_is_sent_once_then_exhausted(clock):
    value = Rig(clock, _saved(next_by_serial={SERIAL: HALO_RECALL_MAX_COMMAND_NUMBER}))
    await value.load()

    await value.press()
    value.clock.now += 60

    assert value.client.sent[0][1].command_number == HALO_RECALL_MAX_COMMAND_NUMBER
    assert value.status() is RecallReason.COUNTER_EXHAUSTED
    with pytest.raises(HaloControlError, match="every Recall command number"):
        await value.press()
    assert len(value.client.sent) == 1


async def test_reservation_storage_failure_sends_nothing(rig):
    rig.store.fail_saves = 1
    with pytest.raises(HaloControlError, match="nothing was sent"):
        await rig.press()
    assert rig.client.sent == []
    assert rig.owner.cooldown_remaining(SERIAL) == 0

    await rig.press()
    assert rig.client.sent[0][1].command_number == 0


async def test_cancel_during_reservation_finishes_the_write_and_sends_nothing(rig):
    rig.store.gate = asyncio.Event()
    task = asyncio.create_task(rig.press())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()

    rig.store.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.store.registration["next_by_serial"] == {SERIAL: 1}
    assert rig.client.sent == []
    assert not rig.lock.locked()


async def test_cancel_after_dispatch_waits_for_the_reply_and_resync(rig):
    rig.client.reply = HaloRecallStaleNumber(9)
    rig.client.reply_gate = asyncio.Event()
    task = asyncio.create_task(rig.press())
    await asyncio.sleep(0.01)
    assert len(rig.client.sent) == 1
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert rig.lock.locked()

    rig.client.reply_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.store.registration["next_by_serial"] == {SERIAL: 10}
    assert not rig.lock.locked()


async def test_press_waits_for_the_shared_control_lock(rig):
    await rig.lock.acquire()
    task = asyncio.create_task(rig.press())
    await asyncio.sleep(0.01)
    assert rig.client.window_reads == 0
    rig.lock.release()
    await task
    assert len(rig.client.sent) == 1


async def test_counters_survive_a_restart_behind_a_cooldown_hold(rig, clock):
    await rig.press()
    restarted = RecallOwner(store=rig.store, account=ACCOUNT, lock=asyncio.Lock())
    clock.now += 60

    await restarted.async_load()

    assert restarted.registration == Registered(7, {SERIAL: 1})
    assert restarted.cooldown_remaining("any-serial") == 60


async def test_registration_saves_intent_before_one_request_then_the_mobile_id(clock):
    value = Rig(clock, None)
    durable_at_dispatch = []
    value.client.on_dispatch = lambda: durable_at_dispatch.append(value.store.registration)
    await value.load()

    await value.register()
    await value.register()

    assert len(value.client.registrations) == 1
    assert durable_at_dispatch == [
        {"state": "pending", "installation_id": str(value.client.registrations[0])}
    ]
    assert value.store.registration == {"state": "registered", "mobile_id": 7, "next_by_serial": {}}
    assert value.owner.registration == Registered(7)
    assert value.status() is RecallReason.READY


async def test_ambiguous_registration_is_never_resent(clock):
    value = Rig(clock, None)
    value.client.mobile_id = HaloWriteOutcomeUnknown("lost")
    await value.load()

    with pytest.raises(HaloWriteOutcomeUnknown):
        await value.register()
    with pytest.raises(HaloControlError, match="never retried"):
        await value.register()

    assert len(value.client.registrations) == 1
    assert isinstance(value.owner.registration, RegistrationUncertain)
    assert value.store.registration["state"] == "uncertain"


@pytest.mark.parametrize("failure", ["veto", "token", "cancel"])
async def test_registration_that_never_dispatched_can_be_retried(clock, failure):
    value = Rig(clock, None)
    await value.load()
    if failure == "veto":
        attempt = value.register(is_current=iter([True, False]).__next__)
        with pytest.raises(HaloControlError, match="reloaded"):
            await attempt
    elif failure == "token":

        def token_refresh_fails():
            raise HaloApiError("token")

        value.client.before_dispatch = token_refresh_fails
        with pytest.raises(HaloApiError):
            await value.register()
        value.client.before_dispatch = None
    else:
        value.client.before_dispatch = None
        value.store.gate = asyncio.Event()
        task = asyncio.create_task(value.register())
        await asyncio.sleep(0.01)
        task.cancel()
        value.store.gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        value.store.gate = None

    assert value.client.registrations == []
    assert value.owner.registration == Unregistered()
    await value.register()
    assert len(value.client.registrations) == 1


async def test_cancelled_registration_after_dispatch_still_records_the_result(clock):
    value = Rig(clock, None)
    value.client.reply_gate = asyncio.Event()
    await value.load()
    task = asyncio.create_task(value.register())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    value.client.reply_gate.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert value.owner.registration == Registered(7)
    assert value.store.registration["state"] == "registered"


async def test_registration_intent_storage_failure_sends_nothing(clock):
    value = Rig(clock, None)
    value.store.fail_saves = 1
    await value.load()
    with pytest.raises(HaloControlError, match="intent"):
        await value.register()
    assert value.client.registrations == []
    assert value.owner.registration == Unregistered()


async def test_unsaved_registration_result_leaves_recovery_required(clock):
    value = Rig(clock, None)
    await value.load()
    value.client.on_dispatch = lambda: setattr(value.store, "fail_saves", 1)
    with pytest.raises(HaloControlError, match="could not be saved"):
        await value.register()

    assert isinstance(value.owner.registration, RegistrationPending)
    assert value.status() is RecallReason.REGISTRATION_UNCERTAIN


async def test_unsaved_ambiguous_registration_outcome_still_blocks_a_second_request(clock):
    value = Rig(clock, None)
    value.client.mobile_id = HaloWriteOutcomeUnknown("lost")
    await value.load()
    value.client.on_dispatch = lambda: setattr(value.store, "fail_saves", 1)

    with pytest.raises(HaloWriteOutcomeUnknown):
        await value.register()
    with pytest.raises(HaloControlError):
        await value.register()

    assert len(value.client.registrations) == 1
    assert value.store.registration["state"] == "pending"
    assert isinstance(value.owner.registration, RegistrationPending)


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (["CorrectionMethodsWithIdAndExpiration"], True),
        (["correctionmethodswithidandexpiration", "fota"], True),
        ([25], True),
        ([24, "fota"], False),
        ([], False),
        ([True], None),
        ([{"id": 25}], None),
        (None, None),
        ("correctionmethodswithidandexpiration", None),
    ],
)
def test_recall_command_support_requires_feature_25(features, expected):
    collar = {"firmware": {} if features is None else {"features": features}}
    assert recall_command_support(collar) is expected
