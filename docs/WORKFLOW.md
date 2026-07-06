# Workflow

## Local development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
python -m compileall custom_components tests
```

## Private Home Assistant deploy loop

Use Samba/HA config access to copy only the integration source under:

```text
/config/custom_components/halo_collar/
```

Recommended private-test deploy steps:

1. Back up the live `/config/custom_components/halo_collar` directory.
2. Upload changed source files and translations.
3. Remove stale `__pycache__` files for changed modules.
4. Re-fetch uploaded files and compare hashes with local files.
5. Run `homeassistant.check_config`.
6. Restart Home Assistant when Python, config flow, manifest, or translations changed.
7. Verify live entities after HA finishes booting.

## Current private prototype verification

Known-good local gates after the cleanup pass:

```bash
uv run pytest -q              # 3 passed
uv run ruff check .           # All checks passed
python -m compileall custom_components tests
```

Known-good live checks after deploy/restart:

- `sensor.cowboy_battery_status` reports a human label such as `Not charging`.
- `sensor.cowboy_battery_runtime` reports hours (`h`) instead of raw seconds.
- `sensor.cowboy_wifi_status` reports `Connected` instead of `socketconnected`.
- `sensor.cowboy_location_status` reports `Indoors` when Halo does not provide GPS coordinates.
- `binary_sensor.cowboy_fence_breach` and calibration problem sensors exist with explicit entity IDs.

## Next stabilization pass

1. Add telemetry freshness sensors:
   - Last telemetry timestamp
   - Seconds/minutes until next expected telemetry
   - Stale/online status based on manifest timestamp
2. Improve device tracker fallback:
   - Use lat/lon only when present
   - Keep `Location status` as the user-readable state for `Indoors`/non-GPS situations
   - Do not invent coordinates from vague status values
3. Add focused extractor tests for stale timestamps and location fallback payload shapes.
4. Dogfood in Larry's Home Assistant for 24–48 hours before public/HACS hardening.

## Public/HACS prep reminders

Keep public prep in a separate pass/branch/chat. Before making the repo public:

- Run a secret scan over tracked files and git history.
- Remove real token bundles and redacted-live payloads from the repo.
- Decide whether the bundled OAuth client details are acceptable to publish or should move behind user-provided config/auth docs.
- Add GitHub Actions, HACS action, Hassfest, `SECURITY.md`, and public install docs.
