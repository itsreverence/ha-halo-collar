"""Confirm the control interleaving check still fails on known-broken product variants.

Each mutant is a literal text replacement in one integration file. The script copies the
integration and tests into a temporary directory, checks that the unmutated explorer passes
there for the tier's scenarios, then applies one mutant at a time to the copy and runs
tests/test_control_interleavings.py against it. The real checkout is only read. A mutant
that no longer applies means its file changed and the verification needs another look.

Tiers run separately so each CI job stays inside its time cap:
  controls: fence and Find collar (controls.py), against the fence and Find scenarios
  recall:   the Recall whistle (recall.py, api.py), against the Recall scenarios
  runtime:  the live-runtime and fresh-fetch checks every press shares, against the
            scenarios that unload the entry

Run: uv run python scripts/check_control_mutants.py [controls] [recall] [runtime]
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
COPIED = ("custom_components", "tests", "pyproject.toml")
INTEGRATION = Path("custom_components", "halo_collar")
CONTROLS = INTEGRATION / "controls.py"
RECALL = INTEGRATION / "recall.py"
API = INTEGRATION / "api.py"
EXPLORER = "tests/test_control_interleavings.py"
TIMEOUT_SECONDS = 90
CONTROL_SCENARIOS = "not recall and not register"


class Mutant(NamedTuple):
    name: str
    old: str
    new: str
    path: Path = CONTROLS
    selector: str = CONTROL_SCENARIOS


CONTROL_MUTANTS: list[tuple[str, str, str]] = [
    (
        "fence pre_dispatch skips option recheck",
        """            ensure_live_runtime(is_current)
            _validate_options(entry, enabled=enabled)
""",
        """            ensure_live_runtime(is_current)
""",
    ),
    (
        "fence pre_dispatch skips snapshot recheck",
        """            _validate_snapshot(
                state_getter,
                enabled=enabled,
                stale_after=current_strictest_stale_after(),
                expected_pet_id=pet_id,
                expected_collar_id=collar_id,
            )
""",
        "",
    ),
    (
        "stale_after is not re-read at dispatch",
        """            strictest_stale_after = min(
                strictest_stale_after,
                control_stale_after(entry),
            )
            return strictest_stale_after

        pet, collar = _validate_snapshot(""",
        """            return strictest_stale_after

        pet, collar = _validate_snapshot(""",
    ),
    (
        "confirmation ignores requested fence mode",
        "        and pet_fences_enabled(pet) is enabled\n",
        "",
    ),
    (
        "confirmation ignores fence sync",
        '        and pet.get("isFencesSynchronized") is True\n',
        "",
    ),
    (
        "ambiguous fence write is replayed once",
        """    except HaloWriteOutcomeUnknown as err:
        if await async_refresh_snapshot(coordinator) and _is_confirmed(""",
        """    except HaloWriteOutcomeUnknown as err:
        try:
            await client.async_set_fences_enabled(
                pet_id, enabled=enabled, pre_dispatch=pre_dispatch
            )
        except HaloWriteOutcomeUnknown:
            pass
        if await async_refresh_snapshot(coordinator) and _is_confirmed(""",
    ),
    (
        "cancellation after commit abandons reconciliation",
        """            cancelled = True
            if not dispatch_committed():""",
        """            cancelled = True
            if True:""",
    ),
    (
        "cancellation after commit releases the lock early",
        """            if task.done():
                break
""",
        """            raise
""",
    ),
    (
        "cancellation before commit orphans the dispatch task",
        """                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
                raise""",
        """                raise""",
    ),
    (
        "fence disable skips the walk/sync block",
        """    if not enabled:
        block_reason = fence_disable_block_reason(pet, collar, stale_after=stale_after)
        if block_reason is not None:
            raise HaloControlError(block_reason)
""",
        "",
    ),
    (
        "find accepts a one-sided pet/collar mapping",
        '    if nested(collar, "petInfo", "id") != pet_id'
        ' or nested(pet, "collarInfo", "id") != collar_id:\n'
        '        raise HaloControlError("Halo pet/collar mapping is unavailable")\n',
        "",
    ),
    (
        "find pre_dispatch ignores a rebind since preflight",
        """                expected_pet_id=pet_id,
                expected_collar_id=collar_id,
            )
            _ensure_find_cooldown_clear""",
        """                expected_collar_id=collar_id,
            )
            _ensure_find_cooldown_clear""",
    ),
    (
        "find skips the walk check",
        """    if has_active_walk(pet, collar):
        raise HaloControlError("Find Collar is blocked during an active or unknown walk")
""",
        "",
    ),
    (
        "find skips the entitlement check",
        """    if not subscription_feature_enabled(subscription, "findcollar"):
        raise HaloControlError("Halo subscription does not enable Find Collar")
""",
        "",
    ),
    (
        "find pre_dispatch skips option recheck",
        """            ensure_live_runtime(is_current)
            _validate_find_options(entry)
""",
        """            ensure_live_runtime(is_current)
""",
    ),
    (
        "find cooldown recorded only after success",
        """            cooldowns[collar_id] = time.monotonic()
            dispatch_committed = True""",
        """            dispatch_committed = True""",
    ),
    (
        "fence refuses every press",
        '''    """Fail closed unless the current snapshot is safe for this exact target."""
''',
        '''    """Fail closed unless the current snapshot is safe for this exact target."""
    raise HaloControlError("refuse")
''',
    ),
    (
        "find refuses every press",
        '''    """Fail closed unless Find Collar is safe for one exact current mapping."""
''',
        '''    """Fail closed unless Find Collar is safe for one exact current mapping."""
    raise HaloControlError("refuse")
''',
    ),
]
# Not listed: removing _ensure_find_cooldown_clear from validate_and_commit_dispatch is
# an equivalent mutant. Every cooldowns writer holds the same entry lock, and the
# post-preflight check already ran under it, so the recheck cannot change an outcome.

RECALL_SCENARIOS = "recall"
REGISTRATION_SCENARIOS = "register"
RECALL_MUTANTS = [
    Mutant(
        "recall pre_dispatch skips the boundary recheck",
        """                if authorized_target(current_strictest_stale_after()) != target:
                    raise HaloControlError(
                        "Halo pet/collar mapping changed before command dispatch"
                    )
                ensure_lifetime()
""",
        """                ensure_lifetime()
""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall pre_dispatch ignores a collar serial change",
        "                if authorized_target(current_strictest_stale_after()) != target:\n",
        "                if authorized_target(current_strictest_stale_after()).pet_id != "
        "target.pet_id:\n",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall pre_dispatch skips the option recheck",
        """                    enabled=_enabled(entry),
                    registration=registration,
""",
        """                    enabled=True,
                    registration=registration,
""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall pre_dispatch skips the lifetime check",
        """                ensure_lifetime()
                self._cooldown_until""",
        """                self._cooldown_until""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall cooldown is not recorded at dispatch",
        "                self._cooldown_until[target.serial] = time.monotonic() + "
        "RECALL_COOLDOWN_SECONDS\n",
        "",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall command number is not spent before dispatch",
        "await self._async_save(_advanced(registration, target.serial, number + 1))",
        "await self._async_save(_advanced(registration, target.serial, number))",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall expiry ignores the 30 second cap",
        "            window = min(await client.async_get_recall_window(), RECALL_MAX_LIFETIME)\n",
        "            window = await client.async_get_recall_window()\n",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall ignores currentCommandNumber",
        "        await owner._async_raise_floor(serial, reply.current_command_number + 1)\n",
        "        pass\n",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall treats OldCommandNumber as success",
        """    if isinstance(reply, HaloRecallAccepted):
        return
""",
        """    if not isinstance(reply, HaloRecallExpired):
        return
""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall retries once after an ambiguous outcome",
        """    except HaloWriteOutcomeUnknown as err:
        raise HaloControlError(
            "Halo Recall whistle outcome is unknown.""",
        """    except HaloWriteOutcomeUnknown as err:
        await client.async_send_recall_whistle(pet_id, command, pre_dispatch=lambda: None)
        raise HaloControlError(
            "Halo Recall whistle outcome is unknown.""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall skips the walk check",
        """    if walking:
        return RecallReason.WALK_ACTIVE
""",
        "",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall skips the firmware check",
        """    if not supported:
        return RecallReason.FIRMWARE_UNSUPPORTED
""",
        "",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "recall refuses every press",
        """    if not enabled:
        return RecallReason.DISABLED
""",
        """    if True:
        return RecallReason.DISABLED
""",
        RECALL,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "whistle sends another correction type",
        '_RECALL_CORRECTION_TYPE = "returnwhistle"\n',
        '_RECALL_CORRECTION_TYPE = "warning"\n',
        API,
        RECALL_SCENARIOS,
    ),
    Mutant(
        "registration posts without a saved intent",
        """        try:
            await owner._async_save(pending)
        except RecallStorageError as err:""",
        """        try:
            pass
        except RecallStorageError as err:""",
        RECALL,
        REGISTRATION_SCENARIOS,
    ),
    Mutant(
        "registration skips the runtime recheck at dispatch",
        """                nonlocal dispatch_committed
                if not is_current():
                    raise HaloControlError(_RUNTIME_CHANGED)
""",
        """                nonlocal dispatch_committed
""",
        RECALL,
        REGISTRATION_SCENARIOS,
    ),
    Mutant(
        "registration forgets an ambiguous outcome",
        "RegistrationUncertain(pending.installation_id) if dispatched() else Unregistered()",
        "Unregistered()",
        RECALL,
        REGISTRATION_SCENARIOS,
    ),
    Mutant(
        "registration resends after an ambiguous outcome",
        """            if not isinstance(self._registration, Unregistered):
                _require_registered(self._registration)
                return
""",
        """            if isinstance(self._registration, Registered):
                return
""",
        RECALL,
        REGISTRATION_SCENARIOS,
    ),
]

RUNTIME_SCENARIOS = f"not {REGISTRATION_SCENARIOS}"
FENCE_SCENARIOS = "able and not find and not recall"
RUNTIME_MUTANTS = [
    Mutant(
        "refresh trusts a coordinator that fetched nothing",
        "    return coordinator.last_update_success and coordinator.data is not previous\n",
        "    return coordinator.last_update_success\n",
        CONTROLS,
        RUNTIME_SCENARIOS,
    ),
    Mutant(
        "preflight trusts a refresh that fetched nothing",
        """    fetched = await async_refresh_snapshot(coordinator)
    ensure_live_runtime(is_current)
    if not fetched:
""",
        """    await coordinator.async_refresh()
    ensure_live_runtime(is_current)
    if not coordinator.last_update_success:
""",
        CONTROLS,
        "enable and find",
    ),
    Mutant(
        "fence pre_dispatch skips the live-runtime recheck",
        """            nonlocal dispatch_committed
            ensure_live_runtime(is_current)
            _validate_options(entry, enabled=enabled)
""",
        """            nonlocal dispatch_committed
            _validate_options(entry, enabled=enabled)
""",
        CONTROLS,
        FENCE_SCENARIOS,
    ),
    Mutant(
        "find pre_dispatch skips the live-runtime recheck",
        """            nonlocal dispatch_committed
            ensure_live_runtime(is_current)
            _validate_find_options(entry)
""",
        """            nonlocal dispatch_committed
            _validate_find_options(entry)
""",
        CONTROLS,
        "find and not recall",
    ),
    Mutant(
        "fence confirmation trusts a refresh that fetched nothing",
        """    if not await async_refresh_snapshot(coordinator):
        raise HaloControlError(
            "Halo command was sent but state confirmation failed""",
        """    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise HaloControlError(
            "Halo command was sent but state confirmation failed""",
        CONTROLS,
        FENCE_SCENARIOS,
    ),
    Mutant(
        "recall pre_dispatch skips the live-runtime recheck",
        """                nonlocal dispatch_committed
                ensure_live_runtime(is_current)
""",
        """                nonlocal dispatch_committed
""",
        RECALL,
        RECALL_SCENARIOS,
    ),
]

TIERS: dict[str, tuple[str, list[Mutant]]] = {
    "controls": (CONTROL_SCENARIOS, [Mutant(*mutant) for mutant in CONTROL_MUTANTS]),
    "recall": (f"{RECALL_SCENARIOS} or {REGISTRATION_SCENARIOS}", RECALL_MUTANTS),
    "runtime": (RUNTIME_SCENARIOS, RUNTIME_MUTANTS),
}


def _copy_tree(workdir: Path) -> None:
    for name in COPIED:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, workdir / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, workdir / name)


def _run_explorer(workdir: Path, selector: str) -> subprocess.CompletedProcess[str] | None:
    """Run the selected scenarios in workdir without bytecode caches. None means timeout."""
    command = [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-x", "-q"]
    try:
        return subprocess.run(
            [*command, "-k", selector, EXPLORER],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None


def _run_tier(workdir: Path, tier: str) -> int:
    baseline_selector, mutants = TIERS[tier]
    baseline = _run_explorer(workdir, baseline_selector)
    if baseline is None or baseline.returncode != 0:
        reason = f"timed out after {TIMEOUT_SECONDS}s" if baseline is None else "failed"
        print(f"BASELINE {tier} {reason}: the unmutated explorer must pass before mutants count")
        if baseline is not None:
            print(baseline.stdout[-4000:])
            print(baseline.stderr[-4000:])
        return 1
    print(f"BASELINE {tier} passed")

    missed = 0
    for mutant in mutants:
        target = workdir / mutant.path
        original = target.read_bytes()
        # Git may check files out with CRLF; match mutants against LF text.
        text = original.decode("utf-8").replace("\r\n", "\n")
        newline = "\r\n" if b"\r\n" in original else "\n"
        if text.count(mutant.old) != 1:
            print(f"STALE   {mutant.name}: mutant text no longer matches {mutant.path.name}")
            missed += 1
            continue
        mutated = text.replace(mutant.old, mutant.new).replace("\n", newline)
        target.write_bytes(mutated.encode("utf-8"))
        try:
            result = _run_explorer(workdir, mutant.selector)
        finally:
            target.write_bytes(original)
        if result is None:
            print(f"TIMEOUT {mutant.name}: no verdict after {TIMEOUT_SECONDS}s")
            missed += 1
            continue
        found = re.search(r"Violation: (\[[^\]]+\] .+)", result.stdout)
        if result.returncode != 0 and found:
            print(f"CAUGHT  {mutant.name}: {found.group(1)}")
        else:
            print(f"MISSED  {mutant.name}")
            print(result.stdout[-2000:])
            print(result.stderr[-2000:])
            missed += 1
    return missed


def main(argv: list[str]) -> int:
    tiers = argv or list(TIERS)
    unknown = sorted(set(tiers) - set(TIERS))
    if unknown:
        print(f"Unknown tier {', '.join(unknown)}; choose from {', '.join(TIERS)}")
        return 2
    with tempfile.TemporaryDirectory(prefix="halo-control-mutants-") as tmp:
        workdir = Path(tmp)
        _copy_tree(workdir)
        missed = sum(_run_tier(workdir, tier) for tier in tiers)
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
