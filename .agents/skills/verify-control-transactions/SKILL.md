---
name: verify-control-transactions
description: Exhaustively check the guarded write transactions (fence mode and Find collar) against every interleaving of refreshes, option changes, telemetry changes, provider outcomes, and cancellation. Use when a change touches custom_components/halo_collar/controls.py, the write path in api.py or transport.py, how switch.py/button.py call into controls, or the helpers those gates use.
---

# Verify the guarded control transactions

## What this checks

**Property.** Every fence-mode or Find collar PUT is sent at most once per press. It is sent only while every opt-in, freshness, mapping, walk, sync, entitlement, and cooldown gate holds on the current snapshot. It never overlaps another press's dispatch or reconciliation, even when the caller is cancelled. A press reports success only after reported-state confirmation (fence) or an unambiguous provider success (Find collar). A valid press with nothing contending with it succeeds.

**Harm if broken.** Home Assistant could disable a dog's containment during a walk, after the user revoked the opt-in, or for a dog the collar no longer belongs to. It could also replay a physical command, or report success that the collar never confirmed. A transaction that refuses every press would pass every safety property and leave the opted-in controls unusable.

## How it works

`tests/test_control_interleavings.py` runs the **real** `controls.async_set_fence_mode`, `controls.async_find_collar` and `HaloApiClient.async_set_fences_enabled` / `async_find_collar` on a real asyncio loop. Only three network edges are fake, and each one parks on a gate:

| Gate | Fake for | Outcomes explored |
|---|---|---|
| `coordinator_refresh` | `DataUpdateCoordinator.async_refresh` | ok (snapshot := provider truth), fail (`last_update_success=False`) |
| `token_refresh` | `HaloApiClient._async_token_request` (token starts expired or valid) | ok, fail (`HaloAuthError`) |
| `put` | `HaloWriteTransport.put`, which calls `pre_dispatch()` with no await before the PUT, like the real one | fence: ok, timeout_after_apply, connect_error, http_500. find: ok, http_404, timeout, http_500 |

A stateless DFS enumerates every choice: which parked gate to release, each gate's outcome, and up to `budget` environment events. The events are option revocations, `stale_after` tightening, stale telemetry, active or unknown walk, fences unsynced, collar rebound to another pet, one-sided mapping, entitlement lost, fences flipped in the official app, cooldown elapsed, and cancelling a press. Cancellation can be a separate step, or land in the same loop tick as a gate completing (in either order). Environment changes reach the snapshot through a modeled background refresh. Every event calls `_check_encoding`, which asserts that the real helpers (`is_online`, `has_active_walk`, `pet_for_collar`, `pet_fences_enabled`, `subscription_feature_enabled`) read the payload the way the abstract `World` says.

Properties, checked in `Harness.on_dispatch`, `Harness.on_success`, `_wrap_phase`, `Harness.check_uncontested`, and at the end of `Harness.run`:

- **P1 gates at dispatch.** At the instant the PUT starts, every gate holds on the current snapshot and current `entry.options`. The pet/collar mapping also matches this press's preflight refresh.
- **P2 at most once.** A press dispatches at most one PUT, so an ambiguous result is never replayed.
- **P3 serialization.** No PUT while another press's dispatch/reconcile phase is still running.
- **P4 cooldown.** Find collar records the cooldown before dispatch, and no two Find dispatches fall inside `FIND_COLLAR_COOLDOWN_SECONDS`.
- **P5 honest success.** A fence press returns normally only if a successful refresh after its dispatch shows the requested `fencesOn`, synced, fresh, same pet. A Find press returns normally only after a 2xx.
- **P6 no orphans.** No PUT after the press's caller has already returned.
- **P7 reconcile survives cancellation.** Once dispatched, the reconcile phase never ends in `CancelledError`.
- **Liveness.** Every press finishes, the lock is released, and no dispatch phase is left running.
- **P8 uncontested success.** On the schedule where every choice is index 0 (token valid, each gate released in order with its first outcome `ok`, no environment event, no cancellation), each press ends with its scenario's `uncontested` result. `success` means it returned normally after exactly one dispatch. `refused` means `HaloControlError` with no dispatch, as for the second Find inside the cooldown.

Scenarios (`SCENARIOS`): `enable`, `disable`, `find` (one press, budget 2), `find+find` (budget 2), `disable+find`, `disable+enable` (budget 1).

## Scope and limits

The check covers everything above within these bounds: one pet and one collar, at most two concurrent presses, at most one or two environment events per schedule (each scenario's `budget`), and the gate and outcome sets listed. It does **not** cover:

- Real aiohttp behaviour in `HaloWriteTransport` (single lease, `allow_redirects=False`, no replay). `tests/test_api_transport.py` covers that.
- The real `DataUpdateCoordinator`. The harness assumes a refresh either replaces the snapshot or fails.
- Entity `available` properties, button and switch error wrapping, and the `find_status_signal` dispatch.
- Two config entries for the same account. Those have separate locks and cooldown dicts, and nothing serializes them.
- Ordering among tasks that are runnable in the same tick, beyond the gate/cancel combinations above. The driver settles the loop between steps.

## Run it

```bash
uv run pytest tests/test_control_interleavings.py -q        # ~25 s, ~40k schedules
uv run python scripts/check_control_mutants.py              # ~50 s, proves the check can fail
```

The first command also runs inside `uv run pytest -q`. The mutant script copies `custom_components/`, `tests/` and `pyproject.toml` into a temporary directory and never writes to the checkout. It first runs the unmutated explorer there and stops with `BASELINE failed` unless it passes. Then it applies each broken variant of `controls.py` to the copy in turn and expects a `Violation`. Every mutant line should read `CAUGHT`. `STALE`, `MISSED` and `TIMEOUT` (no verdict within 600 s) all make it exit non-zero.

## Reading a failure

A `Violation` prints the broken property and the schedule as numbered steps in the code's own terms. This one came from a scratch change that made `_validate_find_snapshot` test `active_walk_state(...) is True` instead of `has_active_walk(...)`:

```
[find] dispatched Find collar during a walk (unknown)
Schedule:
  1. txn1 presses find
  2. environment: walk_active
  3. environment: walk_unknown
  4. txn1 coordinator_refresh completes: ok
  5. txn1 DISPATCHES find PUT to collar-1 (find)
```

Replay the steps against `controls.py` to decide between two causes:

- **Real bug.** The code really does allow that order. Report it with the schedule. Do not weaken the property or remove the environment event to make it pass, and change product code only if the user asks.
- **Modeling error.** The harness does something the code cannot do. Examples: a fake that awaits where the real code does not, an oracle stricter than the documented rule (such as treating a rebind *before* preflight as a mapping change), or an `_check_encoding` assertion failing. Fix the harness and say so.

An `AssertionError` from `_check_encoding` or `settle` is always a harness problem. So is "explorer could not reach a quiescent point".

## When the code changes on purpose

- **New gate or option in a transaction.** Add an environment event to `ENV_ACTIONS`, the matching `World` field and payload encoding in `_build_state`, a P1 clause in `on_dispatch`, and a mutant that removes the new check.
- **New await point in the write path** (for example, a new await before `pre_dispatch`). Make the corresponding fake park on a gate, so the explorer can interleave events there.
- **New write surface.** It needs its own review (AGENTS.md). Add a scenario, its oracle, and mutants.
- If `check_control_mutants.py` prints `STALE`, the mutant text no longer matches `controls.py`. Rewrite the mutant against the new code so it breaks the same guard. Do not just delete it.
- If the run time grows past about a minute, lower a scenario's `budget` or trim its `env` tuple, and record the new bound here.

## Coverage record

| Check | Covers |
|---|---|
| P1, P2, P5 (fence) | `controls.py`: `async_set_fence_mode`, `_validate_options`, `_validate_snapshot`, `_is_confirmed`, `_async_dispatch_and_reconcile`, `control_stale_after`. `helpers.py`: `fence_disable_block_reason`, `is_online`, `pet_fences_enabled`, `has_active_walk`, `pet_for_collar`. `api.py`: `async_set_fences_enabled`, `_async_put_json`, `_async_refresh_if_needed` |
| P1, P2, P4, P5 (find) | `controls.py`: `async_find_collar`, `_validate_find_options`, `_validate_find_snapshot`, `_ensure_find_cooldown_clear`, `find_collar_cooldown_remaining`, `_async_dispatch_find_and_refresh`. `helpers.py`: `subscription_feature_enabled`, `nested`. `api.py`: `async_find_collar`, `_async_put_no_content` |
| P8 | The same transaction paths as P1/P5. Catches a gate that refuses every press |
| P3, P6, P7, liveness | `controls.py`: `_async_finish_committed_phase`, the `async with control_lock` blocks, the `dispatch_committed` flags |
| Fake fidelity | `transport.py`: `HaloWriteTransport.put` (pre_dispatch immediately before `session.put`). `entity.py`: `HaloEntity.pet` / `collar` (mirrored by `state_getter`). `switch.py` / `button.py` pass `self.pet` / `self.collar` / `coordinator.data.subscription` |

Equivalent mutant, intentionally not listed: removing `_ensure_find_cooldown_clear` from `validate_and_commit_dispatch`. Every writer to `cooldowns` holds the same entry lock, and the post-preflight check already ran under it.

**Sync commit:** `2d36c6a389eeb06cd4d366deaf0b4ca422a39786` (v0.7.7). The harness and this record were last confirmed to describe the covered files at this commit.

To see whether the verification needs another look:

```bash
git diff --stat 2d36c6a389eeb06cd4d366deaf0b4ca422a39786 -- custom_components/halo_collar/controls.py custom_components/halo_collar/api.py custom_components/halo_collar/transport.py custom_components/halo_collar/helpers.py custom_components/halo_collar/entity.py custom_components/halo_collar/switch.py custom_components/halo_collar/button.py
```

Move the sync commit forward only after you have read the diff, confirmed (or updated) that the fakes, gates, oracles, and this coverage table still describe the code at the new commit, and seen both commands above pass with every mutant `CAUGHT`. A green test run alone is not enough.
