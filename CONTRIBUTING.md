# Contributing to Halo Collar

Thanks for helping improve this unofficial Home Assistant integration. Bug fixes, telemetry compatibility updates, documentation, translations, and focused feature proposals are welcome.

## Before you start

- Search existing issues before opening a new one.
- Discuss substantial behavior or entity-model changes in an issue first.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Never include Halo passwords, tokens, pet names, serial numbers, precise locations, fences, account identifiers, or unredacted diagnostics and API payloads.

## Development setup

### Portable tests

Install [uv](https://docs.astral.sh/uv/) and run:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
python -m compileall custom_components tests
```

The portable suite tests the API client, guarded control transactions, cooldowns,
provider-payload normalization, and telemetry extractors without a live Halo account.
Tests that require Home Assistant skip when it is absent.

### Pinned Home Assistant contract

Run the full Home Assistant 2026.10.2 contract separately so its dependency graph
stays out of the project lockfile. This target advances when maintainers update
`requirements_test_ha_stable.txt`, not whenever Home Assistant releases a version.
The requirements pin `pytest-homeassistant-custom-component==0.13.373` and
`voluptuous-serialize==2.7.0`:

```bash
uv run --isolated --python 3.14.2 \
  --with-requirements requirements_test_ha_stable.txt \
  pytest -q
```

This lane verifies setup and failure behavior, every platform, configuration and
options flows, control lifecycle, token persistence, and lock identity across real
unload and reload operations. Reauthentication coverage includes changed and
identical credentials, abort before reload dispatch, overlapping setup requests,
and entries without a runtime.

### Minimum Home Assistant compatibility

Run the Home Assistant 2024.12.0 compatibility and write-transport tests separately:

```bash
uv run --isolated --python 3.12.7 \
  --with homeassistant==2024.12.0 --with josepy==1.14.0 \
  --with pytest==8.3.3 --with pytest-asyncio==0.24.0 \
  pytest -q -p no:homeassistant-custom-component \
  tests/test_tracker_compat.py tests/test_api_transport.py tests/test_status_compat.py \
  tests/test_telemetry_compat.py
```

This lane verifies tracker import, indoor Home state, options-flow startup, and
control-status readability and listener isolation through real entity writes.
It also writes and refreshes reported-activity and every recent-walk sensor,
including enum metadata, unknown, unavailable, and cached stale values.
Reauthentication checks call installed ConfigFlow methods and the Home Assistant
task scheduler with synthetic entry, storage, login, and reload boundaries.
Worker-cleanup tests also use synthetic config-entry boundaries.

### Coverage limits

These tests use synthetic provider data. They do not validate live Halo behavior,
physical command execution, or containment. GitHub Actions runs the portable suite
on Python 3.11, 3.13, and 3.14. Hassfest, HACS validation, release-artifact integrity,
and Ruff run as separate jobs. See [the workflow](.github/workflows/lint.yml) for
exact test-lane commands.

## Testing in Home Assistant

1. Back up `/config/custom_components/halo_collar`.
2. Copy the changed integration source and translations into that directory.
3. Remove stale `__pycache__` files for changed modules.
4. Run the deployment-appropriate configuration check:
   - Home Assistant OS/Supervised: `ha core check`
   - Home Assistant Core environment: `python -m homeassistant --script check_config -c /config`
5. Restart Home Assistant when Python, manifest, config-flow, or translation files changed.
6. Confirm the expected entities populate and compare important telemetry with the official Halo app.

Use only test or carefully redacted data in screenshots and fixtures.

## Pull requests

Keep changes narrowly scoped and explain:

- the user-visible problem or benefit;
- private-API compatibility and privacy implications;
- tests added or updated;
- the commands you ran.

Halo Collar is telemetry-first, and all writes are opt-in. Do not add corrections, fence geometry changes, collar commands, walk lifecycle actions, or bind/unbind endpoints without a separate explicit safety review and fail-closed tests.

Maintainers use [docs/RELEASING.md](docs/RELEASING.md) for releases.
