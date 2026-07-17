from __future__ import annotations

import email
import json
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

RUNTIME_ROOT = "custom_components/halo_collar/"
FORBIDDEN_RUNTIME_BYTES = (b"/home/", b"PRIVATE_", b"Cowboy")


def _source_runtime_files(repo: Path) -> set[str]:
    root = repo / RUNTIME_ROOT
    return {
        path.relative_to(repo).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    wheels = list(output.glob("*.whl"))
    sdists = list(output.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(f"expected one wheel and one sdist, got {wheels!r} and {sdists!r}")

    expected_version = tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]
    expected_files = _source_runtime_files(repo)

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_files = {name for name in archive.namelist() if name.startswith(RUNTIME_ROOT)}
        if wheel_files != expected_files:
            raise SystemExit(
                f"wheel runtime file mismatch: missing={expected_files - wheel_files}, "
                f"extra={wheel_files - expected_files}"
            )
        manifest = json.loads(archive.read(f"{RUNTIME_ROOT}manifest.json"))
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))
        wheel_runtime = {name: archive.read(name) for name in wheel_files}

    with tarfile.open(sdists[0]) as archive:
        sdist_runtime: dict[str, bytes] = {}
        for member in archive.getmembers():
            marker = f"/{RUNTIME_ROOT}"
            if member.isfile() and marker in member.name:
                relative = member.name.split(marker, 1)[1]
                name = f"{RUNTIME_ROOT}{relative}"
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise SystemExit(f"could not read {member.name}")
                sdist_runtime[name] = extracted.read()

    if set(sdist_runtime) != expected_files:
        raise SystemExit(
            f"sdist runtime file mismatch: missing={expected_files - set(sdist_runtime)}, "
            f"extra={set(sdist_runtime) - expected_files}"
        )
    sdist_manifest = json.loads(sdist_runtime[f"{RUNTIME_ROOT}manifest.json"])
    versions = {
        expected_version,
        manifest.get("version"),
        sdist_manifest.get("version"),
        metadata["Version"],
    }
    if versions != {expected_version}:
        raise SystemExit(f"artifact version mismatch: {versions!r}")

    for archive_name, runtime in ((wheels[0].name, wheel_runtime), (sdists[0].name, sdist_runtime)):
        joined = b"\n".join(runtime.values())
        for forbidden in FORBIDDEN_RUNTIME_BYTES:
            if forbidden in joined:
                raise SystemExit(f"{archive_name} runtime contains forbidden marker {forbidden!r}")

    print(
        f"release_artifacts=PASS version={expected_version} "
        f"runtime_files={len(expected_files)} wheel={wheels[0].name} sdist={sdists[0].name}"
    )


if __name__ == "__main__":
    main()
