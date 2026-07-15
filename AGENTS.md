# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep the default installation telemetry-only. Any write/control feature must be separately reviewed, explicitly opt-in, unavailable on stale telemetry, avoid blind retries, refresh state after the command, and ship with contract tests.
- The only currently approved write surface is fence mode: fail-safe enable is the first tier, while disabling containment requires a separate stronger opt-in and must be blocked during active walks.
- Do not implement corrections, fence creation/editing/deletion, bind/unbind, account writes, or proprietary BLE walk-start behavior without another explicit review.
- Keep the integration generic and publishable; do not bake in specific accounts, entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit real Halo refresh/access tokens or account credentials. The only credential in the repo is the static, app-level OAuth `client_id`/`client_secret` extracted from the Halo app, which is overridable in the config flow.

## Verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Run `uv run pytest -q`, `uv run ruff check .`, and `python -m compileall custom_components tests` before public-quality handoff.
- Use [docs/RELEASING.md](docs/RELEASING.md) for release and brand-asset checks.
