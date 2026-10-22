"""Exhaustive interleaving check of the guarded write transactions.

This runs the real ``controls.async_set_fence_mode`` / ``controls.async_find_collar``
and the real ``HaloApiClient`` write path on a real asyncio loop. Only the network
edges are fake: coordinator refresh, OAuth token refresh, and the write transport's
PUT each park on a gate. The explorer enumerates every schedule of gate releases,
gate outcomes, and up to ``budget`` environment events (option changes, telemetry
changes, task cancellation) and checks the safety properties at every dispatch and
every transaction exit. See .agents/skills/verify-control-transactions/SKILL.md.
"""

from __future__ import annotations

import asyncio
import contextvars
import time as real_time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from custom_components.halo_collar import controls
from custom_components.halo_collar.api import HaloApiClient, HaloApiError, HaloAuthError, HaloState
from custom_components.halo_collar.const import (
    CONF_ALLOW_FENCE_DISABLE,
    CONF_ENABLE_FENCE_CONTROLS,
    CONF_ENABLE_FIND_COLLAR,
    CONF_STALE_AFTER,
    DEFAULT_STALE_AFTER_SECONDS,
    FIND_COLLAR_COOLDOWN_SECONDS,
)
from custom_components.halo_collar.controls import HaloControlError
from custom_components.halo_collar.helpers import (
    has_active_walk,
    is_online,
    pet_fences_enabled,
    pet_for_collar,
    subscription_feature_enabled,
)

COLLAR_ID = "collar-1"
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


def _build_state(world: World) -> HaloState:
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
    collar = {"id": COLLAR_ID, "petInfo": {"id": world.pet_id}, "telemetry": collar_telemetry}
    subscription = {
        "features": [{"featureType": {"id": "findcollar"}, "isEnabled": world.entitled}]
    }
    return HaloState(pets=[pet], collars=[collar], subscription=subscription)


ENV_ACTIONS = {
    "revoke_fence_controls": lambda h: h.set_option(CONF_ENABLE_FENCE_CONTROLS, False),
    "revoke_disable": lambda h: h.set_option(CONF_ALLOW_FENCE_DISABLE, False),
    "revoke_find": lambda h: h.set_option(CONF_ENABLE_FIND_COLLAR, False),
    "tighten_stale_after": lambda h: h.set_option(CONF_STALE_AFTER, 5),
    "telemetry_stale": lambda h: h.observe(fresh=False),
    "walk_active": lambda h: h.observe(walk="active"),
    "walk_unknown": lambda h: h.observe(walk="unknown"),
    "fences_unsynced": lambda h: h.observe(synced=False),
    "collar_rebound": lambda h: h.observe(pet_id="pet-2"),
    "mapping_one_sided": lambda h: h.observe(exact_mapping=False),
    "entitlement_lost": lambda h: h.observe(entitled=False),
    "fences_flipped_elsewhere": lambda h: h.observe(fences_on=not h.world.fences_on),
    "cooldown_elapsed": lambda h: h.advance_clock(FIND_COLLAR_COOLDOWN_SECONDS),
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
    kind: str  # "fence" | "find"
    target: str
    enabled: bool | None
    step: int
    clock: float
    outcome: str | None = None


class FakeCoordinator:
    def __init__(self, harness: Harness) -> None:
        self._h = harness
        self.last_update_success = True

    @property
    def data(self) -> HaloState:
        return self._h.state

    async def async_refresh(self) -> None:
        if await self._h.gate("coordinator_refresh", ("ok", "fail")) == "ok":
            self._h.rebuild_snapshot()
            # The first refresh of a press is its preflight; validation follows with no await.
            self._h.preflight_pet.setdefault(CURRENT_TXN.get(), self._h.snapshot_world.pet_id)
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
    """Mirrors HaloWriteTransport.put: pre_dispatch runs with no await before the PUT."""

    def __init__(self, harness: Harness) -> None:
        self._h = harness

    @asynccontextmanager
    async def put(self, url, *, headers, payload=None, pre_dispatch=None):
        if pre_dispatch is not None:
            pre_dispatch()
        dispatch = self._h.on_dispatch(url, payload)
        if dispatch.kind == "fence":
            outcomes = ("ok", "timeout_after_apply", "connect_error", "http_500")
        else:
            outcomes = ("ok", "http_404", "timeout", "http_500")
        outcome = await self._h.gate("put", outcomes)
        dispatch.outcome = outcome
        if dispatch.kind == "fence" and outcome in ("ok", "timeout_after_apply"):
            self._h.world = replace(self._h.world, fences_on=dispatch.enabled)
        if outcome in ("timeout", "timeout_after_apply"):
            raise TimeoutError
        if outcome == "connect_error":
            raise aiohttp.ClientConnectionError
        status = {"ok": 200 if dispatch.kind == "fence" else 204, "http_404": 404, "http_500": 500}
        yield FakeResponse(status[outcome], {"ok": True})


@dataclass
class Scenario:
    name: str
    presses: tuple[str, ...]  # each "enable" | "disable" | "find"
    # Per press, "success" or "refused" on the schedule where every choice is index 0.
    uncontested: tuple[str, ...]
    env: tuple[str, ...]
    budget: int
    initial_fences_on: bool = True
    options: dict[str, Any] = field(
        default_factory=lambda: {
            CONF_ENABLE_FENCE_CONTROLS: True,
            CONF_ALLOW_FENCE_DISABLE: True,
            CONF_ENABLE_FIND_COLLAR: True,
        }
    )


class Harness:
    def __init__(self, scenario: Scenario, prefix: list[int]) -> None:
        self.scenario = scenario
        self.prefix = prefix
        self.trace: list[tuple[int, int]] = []
        self.log: list[str] = []
        self.world = World(fences_on=scenario.initial_fences_on)
        self.snapshot_world = self.world
        self.state = _build_state(self.world)
        self.snapshot_step = 0
        self.entry = SimpleNamespace(options=dict(scenario.options))
        self.clock = 1000.0
        self.gates: list[Gate] = []
        self.dispatches: list[Dispatch] = []
        self.in_dispatch_phase: set[int] = set()
        self.preflight_pet: dict[int, str] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.results: dict[int, str] = {}
        self.cooldowns: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.coordinator = FakeCoordinator(self)
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
        self.rebuild_snapshot()

    def advance_clock(self, seconds: float) -> None:
        self.clock += seconds

    def rebuild_snapshot(self) -> None:
        self.snapshot_world = self.world
        self.state = _build_state(self.world)
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

    async def gate(self, kind: str, outcomes: tuple[str, ...]) -> str:
        gate = Gate(CURRENT_TXN.get(), kind, outcomes, asyncio.get_running_loop().create_future())
        self.gates.append(gate)
        try:
            return await gate.future
        finally:
            self.gates.remove(gate)

    # -- properties checked at the dispatch instant ----------------------------
    def fresh_now(self) -> bool:
        return self.snapshot_world.fresh and controls.control_stale_after(self.entry) >= 10

    def on_dispatch(self, url: str, payload: dict[str, Any] | None) -> Dispatch:
        txn = CURRENT_TXN.get()
        assert txn is not None
        kind = "find" if url.endswith("/find") else "fence"
        target = url.rsplit("/", 2)[-2]
        enabled = payload["modePatch"]["fencesOn"] if payload else None
        dispatch = Dispatch(txn, kind, target, enabled, self.step, self.clock)
        what = f"enabled={enabled}" if kind == "fence" else "find"
        self.log.append(f"txn{txn} DISPATCHES {kind} PUT to {target} ({what})")
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
        # P1: every gate holds on the current snapshot and current options.
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
        else:
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
        self.dispatches.append(dispatch)
        return dispatch

    # -- properties checked when a press returns -------------------------------
    def on_success(self, txn: int, action: str) -> None:
        mine = [d for d in self.dispatches if d.txn == txn]
        if len(mine) != 1:
            self.fail(f"txn{txn} reported success with {len(mine)} dispatches")
        if action == "find":
            if mine[0].outcome != "ok":
                self.fail(f"Find collar reported success after outcome {mine[0].outcome}")
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
                    cooldowns=self.cooldowns,
                    state_getter=lambda: (*state_getter(), self.state.subscription),
                )
            else:
                await controls.async_set_fence_mode(
                    coordinator=self.coordinator,
                    client=self.client,
                    entry=self.entry,
                    control_lock=self.lock,
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

        self.client._async_token_request = token_request
        if self.client._expires_at == 0.0:
            self.log.append("access token starts expired")

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
        if all(choice == 0 for choice, _ in self.trace):
            self.check_uncontested()

    def check_uncontested(self) -> None:
        """Valid presses must work when nothing contends with them."""
        for txn, expected in enumerate(self.scenario.uncontested, start=1):
            result = self.results[txn].partition(":")[0]
            dispatched = sum(d.txn == txn for d in self.dispatches)
            if (result, dispatched) != (expected, int(expected == "success")):
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


def explore(scenario: Scenario, monkeypatch) -> int:
    monkeypatch.setattr(
        controls,
        "_async_dispatch_and_reconcile",
        _wrap_phase(controls._async_dispatch_and_reconcile),
    )
    monkeypatch.setattr(
        controls,
        "_async_dispatch_find_and_refresh",
        _wrap_phase(controls._async_dispatch_find_and_refresh),
    )
    loop = asyncio.new_event_loop()
    prefix: list[int] = []
    runs = 0
    try:
        while True:
            harness = Harness(scenario, prefix)
            _ACTIVE[:] = [harness]
            monkeypatch.setattr(
                controls, "time", SimpleNamespace(monotonic=lambda h=harness: h.clock)
            )
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


FENCE_ENV = (
    "revoke_fence_controls",
    "tighten_stale_after",
    "telemetry_stale",
    "collar_rebound",
    "fences_flipped_elsewhere",
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
        ),
        budget=2,
    ),
    Scenario(
        "find+find", ("find", "find"), ("success", "refused"), ("cooldown_elapsed",), budget=2
    ),
    Scenario(
        "disable+find", ("disable", "find"), ("success", "success"), ("walk_active",), budget=1
    ),
    Scenario(
        "disable+enable",
        ("disable", "enable"),
        ("success", "success"),
        ("fences_flipped_elsewhere",),
        budget=1,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_control_transactions_hold_under_every_interleaving(scenario, monkeypatch):
    runs = explore(scenario, monkeypatch)
    assert runs > 1
