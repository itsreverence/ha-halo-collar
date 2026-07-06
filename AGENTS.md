# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep v1 read-only: expose pet/collar telemetry only. Do not implement correction, fence modification, mode changes, bind/unbind, or other write/control endpoints.
- Keep the integration generic and publishable; do not bake in Larry/Ricky Home entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit Halo refresh/access tokens. Local token bundles belong outside the repo.
- Avoid hardcoding extracted secrets in public docs. For the initial prototype, allow users to provide required OAuth client details through config/options.

## Verification

- Run `uv run pytest -q` before claiming implementation success.
- Run `uv run ruff check .` before public-quality handoff.
- Run `python -m compileall custom_components tests` if HA test dependencies are unavailable.
