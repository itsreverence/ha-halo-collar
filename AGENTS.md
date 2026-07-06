# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep v1 read-only: expose pet/collar telemetry only. Do not implement correction, fence modification, mode changes, bind/unbind, or other write/control endpoints.
- Keep the integration generic and publishable; do not bake in specific accounts, entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit real Halo refresh/access tokens or account credentials. The only credential in the repo is the static, app-level OAuth `client_id`/`client_secret` extracted from the Halo app, which is overridable in the config flow.

## Verification

- Run `uv run pytest -q` before claiming implementation success.
- Run `uv run ruff check .` before public-quality handoff.
- Run `python -m compileall custom_components tests` if HA test dependencies are unavailable.
