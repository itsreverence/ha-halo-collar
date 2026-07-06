# Workflow

## Local development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
python -m compileall custom_components tests
```

The unit tests do not require a Home Assistant install; they exercise the API
client and telemetry extractors directly. `hassfest` and HACS validation run in
CI via GitHub Actions (`.github/workflows/validate.yml`).

## Testing in a live Home Assistant instance

To try changes against a real instance, copy only the integration source into
your Home Assistant configuration directory:

```text
/config/custom_components/halo_collar/
```

Suggested loop:

1. Back up the existing `/config/custom_components/halo_collar` directory.
2. Copy the changed source files and translations.
3. Remove stale `__pycache__` files for changed modules.
4. Run `homeassistant.check_config`.
5. Restart Home Assistant when Python, config flow, manifest, or translations changed.
6. Verify entities after Home Assistant finishes booting.

## Release checklist

Before cutting a public release / GitHub tag:

- Bump `version` in `custom_components/halo_collar/manifest.json` and
  `pyproject.toml`.
- Confirm `uv run pytest -q`, `uv run ruff check .`, and
  `uv run ruff format --check .` pass.
- Confirm the `Validate` workflow (hassfest + HACS) is green.
- Create a GitHub release/tag so HACS can offer a versioned download.

## HACS default submission

1. Ensure the repository is public with a description and topics.
2. Add the `halo_collar` domain to
   [`home-assistant/brands`](https://github.com/home-assistant/brands) (icon +
   logo). See `docs/BRANDS.md`.
3. Open a PR against [`hacs/default`](https://github.com/hacs/default) adding
   `itsreverence/ha-halo-collar` to the `integration` list.
