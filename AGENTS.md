# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep v1 read-only: expose pet/collar telemetry only. Do not implement correction, fence modification, mode changes, bind/unbind, or other write/control endpoints.
- Keep the integration generic and publishable; do not bake in specific accounts, entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit real Halo refresh/access tokens or account credentials. The only credential in the repo is the static, app-level OAuth `client_id`/`client_secret` extracted from the Halo app, which is overridable in the config flow.

## Verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Run `uv run pytest -q`, `uv run ruff check .`, and `python -m compileall custom_components tests` before public-quality handoff.
- Use [docs/RELEASING.md](docs/RELEASING.md) for release and brand-asset checks.
