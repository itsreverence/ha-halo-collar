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

Unit tests exercise the API client, guarded control transactions, and telemetry extractors without requiring a Home Assistant installation. The locked test suite runs in GitHub Actions on Python 3.11, 3.13, and 3.14; Hassfest and HACS validation run there as separate jobs.

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

Halo Collar is read-only by design. Do not add corrections, fence modification, mode changes, collar control, or bind/unbind endpoints without a separate explicit safety review.

Maintainers use [docs/RELEASING.md](docs/RELEASING.md) for releases.
