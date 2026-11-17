"""Exhaustive interleaving check of the guarded write transactions.

This runs the real ``controls.async_set_fence_mode`` / ``controls.async_find_collar``,
the real ``RecallOwner.async_press`` / ``RecallOwner.async_register``, and the real
``HaloApiClient`` write path on a real asyncio loop. Only the network and storage edges
are fake: coordinator refresh, OAuth token refresh, Halo's configuration read, the Recall
state file, and the write transport each park on a gate. The explorer enumerates every
schedule of gate releases, gate outcomes, and up to ``budget`` environment events (option
changes, telemetry changes, task cancellation) and checks the safety properties at every
dispatch and every transaction exit. See .agents/skills/verify-control-transactions/SKILL.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import time as real_time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from custom_components.halo_collar import controls, recall
from custom_components.halo_collar.api import HaloApiClient, HaloApiError, HaloAuthError, HaloState
from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_ENABLE_RECALL_WHISTLE,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
    FIND_COLLAR_COOLDOWN_SECONDS,
    RECALL_COOLDOWN_SECONDS,
)
from custom_components.halo_collar.controls import HaloControlError
from custom_components.halo_collar.helpers import (
    has_active_walk,
    is_online,
    parse_timestamp,
    pet_fences_enabled,
    pet_for_collar,
    recall_command_support,
    subscription_feature_enabled,
)
from custom_components.halo_collar.recall import RecallOwner, RecallStorageError, recall_serial

COLLAR_ID = "collar-1"
RECALL_PET = "pet-1"
ACCOUNT = "account-binding"
MOBILE_ID = 7
SERVER_TIME = "2026-11-17T12:00:00Z"
RECALL_WINDOW = "00:01:00"
RECALL_BODY = {
    "mobileId": MOBILE_ID,
    "expirationDate": "2026-11-17T12:00:30Z",
    "correctionType": "returnwhistle",
}
REGISTERED = {
    "version": 1,
    "account": ACCOUNT,
    "registration": {"state": "registered", "mobile_id": MOBILE_ID, "next_by_serial": {}},
}
CURRENT_TXN: contextvars.ContextVar[int | None] = contextvars.ContextVar("txn", default=None)


class Violation(AssertionError):
    pass


@dataclass(frozen=True)
class World:
    """Abstract provider truth. Every field maps to one concrete payload field."""

    fences_on: bool
    pet_id: str = "pet-1"
    exact_mapping: bool = True  # pet.collarInfo.id points back at the collar
    fresh: bool = True  # collar telemetry.manifest.timestamp is 10s old
    walk: str = "none"  # "none" | "active" | "unknown"
    synced: bool = True
    entitled: bool = True
    firmware_ok: bool = True  # collar firmware.features lists numbered, expiring commands
    serial: str = "serial-1"  # collar serialNumber
    server_time_known: bool = True


def _build_state(world: World, sampled_at: float) -> HaloState:
    timestamp = (
        datetime.now(UTC) - timedelta(seconds=10)
        if world.fresh
        else datetime(2026, 1, 1, tzinfo=UTC)
    ).isoformat()
    walk_payload: dict[str, Any] = {}
    if world.walk == "active":
        walk_payload = {"walk": {"startTime": timestamp}}
    elif world.walk == "none":
        walk_payload = {"walk": None}
    collar_telemetry = {"manifest": {"timestamp": timestamp}, **walk_payload}
    pet = {
        "id": world.pet_id,
        "collarInfo": {
            "id": COLLAR_ID if world.exact_mapping else None,
            "telemetry": dict(walk_payload),
        },
        "isFencesSynchronized": world.synced,
        "telemetry": {"mode": {"fencesOn": world.fences_on}},
    }
    collar = {
        "id": COLLAR_ID,
        "serialNumber": world.serial,
        "petInfo": {"id": world.pet_id},
        "firmware": {
            "features": ["correctionmethodswithidandexpiration" if world.firmware_ok else "fota"]
        },
        "telemetry": collar_telemetry,
    }
    subscription = {
        "features": [{"featureType": {"id": "findcollar"}, "isEnabled": world.entitled}]
    }
    return HaloState(
        pets=[pet],
        collars=[collar],
        subscription=subscription,
        server_time=SERVER_TIME if world.server_time_known else None,
        server_time_monotonic=sampled_at if world.server_time_known else None,
    )


ENV_ACTIONS = {
    "revoke_fence_controls": lambda h: h.set_option(CONF_ENABLE_FENCE_CONTROLS, False),
    "revoke_disable": lambda h: h.set_option(CONF_ALLOW_FENCE_DISABLE, False),
    "revoke_find": lambda h: h.set_option(CONF_ENABLE_FIND_COLLAR, False),
    "revoke_recall": lambda h: h.set_option(CONF_ENABLE_RECALL_WHISTLE, False),
    "tighten_stale_after": lambda h: h.set_option(CONF_STALE_AFTER, 5),
    "telemetry_stale": lambda h: h.observe(fresh=False),
    "walk_active": lambda h: h.observe(walk="active"),
    "walk_unknown": lambda h: h.observe(walk="unknown"),
    "fences_unsynced": lambda h: h.observe(synced=False),
    "collar_rebound": lambda h: h.observe(pet_id="pet-2"),
    "mapping_one_sided": lambda h: h.observe(exact_mapping=False),
    "entitlement_lost": lambda h: h.observe(entitled=False),
    "fences_flipped_elsewhere": lambda h: h.observe(fences_on=not h.world.fences_on),
    "firmware_lost": lambda h: h.observe(firmware_ok=False),
    "serial_changed": lambda h: h.observe(serial="serial-2"),
    "server_time_lost": lambda h: h.observe(server_time_known=False),
    "cooldown_elapsed": lambda h: h.advance_clock(FIND_COLLAR_COOLDOWN_SECONDS),
    "runtime_replaced": lambda h: h.replace_runtime(),
    "coordinator_shut_down": lambda h: setattr(h, "coordinator_live", False),
}

WRITE_OUTCOMES = {
    "fence": ("ok", "timeout_after_apply", "connect_error", "http_500"),
    "find": ("ok", "http_404", "timeout", "http_500"),
    "recall": ("ok", "timeout_after_apply", "http_500", "expired"),
    "register": ("ok", "timeout", "http_500"),
}


@dataclass
class Gate:
    txn: int | None
    kind: str
    outcomes: tuple[str, ...]
    future: asyncio.Future


@dataclass
class Dispatch:
    txn: int
    kind: str  # "fence" | "find" | "recall" | "register"
    target: str
    enabled: bool | None
    step: int
    clock: float
    outcome: str | None = None
    number: int | None = None
    serial: str | None = None
    reply: str | None = None  # Recall: "accepted" | "stale", as Halo decided


class FakeCoordinator:
    def __init__(self, harness: Harness) -> None:
        self._h = harness
        self.last_update_success = True

    @property
    def data(self) -> HaloState:
        return self._h.state

    async def async_refresh(self) -> None:
        if not self._h.coordinator_live:
            # Home Assistant after unload: no fetch, old data and success flag kept.
            return
        if await self._h.gate("coordinator_refresh", ("ok", "fail")) == "ok":
            self._h.rebuild_snapshot()
            # The first refresh of a press is its preflight; validation follows with no await.
            txn = CURRENT_TXN.get()
            self._h.preflight_pet.setdefault(txn, self._h.snapshot_world.pet_id)
            self._h.preflight_serial.setdefault(txn, self._h.snapshot_world.serial)
            self._h.preflight_clock.setdefault(txn, self._h.clock)
            self.last_update_success = True
        else:
            self.last_update_success = False


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return ""

    async def json(self, content_type=None) -> Any:
        return self._body


class FakeWriteTransport:
    def __init__(self, harness: Harness) -> None:
        self._h = harness

    @asynccontextmanager
    async def request(self, method, url, *, headers, payload=None, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        dispatch = self._h.on_dispatch(url, payload)
        outcome = await self._h.gate(method.lower(), WRITE_OUTCOMES[dispatch.kind])
        dispatch.outcome = outcome
        applied = outcome in ("ok", "timeout_after_apply")
        if dispatch.kind == "fence" and applied:
            self._h.world = replace(self._h.world, fences_on=dispatch.enabled)
        body: Any = {"ok": True}
        if dispatch.kind == "recall":
            body = self._h.provider_receives(dispatch) if applied else {"result": "CommandExpired"}
        elif dispatch.kind == "register":
            body = {"mobileId": MOBILE_ID}
        if outcome in ("timeout", "timeout_after_apply"):
            raise TimeoutError
        if outcome == "connect_error":
            raise aiohttp.ClientConnectionError
        status = {"http_404": 404, "http_500": 500}.get(
            outcome, 204 if dispatch.kind == "find" else 200
        )
        yield FakeResponse(status, body)


class FakeStore:
    def __init__(self, harness: Harness) -> None:
        self._h = harness

    async def async_load(self) -> Any:
        return deepcopy(self._h.durable)

    async def async_save(self, data: dict[str, Any]) -> None:
        outcome = await self._h.gate("store", ("ok", "fail"))
        self._h.store_results.append((CURRENT_TXN.get(), outcome, self._h.step))
        if outcome == "fail":
            raise RecallStorageError("synthetic storage failure")
        self._h.durable = deepcopy(data)


@dataclass
class Scenario:
    name: str
    presses: tuple[str, ...]
    uncontested: tuple[str, ...]
    env: tuple[str, ...]
    budget: int
    initial_fences_on: bool = True
    recall_saved: dict[str, Any] | None = field(default_factory=lambda: deepcopy(REGISTERED))
    halo_next: int = 0
    options: dict[str, Any] = field(
        default_factory=lambda: {
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ALLOW_FENCE_DISABLE: True,
            CONF_ENABLE_FIND_COLLAR: True,
            CONF_ENABLE_RECALL_WHISTLE: True,
        }
    )


_UNCONTESTED = {"success": ("success", 1), "noop": ("success", 0), "refused": ("refused", 0)}
_UNCONTESTED["rejected"] = ("refused", 1)


class Harness:
    def __init__(self, scenario: Scenario, prefix: list[int]) -> None:
        self.scenario = scenario
        self.prefix = prefix
        self.trace: list[tuple[int, int]] = []
        self.log: list[str] = []
        self.clock = 1000.0
        self.world = World(fences_on=scenario.initial_fences_on)
        self.snapshot_world = self.world
        self.state = _build_state(self.world, self.clock)
        self.snapshot_step = 0
        self.entry = SimpleNamespace(options=dict(scenario.options))
        self.gates: list[Gate] = []
        self.dispatches: list[Dispatch] = []
        self.in_dispatch_phase: set[int] = set()
        self.preflight_pet: dict[int, str] = {}
        self.preflight_serial: dict[int, str] = {}
        self.preflight_clock: dict[int, float] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.results: dict[int, str] = {}
        self.cooldowns: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.coordinator = FakeCoordinator(self)
        self.durable = deepcopy(scenario.recall_saved)
        self.initial_next = self.durable_next(self.world.serial)
        self.halo_next = {self.world.serial: scenario.halo_next}
        self.store_results: list[tuple[int | None, str, int]] = []
        self.runtime_current = True
        self.coordinator_live = True
        self.step = 0
        self.violation: Violation | None = None

    # -- choice oracle ---------------------------------------------------------
    def choose(self, n: int) -> int:
        i = len(self.trace)
        choice = self.prefix[i] if i < len(self.prefix) else 0
        self.trace.append((choice, n))
        return choice

    def fail(self, message: str) -> None:
        # Recorded as well as raised: product code may swallow exceptions from the
        # inner dispatch task (cancel path), and an orphaned task's error is never awaited.
        if self.violation is None:
            steps = "\n".join(f"  {i + 1}. {line}" for i, line in enumerate(self.log))
            self.violation = Violation(f"[{self.scenario.name}] {message}\nSchedule:\n{steps}")
        raise self.violation

    # -- environment -----------------------------------------------------------
    def set_option(self, key: str, value: Any) -> None:
        self.entry.options = {**self.entry.options, key: value}

    def observe(self, **changes: Any) -> None:
        """Provider state changes and a background coordinator refresh lands it."""
        self.world = replace(self.world, **changes)
        if self.coordinator_live:
            self.rebuild_snapshot()

    def replace_runtime(self) -> None:
        self.runtime_current = False
        self.coordinator_live = False

    def advance_clock(self, seconds: float) -> None:
        self.clock += seconds

    def rebuild_snapshot(self) -> None:
        self.snapshot_world = self.world
        self.state = _build_state(self.world, self.clock)
        self.snapshot_step = self.step
        self._check_encoding()

    def _check_encoding(self) -> None:
        """Modeling check: helpers must read the payload the way the abstract world says."""
        w = self.snapshot_world
        collar = self.state.collars[0]
        pet = pet_for_collar(self.state.pets, collar, self.state.collars)
        assert pet is not None and pet["id"] == w.pet_id
        assert is_online(collar, stale_after=DEFAULT_STALE_AFTER_SECONDS) is w.fresh
        assert has_active_walk(pet, collar) is (w.walk != "none")
        assert pet_fences_enabled(pet) is w.fences_on
        assert subscription_feature_enabled(self.state.subscription, "findcollar") is w.entitled
        assert recall_command_support(collar) is w.firmware_ok
        assert recall_serial(self.state, w.pet_id) == (w.serial if w.exact_mapping else None)
        assert (parse_timestamp(self.state.server_time) is not None) is w.server_time_known

    async def gate(self, kind: str, outcomes: tuple[str, ...]) -> str:
        gate = Gate(CURRENT_TXN.get(), kind, outcomes, asyncio.get_running_loop().create_future())
        self.gates.append(gate)
        try:
            return await gate.future
        finally:
            self.gates.remove(gate)

    def durable_state(self) -> str:
        return self.durable["registration"]["state"] if self.durable else "unregistered"

    def durable_next(self, serial: str) -> int:
        if self.durable_state() != "registered":
            return 0
        return self.durable["registration"]["next_by_serial"].get(serial, 0)

    def provider_receives(self, dispatch: Dispatch) -> dict[str, Any]:
        """Halo accepts a number at or above its next expected one and rejects the rest."""
        expected = self.halo_next.get(dispatch.serial, 0)
        if dispatch.number >= expected:
            self.halo_next[dispatch.serial] = dispatch.number + 1
            dispatch.reply = "accepted"
            return {"result": "Success"}
        dispatch.reply = "stale"
        return {"result": "OldCommandNumber", "currentCommandNumber": expected - 1}

    # -- properties checked at the dispatch instant ----------------------------
    def fresh_now(self) -> bool:
        return self.snapshot_world.fresh and controls.control_stale_after(self.entry) >= 10

    def on_dispatch(self, url: str, payload: dict[str, Any] | None) -> Dispatch:
        txn = CURRENT_TXN.get()
        assert txn is not None
        if url.endswith("/find"):
            kind = "find"
        elif url.endswith("/run-instant-correction"):
            kind = "recall"
        elif url.endswith("/account/mobile-data"):
            kind = "register"
        else:
            kind = "fence"
        target = url.rsplit("/", 2)[-2]
        enabled = payload["modePatch"]["fencesOn"] if kind == "fence" else None
        dispatch = Dispatch(txn, kind, target, enabled, self.step, self.clock)
        what = {
            "fence": f"enabled={enabled}",
            "recall": f"number={(payload or {}).get('commandNumber')}",
        }
        self.log.append(f"txn{txn} DISPATCHES {kind} to {target} ({what.get(kind, kind)})")
        snap, opts = self.snapshot_world, self.entry.options

        # P3/P6: serialization and no orphaned dispatch.
        if self.tasks[txn].done():
            self.fail(f"txn{txn} dispatched after its caller had already returned")
        others = self.in_dispatch_phase - {txn}
        if others:
            self.fail(
                f"txn{txn} dispatched while txn{min(others)} was still dispatching/reconciling"
            )
        # P2: at most one dispatch per transaction.
        if any(d.txn == txn for d in self.dispatches):
            self.fail(f"txn{txn} dispatched a second time")
        if kind == "register":
            self.check_registration_dispatch()
            self.dispatches.append(dispatch)
            return dispatch
        # P1: authorized by a fetch this press made on the live runtime, and every gate
        # holds on the current snapshot and current options.
        if not self.runtime_current:
            self.fail("dispatched for a replaced runtime")
        if txn not in self.preflight_pet:
            self.fail("dispatched without a fresh preflight fetch")
        if snap.pet_id != self.preflight_pet.get(txn):
            self.fail("dispatched after the pet/collar mapping changed since preflight")
        if not self.fresh_now():
            self.fail("dispatched on stale telemetry")
        if kind == "fence":
            if not opts.get(CONF_ENABLE_FENCE_CONTROLS):
                self.fail("dispatched a fence write while fence controls were disabled")
            if target != snap.pet_id:
                self.fail(f"dispatched to {target} but the collar now maps to {snap.pet_id}")
            if enabled is False:
                if not opts.get(CONF_ALLOW_FENCE_DISABLE):
                    self.fail("disabled fences while the disable opt-in was off")
                if not snap.synced:
                    self.fail("disabled fences while fence sync was unconfirmed")
                if snap.walk != "none":
                    self.fail(f"disabled fences during a walk ({snap.walk})")
        elif kind == "find":
            if not opts.get(CONF_ENABLE_FIND_COLLAR):
                self.fail("dispatched Find collar while the option was off")
            if target != COLLAR_ID or not snap.exact_mapping:
                self.fail("dispatched Find collar without an exact two-way mapping")
            if snap.walk != "none":
                self.fail(f"dispatched Find collar during a walk ({snap.walk})")
            if not snap.entitled:
                self.fail("dispatched Find collar without entitlement")
            # P4: cooldown recorded before dispatch and respected between dispatches.
            if self.cooldowns.get(COLLAR_ID) != self.clock:
                self.fail("Find collar dispatched without first recording the cooldown")
            for prior in self.dispatches:
                if prior.kind == "find" and self.clock - prior.clock < FIND_COLLAR_COOLDOWN_SECONDS:
                    self.fail("Find collar dispatched twice inside the cooldown")
        else:
            self.check_recall_dispatch(txn, dispatch, target, payload or {})
        self.dispatches.append(dispatch)
        return dispatch

    def check_recall_dispatch(
        self, txn: int, dispatch: Dispatch, target: str, payload: dict[str, Any]
    ) -> None:
        snap = self.snapshot_world
        dispatch.serial = serial = self.preflight_serial[txn]
        dispatch.number = number = payload.get("commandNumber")
        if not self.entry.options.get(CONF_ENABLE_RECALL_WHISTLE):
            self.fail("dispatched the Recall whistle while the option was off")
        if target != RECALL_PET or snap.pet_id != RECALL_PET or not snap.exact_mapping:
            self.fail("dispatched the Recall whistle without an exact mapping for its pet")
        if snap.serial != serial:
            self.fail("dispatched the Recall whistle after the collar serial changed")
        if snap.walk != "none":
            self.fail(f"dispatched the Recall whistle during a walk ({snap.walk})")
        if not snap.firmware_ok:
            self.fail("dispatched the Recall whistle to firmware without numbered commands")
        if self.clock - self.preflight_clock[txn] > 30 - recall.RECALL_MIN_REMAINING_SECONDS:
            self.fail("dispatched a Recall command with too little lifetime left")
        if type(number) is not int or payload != {**RECALL_BODY, "commandNumber": number}:
            self.fail(f"Recall request body was {payload}")
        # P4: cooldown recorded before dispatch and respected between dispatches.
        if self.owner.cooldown_remaining(serial) != RECALL_COOLDOWN_SECONDS:
            self.fail("Recall whistle dispatched without first recording the cooldown")
        prior = [d for d in self.dispatches if d.kind == "recall" and d.serial == serial]
        if any(self.clock - d.clock < RECALL_COOLDOWN_SECONDS for d in prior):
            self.fail("Recall whistle dispatched twice inside the cooldown")
        # P9/P10: numbers only increase and are spent durably before the request.
        if number < self.initial_next or any(d.number >= number for d in prior):
            self.fail(f"Recall command number {number} did not increase")
        if self.durable_next(serial) <= number:
            self.fail(f"Recall command number {number} was not spent before dispatch")

    def check_registration_dispatch(self) -> None:
        if any(d.kind == "register" for d in self.dispatches):
            self.fail("sent a second registration request")
        if self.durable_state() != "pending":
            self.fail("sent a registration request without a saved intent")
        if not self.runtime_current:
            self.fail("sent a registration request for a replaced runtime")

    # -- properties checked when a press returns -------------------------------
    def on_success(self, txn: int, action: str) -> None:
        mine = [d for d in self.dispatches if d.txn == txn]
        if action == "register":
            registration = (self.durable or {}).get("registration", {})
            if registration.get("state") != "registered" or registration.get("mobile_id") != 7:
                self.fail(f"txn{txn} reported registration without saving Halo's mobile ID")
            if any(d.outcome != "ok" for d in mine):
                self.fail(f"txn{txn} reported registration after outcome {mine[0].outcome}")
            return
        if len(mine) != 1:
            self.fail(f"txn{txn} reported success with {len(mine)} dispatches")
        if action == "find":
            if mine[0].outcome != "ok":
                self.fail(f"Find collar reported success after outcome {mine[0].outcome}")
            return
        if action == "recall":
            if mine[0].outcome != "ok" or mine[0].reply != "accepted":
                self.fail(f"Recall reported success after {mine[0].outcome}/{mine[0].reply}")
            return
        snap = self.snapshot_world
        confirmed = (
            self.coordinator.last_update_success
            and self.snapshot_step > mine[0].step
            and snap.fences_on is (action == "enable")
            and snap.synced
            and self.fresh_now()
            and snap.pet_id == mine[0].target
        )
        if not confirmed:
            self.fail(f"txn{txn} reported {action} success without reported-state confirmation")

    def on_finish(self, txn: int) -> None:
        """P11: a press that read OldCommandNumber leaves the saved number at Halo's."""
        for dispatch in self.dispatches:
            if dispatch.txn != txn or dispatch.outcome != "ok" or dispatch.reply != "stale":
                continue
            failed_save = any(
                t == txn and outcome == "fail" and step >= dispatch.step
                for t, outcome, step in self.store_results
            )
            if (
                not failed_save
                and self.durable_next(dispatch.serial) < self.halo_next[dispatch.serial]
            ):
                self.fail(f"txn{txn} left the saved command number behind Halo's")

    # -- driver ----------------------------------------------------------------
    async def press(self, txn: int, action: str) -> None:
        CURRENT_TXN.set(txn)

        def state_getter():
            collar = next((c for c in self.state.collars if c.get("id") == COLLAR_ID), None)
            pet = pet_for_collar(self.state.pets, collar, self.state.collars) if collar else None
            return pet, collar

        try:
            if action == "find":
                await controls.async_find_collar(
                    coordinator=self.coordinator,
                    client=self.client,
                    entry=self.entry,
                    control_lock=self.lock,
                    is_current=lambda: self.runtime_current,
                    cooldowns=self.cooldowns,
                    state_getter=lambda: (*state_getter(), self.state.subscription),
                )
            elif action == "recall":
                await self.owner.async_press(
                    RECALL_PET,
                    coordinator=self.coordinator,
                    client=self.client,
                    entry=self.entry,
                    is_current=lambda: self.runtime_current,
                )
            elif action == "register":
                await self.owner.async_register(
                    client=self.client,
                    version_string="explorer",
                    is_current=lambda: self.runtime_current,
                )
            else:
                await controls.async_set_fence_mode(
                    coordinator=self.coordinator,
                    client=self.client,
                    entry=self.entry,
                    control_lock=self.lock,
                    is_current=lambda: self.runtime_current,
                    state_getter=state_getter,
                    enabled=action == "enable",
                )
        except HaloControlError as err:
            self.results[txn] = f"refused: {err}"
        except HaloApiError as err:
            self.results[txn] = f"error: {err}"
        except asyncio.CancelledError:
            self.results[txn] = "cancelled"
        except Violation:
            raise
        except BaseException as err:  # noqa: BLE001 - surfaced as a violation
            self.fail(f"txn{txn} leaked {type(err).__name__}: {err}")
        else:
            self.on_success(txn, action)
            self.results[txn] = "success"
        self.on_finish(txn)
        self.log.append(f"txn{txn} {action} returns: {self.results[txn]}")

    async def settle(self) -> None:
        for _ in range(200):
            before = (len(self.gates), sum(t.done() for t in self.tasks.values()))
            for _ in range(5):
                await asyncio.sleep(0)
            if (len(self.gates), sum(t.done() for t in self.tasks.values())) == before:
                return
        raise AssertionError("explorer could not reach a quiescent point")

    async def run(self) -> None:
        self.client = HaloApiClient(
            session=None,
            access_token="token",
            refresh_token="refresh",
            expires_at=0.0 if self.choose(2) else real_time.time() + 3600,
            client_id="client",
            client_secret="secret",
            api_base="https://api.example",
            auth_base="https://auth.example",
            write_transport=FakeWriteTransport(self),
        )

        async def token_request(_data):
            if await self.gate("token_refresh", ("ok", "fail")) == "fail":
                raise HaloAuthError("refresh failed")
            self.client._access_token = "token2"
            self.client._expires_at = real_time.time() + 3600

        async def configuration_get(path):
            assert path == "/configuration"
            if await self.gate("configuration", ("ok", "fail")) == "fail":
                raise HaloApiError("configuration unavailable")
            return {"correction": {"runPositiveCorrectionExpiration": RECALL_WINDOW}}

        self.client._async_token_request = token_request
        self.client.async_get = configuration_get
        if self.client._expires_at == 0.0:
            self.log.append("access token starts expired")
        self.owner = RecallOwner(store=FakeStore(self), account=ACCOUNT, lock=self.lock)
        await self.owner.async_load()
        self.advance_clock(RECALL_COOLDOWN_SECONDS)

        for txn, action in enumerate(self.scenario.presses, start=1):
            self.log.append(f"txn{txn} presses {action}")
            self.tasks[txn] = asyncio.create_task(self.press(txn, action))
        budget = self.scenario.budget
        while True:
            await self.settle()
            if self.violation is not None:
                raise self.violation
            for task in self.tasks.values():
                if task.done() and task.exception() is not None:
                    raise task.exception()
            if not self.gates:
                break
            gates = list(self.gates)
            env = [*self.scenario.env]
            env += [f"cancel_txn{t}" for t, task in self.tasks.items() if not task.done()]
            # Options: release a gate; apply an environment event; or release a gate and
            # cancel its press in the same loop tick (cancel first, or release first).
            n_env = len(env) if budget else 0
            n_same_tick = 2 * len(gates) if budget else 0
            choice = self.choose(len(gates) + n_env + n_same_tick)
            self.step += 1
            if len(gates) <= choice < len(gates) + n_env:
                action = env[choice - len(gates)]
                budget -= 1
                self.log.append(f"environment: {action}")
                if action.startswith("cancel_txn"):
                    self.tasks[int(action.removeprefix("cancel_txn"))].cancel()
                else:
                    ENV_ACTIONS[action](self)
                continue
            same_tick = None
            if choice >= len(gates) + n_env:
                same_tick, cancel_first = divmod(choice - len(gates) - n_env, 2)
                choice = same_tick
                budget -= 1
            gate = gates[choice]
            if same_tick is not None and cancel_first:
                self.log.append(f"environment: cancel_txn{gate.txn} (same tick, before)")
                self.tasks[gate.txn].cancel()
            if gate.future.done():
                # The press was awaiting this gate directly, so cancel won the race,
                # as it would against a real I/O completion callback.
                continue
            outcome = gate.outcomes[self.choose(len(gate.outcomes))]
            self.log.append(f"txn{gate.txn} {gate.kind} completes: {outcome}")
            gate.future.set_result(outcome)
            if same_tick is not None and not cancel_first:
                self.log.append(f"environment: cancel_txn{gate.txn} (same tick, after)")
                self.tasks[gate.txn].cancel()

        if not all(t.done() for t in self.tasks.values()):
            self.fail("a press never finished (deadlock)")
        if self.lock.locked():
            self.fail("control lock still held after every press finished")
        if self.in_dispatch_phase:
            self.fail("a dispatch phase was still running after every press finished")
        if any(d.kind == "register" for d in self.dispatches) and (
            self.durable_state() == "unregistered"
        ):
            self.fail("forgot a registration request that may have reached Halo")
        if all(choice == 0 for choice, _ in self.trace):
            self.check_uncontested()

    def check_uncontested(self) -> None:
        """Valid presses must work when nothing contends with them."""
        for txn, expected in enumerate(self.scenario.uncontested, start=1):
            result = self.results[txn].partition(":")[0]
            dispatched = sum(d.txn == txn for d in self.dispatches)
            if (result, dispatched) != _UNCONTESTED[expected]:
                self.fail(
                    f"uncontested txn{txn} expected {expected}, got {self.results[txn]}"
                    f" after {dispatched} dispatches"
                )


_ACTIVE: list[Harness] = []


def _wrap_phase(original):
    harness_ref = _ACTIVE

    async def wrapped(**kwargs):
        harness = harness_ref[0]
        txn = CURRENT_TXN.get()
        harness.in_dispatch_phase.add(txn)
        try:
            await original(**kwargs)
        except asyncio.CancelledError:
            # P7: once dispatched, reconciliation must run to completion under the lock.
            if any(d.txn == txn for d in harness.dispatches):
                harness.fail(f"txn{txn} abandoned post-dispatch reconciliation on cancellation")
            raise
        finally:
            harness.in_dispatch_phase.discard(txn)

    return wrapped


_PHASES = (
    (controls, "_async_dispatch_and_reconcile"),
    (controls, "_async_dispatch_find_and_refresh"),
    (recall, "_async_dispatch_and_settle"),
    (recall, "_async_register_and_record"),
)


def explore(scenario: Scenario, monkeypatch) -> int:
    for module, name in _PHASES:
        monkeypatch.setattr(module, name, _wrap_phase(getattr(module, name)))
    loop = asyncio.new_event_loop()
    prefix: list[int] = []
    runs = 0
    try:
        while True:
            harness = Harness(scenario, prefix)
            _ACTIVE[:] = [harness]
            clock = SimpleNamespace(monotonic=lambda h=harness: h.clock)
            monkeypatch.setattr(controls, "time", clock)
            monkeypatch.setattr(recall, "time", clock)
            loop.run_until_complete(harness.run())
            runs += 1
            trace = harness.trace
            while trace and trace[-1][0] + 1 >= trace[-1][1]:
                trace.pop()
            if not trace:
                return runs
            prefix = [c for c, _ in trace[:-1]] + [trace[-1][0] + 1]
    finally:
        loop.close()


RUNTIME_ENV = ("runtime_replaced", "coordinator_shut_down")
FENCE_ENV = (
    "revoke_fence_controls",
    "tighten_stale_after",
    "telemetry_stale",
    "collar_rebound",
    "fences_flipped_elsewhere",
    *RUNTIME_ENV,
)
RECALL_ENV = (
    "revoke_recall",
    "tighten_stale_after",
    "telemetry_stale",
    "walk_active",
    "walk_unknown",
    "collar_rebound",
    "mapping_one_sided",
    "firmware_lost",
    "serial_changed",
    "server_time_lost",
    "cooldown_elapsed",
    *RUNTIME_ENV,
)
SCENARIOS = [
    Scenario("enable", ("enable",), ("success",), FENCE_ENV, budget=2, initial_fences_on=False),
    Scenario(
        "disable",
        ("disable",),
        ("success",),
        FENCE_ENV + ("revoke_disable", "walk_active", "walk_unknown", "fences_unsynced"),
        budget=2,
    ),
    Scenario(
        "find",
        ("find",),
        ("success",),
        (
            "revoke_find",
            "tighten_stale_after",
            "telemetry_stale",
            "walk_active",
            "walk_unknown",
            "collar_rebound",
            "mapping_one_sided",
            "entitlement_lost",
            *RUNTIME_ENV,
        ),
        budget=2,
    ),
    Scenario(
        "find+find", ("find", "find"), ("success", "refused"), ("cooldown_elapsed",), budget=2
    ),
    Scenario(
        "disable+find", ("disable", "find"), ("success", "success"), ("walk_active",), budget=1
    ),
    Scenario("enable+find", ("enable", "find"), ("success", "success"), RUNTIME_ENV, budget=1),
    Scenario(
        "disable+enable",
        ("disable", "enable"),
        ("success", "success"),
        ("fences_flipped_elsewhere",),
        budget=1,
    ),
    Scenario("recall", ("recall",), ("success",), RECALL_ENV, budget=2),
    Scenario(
        "recall+recall",
        ("recall", "recall"),
        ("success", "refused"),
        ("cooldown_elapsed",),
        budget=2,
    ),
    Scenario(
        "recall-desync",
        ("recall", "recall"),
        ("rejected", "refused"),
        ("cooldown_elapsed",),
        budget=2,
        halo_next=5,
    ),
    Scenario("find+recall", ("find", "recall"), ("success", "success"), ("walk_active",), budget=1),
    Scenario(
        "disable+recall", ("disable", "recall"), ("success", "success"), ("walk_active",), budget=1
    ),
    Scenario(
        "register", ("register",), ("success",), ("runtime_replaced",), budget=2, recall_saved=None
    ),
    Scenario(
        "register+register",
        ("register", "register"),
        ("success", "noop"),
        (),
        budget=2,
        recall_saved=None,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_control_transactions_hold_under_every_interleaving(scenario, monkeypatch):
    runs = explore(scenario, monkeypatch)
    assert runs > 1
