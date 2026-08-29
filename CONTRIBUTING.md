# Contributing to Halo Collar

Thanks for helping improve this unofficial Home Assistant integration. Bug fixes, telemetry compatibility updates, documentation, translations, and focused feature proposals are welcome.

## Before you start

- Search existing issues before opening a new one.
- Discuss substantial behavior or entity-model changes in an issue first.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
- Never include Halo passwords, tokens, pet names, serial numbers, precise locations, fences, account identifiers, or unredacted diagnostics and API payloads.

## Development setup

Install [uv](https://docs.astral.sh/uv/) and run:

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
python -m compileall custom_components tests
```

Run the full current-stable Home Assistant contract separately so its large,
Home Assistant-pinned dependency graph does not enter the project lockfile:

```bash
uv run --isolated --python 3.14.2 \
  --with-requirements requirements_test_ha_stable.txt \
  pytest -q
```

Unit tests exercise the API client, authentication/configuration/reauth/options flows, guarded fence and Find collar transactions, cooldown/dispatch-boundary behavior, provider-payload normalization, and telemetry extractors without requiring a live Halo account. On Python 3.14.2+, the pinned `pytest-homeassistant-custom-component` harness loads the current stable Home Assistant contract and verifies config-entry setup/failure behavior, every platform, option-driven control lifecycle, token persistence, and lock identity across real unload/reload operations. A separate isolated compatibility lane loads Home Assistant 2024.12.0 on Python 3.12.7 and verifies the tracker import, indoor-home behavior, and options-flow startup against the documented minimum API. GitHub Actions also exercise the portable unit suite across the supported development matrix; Hassfest, HACS validation, release-artifact integrity, and Ruff run as separate jobs.

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
