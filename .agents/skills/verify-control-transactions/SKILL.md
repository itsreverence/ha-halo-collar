---
name: verify-control-transactions
description: Exhaustively check the guarded write transactions (fence mode, Find collar, and the Recall whistle with its one-time registration) against every interleaving of refreshes, option changes, telemetry changes, storage outcomes, provider outcomes, and cancellation. Use when a change touches custom_components/halo_collar/controls.py, recall.py, the write path in api.py or transport.py, how switch.py/button.py call into controls or recall, or the helpers those gates use.
---

# Verify the guarded control transactions

## What this checks

**Property.** Every fence-mode or Find collar PUT and every Recall POST is sent at most once per press. It is sent only while every opt-in, freshness, mapping, walk, sync, entitlement, firmware, lifetime, and cooldown gate holds on the current snapshot. It never overlaps another press's dispatch or reconciliation, even when the caller is cancelled. A press reports success only after reported-state confirmation (fence) or an unambiguous provider success (Find collar, Recall). A Recall request carries exactly the whistle body, a command number that was saved as spent before the request and is higher than every earlier one, and an expiry no later than 30 seconds after Halo's clock. The one-time registration POST is sent at most once ever, only after its intent is saved, and an ambiguous outcome is never forgotten. A valid press with nothing contending with it succeeds.

**Harm if broken.** Home Assistant could disable a dog's containment during a walk, after the user revoked the opt-in, or for a dog the collar no longer belongs to. It could replay a physical command, send a correction other than the Recall whistle, reuse a command number, or report success that the collar never confirmed. It could create a second permanent Halo device registration. A transaction that refuses every press would pass every safety property and leave the opted-in controls unusable.

## How it works

`tests/test_control_interleavings.py` runs the **real** `controls.async_set_fence_mode`, `controls.async_find_collar`, `RecallOwner.async_press`, `RecallOwner.async_register`, and the real `HaloApiClient` write methods on a real asyncio loop. Only the network and storage edges are fake, and each one parks on a gate:

| Gate | Fake for | Outcomes explored |
|---|---|---|
| `coordinator_refresh` | `DataUpdateCoordinator.async_refresh` | ok (snapshot := provider truth, server clock sampled now), fail (`last_update_success=False`). Once the coordinator is shut down, the refresh returns at once without a gate and keeps the old snapshot and success flag, as Home Assistant's does after unload |
| `token_refresh` | `HaloApiClient._async_token_request` (token starts expired or valid) | ok, fail (`HaloAuthError`) |
| `configuration` | `HaloApiClient.async_get("/configuration")` (the real window parser runs) | ok (one-minute window), fail (`HaloApiError`) |
| `store` | the Recall state file's `async_save` | ok (durable copy replaced), fail (`RecallStorageError`) |
| `put` / `post` | `HaloWriteTransport.request`, which calls `pre_dispatch()` with no await before the send, like the real one | fence: ok, timeout_after_apply, connect_error, http_500. find: ok, http_404, timeout, http_500. recall: ok, timeout_after_apply, http_500, expired. register: ok, timeout, http_500 |

A Recall request that reaches Halo (`ok`, `timeout_after_apply`) meets a provider counter model: Halo accepts a number at or above its next expected one and otherwise answers `OldCommandNumber` with its last accepted number.

A stateless DFS enumerates every choice: which parked gate to release, each gate's outcome, and up to `budget` environment events. The events are option revocations, `stale_after` tightening, stale telemetry, active or unknown walk, fences unsynced, collar rebound to another pet, one-sided mapping, entitlement lost, fences flipped in the official app, firmware without numbered commands, a different collar serial, server time missing, cooldown elapsed (60 s, which also expires a Recall command in flight), the runtime replaced by a reload (which also shuts its coordinator down), the coordinator shut down while its runtime stays registered (a failed unload), and cancelling a press. Cancellation can be a separate step, or land in the same loop tick as a gate completing (in either order). Environment changes reach the snapshot through a modeled background refresh, which stops once the coordinator is shut down. Only `Harness.rebuild_snapshot` calls `_check_encoding`. It runs on each `ok` coordinator refresh and each telemetry event, but not on option changes, cancellation, or `cooldown_elapsed`. `_check_encoding` asserts that the real helpers (`is_online`, `has_active_walk`, `pet_for_collar`, `pet_fences_enabled`, `subscription_feature_enabled`, `recall_command_support`, `recall_serial`, `parse_timestamp`) read the payload the way the abstract `World` says.

Each run builds a fresh `RecallOwner` over the fake store, already registered unless the scenario starts unregistered, and lets the restart hold lapse before the presses start.

Properties, checked in `Harness.on_dispatch`, `Harness.on_success`, `Harness.on_finish`, `_wrap_phase`, `Harness.check_uncontested`, and at the end of `Harness.run`:

- **P1 gates at dispatch.** At the instant the request starts, the press's runtime is still the entry's live runtime and its own preflight refresh fetched a new snapshot. Every gate holds on the current snapshot and current `entry.options`. The pet/collar mapping also matches this press's preflight refresh. For Recall the collar serial matches preflight, firmware lists numbered commands, and at most 28 seconds passed since the preflight clock sample.
- **Body.** A Recall request body is exactly `{mobileId, commandNumber, expirationDate, correctionType}` with the registered mobile ID, `returnwhistle`, and Halo's time plus 30 seconds.
- **P2 at most once.** A press dispatches at most one request, so an ambiguous result is never replayed. At most one registration request is ever sent.
- **P3 serialization.** No request while another press's dispatch/reconcile phase is still running, across every tier.
- **P4 cooldown.** Find collar and Recall each record their own cooldown before dispatch, and no two dispatches of the same tier (Recall: same serial) fall inside 60 seconds.
- **P5 honest success.** A fence press returns normally only if a successful refresh after its dispatch shows the requested `fencesOn`, synced, fresh, same pet. A Find press returns normally only after a 2xx. A Recall press returns normally only after Halo accepted its number. A registration returns normally only with Halo's mobile ID saved.
- **P6 no orphans.** No request after the press's caller has already returned.
- **P7 reconcile survives cancellation.** Once dispatched, the reconcile phase never ends in `CancelledError`.
- **P9 increasing numbers.** A Recall number is never lower than the saved starting number or any earlier number sent to the same serial.
- **P10 spent before sending.** The saved next number already exceeds the number being sent.
- **P11 resync.** After a press reads `OldCommandNumber` and its save succeeds, the saved next number is not behind Halo's.
- **Registration.** The request is sent only while the saved state is the intent marker and the registering runtime is current. If a registration request was sent, the saved state never returns to unregistered.
- **Liveness.** Every press finishes, the lock is released, and no dispatch phase is left running.
- **P8 uncontested success.** On the schedule where every choice is index 0 (token valid, each gate released in order with its first outcome `ok`, no environment event, no cancellation), each press ends with its scenario's `uncontested` result. `success` means it returned normally after exactly one dispatch. `noop` means it returned normally with none, as for a second registration. `refused` means `HaloControlError` with no dispatch, as for a press inside the cooldown. `rejected` means `HaloControlError` after one dispatch, as for a desynchronized command number.

Scenarios (`SCENARIOS`): `enable`, `disable`, `find`, `recall` (one press, budget 2), `find+find`, `recall+recall`, `recall-desync` (Halo five numbers ahead), `register`, `register+register` (budget 2), `disable+find`, `enable+find` (the second press waits on the lock while the entry unloads), `disable+enable`, `find+recall`, `disable+recall` (budget 1).

## Scope and limits

The check covers everything above within these bounds: one pet, one collar, and one Halo serial at a time, at most two concurrent presses, at most one or two environment events per schedule (each scenario's `budget`), and the gate and outcome sets listed. It does **not** cover:

- Real aiohttp behaviour in `HaloWriteTransport` (single lease, `allow_redirects=False`, no replay). `tests/test_api_transport.py` covers that for PUT and POST.
- The real `DataUpdateCoordinator`, the real configuration GET retry loop, or the real state file. The harness assumes a refresh either replaces the snapshot, fails, or (after shutdown) does nothing, and a save either replaces the durable copy or fails.
- Entity `available` properties, button and switch error wrapping, the options flow, and the status dispatcher signals. Which dogs get Recall entities and which device each sits on (`recall_dogs`, `HaloDogEntity`, the reload on a wearer change); `tests/test_recall_integration.py` covers them.
- Unsafe pet or collar IDs. The explorer's IDs always pass `_path_id`; `tests/test_api_transport.py` covers the refusal on every write route.
- Two config entries for the same account. Those have separate locks, cooldowns, and Recall state, and nothing serializes them.
- Ordering among tasks that are runnable in the same tick, beyond the gate/cancel combinations above. The driver settles the loop between steps.

## Run it

```bash
uv run pytest tests/test_control_interleavings.py -q        # ~35 to 60 s, ~84k schedules
uv run python scripts/check_control_mutants.py controls     # ~2 min, fence and Find mutants
uv run python scripts/check_control_mutants.py recall       # ~1 to 2 min, Recall mutants
uv run python scripts/check_control_mutants.py runtime      # ~1 to 2 min, live-runtime and fresh-fetch mutants
```

The first command also runs inside `uv run pytest -q`. The mutant script copies `custom_components/`, `tests/` and `pyproject.toml` into a temporary directory and never writes to the checkout. For each tier it first runs the unmutated explorer there on that tier's scenarios and stops with `BASELINE <tier> failed` unless it passes. Then it applies each broken variant of `controls.py`, `recall.py`, or `api.py` to the copy in turn, runs the scenarios that mutant names, and expects a `Violation`. Every mutant line should read `CAUGHT`. `STALE`, `MISSED` and `TIMEOUT` (no verdict within 90 s) all make it exit non-zero. CI runs each tier as its own job capped at 30 minutes. The `controls` and `recall` tiers hold 18 mutants each and `runtime` holds fewer, so a tier fits its cap even if every run times out. Add a mutant past 18 to a new tier and job rather than raising the cap. CI runs the script with `python -u` so a cancelled job still shows each verdict printed so far.

## Reading a failure

A `Violation` prints the broken property and the schedule as numbered steps in the code's own terms. This one came from a scratch change that made `_validate_find_snapshot` test `active_walk_state(...) is True` instead of `has_active_walk(...)`:

```
[find] dispatched Find collar during a walk (unknown)
Schedule:
  1. txn1 presses find
  2. environment: walk_active
  3. environment: walk_unknown
  4. txn1 coordinator_refresh completes: ok
  5. txn1 DISPATCHES find to collar-1 (find)
```

Replay the steps against the product code to decide between two causes:

- **Real bug.** The code really does allow that order. Report it with the schedule. Do not weaken the property or remove the environment event to make it pass, and change product code only if the user asks.
- **Modeling error.** The harness does something the code cannot do. Examples: a fake that awaits where the real code does not, or an oracle stricter than the documented rule (such as treating a rebind *before* preflight as a mapping change). Fix the harness and say so.

An `AssertionError` from `settle`, or "explorer could not reach a quiescent point", is always a harness problem.

An `AssertionError` from `_check_encoding` can come from either side, because it calls the production helpers. Read the failing assertion, then check two things before deciding:

- **The encoding.** Does `_build_state` write the payload field the real Halo API uses for that `World` field? If not, the harness is wrong. Fix `_build_state`.
- **The helper's contract.** Does the helper still return what its docstring and its callers expect for that payload? Check its own tests (`tests/test_extractors.py`, `tests/test_recall.py`, or `tests/test_controls.py` for `subscription_feature_enabled`) and its diff since the sync commit. If the helper changed, this is a real regression. Report it like a real bug.

## When the code changes on purpose

- **New gate or option in a transaction.** Add an environment event to `ENV_ACTIONS`, the matching `World` field and payload encoding in `_build_state`, a P1 clause in `on_dispatch`, and a mutant that removes the new check.
- **New await point in the write path** (for example, a new await before `pre_dispatch`). Make the corresponding fake park on a gate, so the explorer can interleave events there.
- **New write surface.** It needs its own review (AGENTS.md). Add a scenario, its oracle, and mutants.
- If `check_control_mutants.py` prints `STALE`, the mutant text no longer matches its file. Rewrite the mutant against the new code so it breaks the same guard. Do not just delete it.
- If the explorer (`tests/test_control_interleavings.py`) grows past about a minute, lower a scenario's `budget` or trim its `env` tuple, and record the new bound here.

## Coverage record

| Check | Covers |
|---|---|
| P1 (runtime) | `controls.py`: `async_refresh_snapshot`, `async_preflight_refresh`, `ensure_live_runtime`, and the `ensure_live_runtime` call in each transaction's dispatch-boundary callback, including `RecallOwner.async_press`'s. `live_runtime_check` builds the real check from `hass.data`; the harness models it as `runtime_current` |
| P1, P2, P5 (fence) | `controls.py`: `async_set_fence_mode`, `_validate_options`, `_validate_snapshot`, `_is_confirmed`, `_async_dispatch_and_reconcile`, `control_stale_after`. `helpers.py`: `fence_disable_block_reason`, `is_online`, `pet_fences_enabled`, `has_active_walk`, `pet_for_collar`. `api.py`: `async_set_fences_enabled`, `_async_write_json`, `_async_refresh_if_needed`. Not explored: unknown fence mode. `World.fences_on` is always a bool, so the explorer never reaches the "Halo has not reported current fence mode" refusal. `tests/test_controls.py::test_direct_disable_call_fails_closed_after_fresh_read` covers it at the transaction level |
| P1, P2, P4, P5 (find) | `controls.py`: `async_find_collar`, `_validate_find_options`, `_validate_find_snapshot`, `_ensure_find_cooldown_clear`, `find_collar_cooldown_remaining`, `_async_dispatch_find_and_refresh`. `helpers.py`: `subscription_feature_enabled`, `nested`. `api.py`: `async_find_collar`, `_async_put_no_content` |
| P1, Body, P2, P4, P5, P9, P10, P11 (recall) | `recall.py`: `RecallOwner.async_press`, `classify_recall`, `_resolve`, `_server_clock`, `_advanced`, `RecallOwner.cooldown_remaining`, `RecallOwner._async_save`, `RecallOwner._async_raise_floor`, `_async_dispatch_and_settle`. `helpers.py`: `recall_command_support`, `active_walk_state`, `last_telemetry`, `parse_timestamp`. `api.py`: `async_get_recall_window`, `_parse_timespan`, `async_send_recall_whistle`, `_parse_recall_reply`, `_utc_wire_time`, `_RECALL_CORRECTION_TYPE`. Not explored: storage load failures and malformed saved state; `tests/test_recall.py` covers them, and `tests/test_recall_integration.py::test_unreadable_state_file_never_permits_a_registration` covers the real state file |
| P2, P5, registration (register) | `recall.py`: `RecallOwner.async_register`, `_async_register_and_record`, `_encode`. `api.py`: `async_register_recall_device`, `halo_mobile_id` |
| P8 | The same transaction paths as P1/P5. Catches a gate that refuses every press |
| P3, P6, P7, liveness | `controls.py`: `async_finish_committed_phase`, the `async with control_lock` blocks, the `dispatch_committed` flags. `recall.py`: the `async with self._lock` blocks and their `dispatch_committed` flags |
| Fake fidelity | `transport.py`: `HaloWriteTransport.request` (pre_dispatch immediately before `session.request`). `entity.py`: `HaloEntity.pet` / `collar` (mirrored by `state_getter`). `switch.py` / `button.py` pass `self.pet` / `self.collar` / `coordinator.data.subscription` and a `live_runtime_check` captured at platform setup, and the Recall button passes only its pet ID and that check. `api.py`: `async_fetch_state` samples `server_time_monotonic` before the server-time request (mirrored by `FakeCoordinator`) |

Equivalent mutants, intentionally not listed:

- Removing `ensure_live_runtime` from `async_preflight_refresh`. A reload shuts the replaced runtime's coordinator down in the same step, so a later preflight fetches nothing and is refused, and a fetch already in flight is followed by the dispatch-boundary recheck.
- Removing `_ensure_find_cooldown_clear` from `validate_and_commit_dispatch`. Every cooldowns writer holds the same entry lock, and the post-preflight check already ran under it.

Not listed for budget: re-reading the strictest `stale_after` at the Recall boundary is the same closure the fence and Find mutants prove; `tests/test_recall.py::test_dispatch_boundary_rechecks_current_state` covers the Recall copy. The Recall retry mutant bypasses `pre_dispatch` because a retry through the same callback is refused by the cooldown that callback just started.

CI enforces both commands. The `tests` job runs the explorer, and the `control-mutants` matrix in `.github/workflows/lint.yml` runs each tier and fails on a failed baseline or any `STALE`, `MISSED` or `TIMEOUT` mutant.

CI cannot tell whether the harness still models the code. A fake can drift from the real transport, or an oracle from the documented rule, while every mutant stays `CAUGHT`. The sync commit marks where a person last checked that.

**Sync commit:** `b61031b65bf053404bf1cafd9824ecad7e603e46`. The harness and this record were last confirmed to describe the covered files at this commit.

To see whether model fidelity needs another review:

```bash
git diff --stat b61031b65bf053404bf1cafd9824ecad7e603e46 -- custom_components/halo_collar/controls.py custom_components/halo_collar/recall.py custom_components/halo_collar/api.py custom_components/halo_collar/transport.py custom_components/halo_collar/helpers.py custom_components/halo_collar/entity.py custom_components/halo_collar/switch.py custom_components/halo_collar/button.py custom_components/halo_collar/const.py
```

If that diff is not empty, read it and confirm (or update) that the fakes, gates, oracles, and this coverage table still describe the code. Then move the sync commit forward.
