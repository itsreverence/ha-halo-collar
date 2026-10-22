"""Confirm the control interleaving check still fails on known-broken controls.py variants.

Each mutant is a literal text replacement in custom_components/halo_collar/controls.py.
The script copies the integration and tests into a temporary directory, checks that the
unmutated explorer passes there, then applies one mutant at a time to the copy and runs
tests/test_control_interleavings.py against it. The real checkout is only read. A mutant
that no longer applies means controls.py changed and the verification needs another look.

Run: uv run python scripts/check_control_mutants.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COPIED = ("custom_components", "tests", "pyproject.toml")
CONTROLS = Path("custom_components", "halo_collar", "controls.py")
EXPLORER = "tests/test_control_interleavings.py"
TIMEOUT_SECONDS = 600

MUTANTS: list[tuple[str, str, str]] = [
    (
        "fence pre_dispatch skips option recheck",
        """            nonlocal dispatch_committed
            _validate_options(entry, enabled=enabled)
""",
        """            nonlocal dispatch_committed
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
        await coordinator.async_refresh()
        if coordinator.last_update_success and _is_confirmed(""",
        """    except HaloWriteOutcomeUnknown as err:
        try:
            await client.async_set_fences_enabled(
                pet_id, enabled=enabled, pre_dispatch=pre_dispatch
            )
        except HaloWriteOutcomeUnknown:
            pass
        await coordinator.async_refresh()
        if coordinator.last_update_success and _is_confirmed(""",
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
        """            nonlocal dispatch_committed
            _validate_find_options(entry)
""",
        """            nonlocal dispatch_committed
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


def _copy_tree(workdir: Path) -> None:
    for name in COPIED:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, workdir / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, workdir / name)


def _run_explorer(workdir: Path) -> subprocess.CompletedProcess[str] | None:
    """Run the explorer in workdir without bytecode caches. None means it timed out."""
    try:
        return subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-p", "no:cacheprovider", "-x", "-q", EXPLORER],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="halo-control-mutants-") as tmp:
        workdir = Path(tmp)
        _copy_tree(workdir)
        controls = workdir / CONTROLS
        original = controls.read_bytes()
        # Git may check controls.py out with CRLF; match mutants against LF text.
        text = original.decode("utf-8").replace("\r\n", "\n")
        newline = "\r\n" if b"\r\n" in original else "\n"

        baseline = _run_explorer(workdir)
        if baseline is None or baseline.returncode != 0:
            reason = f"timed out after {TIMEOUT_SECONDS}s" if baseline is None else "failed"
            print(f"BASELINE {reason}: the unmutated explorer must pass before mutants count")
            if baseline is not None:
                print(baseline.stdout[-4000:])
                print(baseline.stderr[-4000:])
            return 1
        print("BASELINE passed")

        missed = 0
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                print(f"STALE   {name}: mutant text no longer matches controls.py")
                missed += 1
                continue
            controls.write_bytes(text.replace(old, new).replace("\n", newline).encode("utf-8"))
            result = _run_explorer(workdir)
            if result is None:
                print(f"TIMEOUT {name}: no verdict after {TIMEOUT_SECONDS}s")
                missed += 1
                continue
            found = re.search(r"Violation: (\[[^\]]+\] .+)", result.stdout)
            if result.returncode != 0 and found:
                print(f"CAUGHT  {name}: {found.group(1)}")
            else:
                print(f"MISSED  {name}")
                print(result.stdout[-2000:])
                print(result.stderr[-2000:])
                missed += 1
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
