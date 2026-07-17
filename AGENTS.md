# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep the default installation telemetry-only. Any write/control feature must be separately reviewed, explicitly opt-in, serialize conflicting actions, revalidate current options and fresh authoritative state in the action path, issue at most one request with transport redirects disabled and no replay after an ambiguous result, retain its lock through cancellation-safe post-dispatch reconciliation, and ship with transaction-level tests. State-bearing writes must require reported-state confirmation. A separately reviewed transient physical command may instead treat only an unambiguous provider success as command acceptance when no durable confirmation state exists, but must say so explicitly and retain a conservative post-dispatch cooldown after every attempted dispatch.
- The only currently approved write surfaces are fence mode and Find collar. Fence mode requires reported-state confirmation: fail-safe enable is the first tier, while disabling containment requires a separate stronger opt-in and must be blocked during active walks. Find collar is a separate transient-command tier and must require entitlement, fresh exact mapping, no active or unknown walk, at-most-once dispatch, and a cooldown.
- Do not implement corrections, fence creation/editing/deletion, bind/unbind, account writes, or proprietary BLE walk-start behavior without another explicit review.
- Keep the integration generic and publishable; do not bake in specific accounts, entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit real Halo refresh/access tokens or account credentials. The only credential in the repo is the static, app-level OAuth `client_id`/`client_secret` extracted from the Halo app, which is overridable in the config flow.

## Verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Run `uv run pytest -q`, `uv run ruff check .`, and `python -m compileall custom_components tests` before public-quality handoff.
- Use [docs/RELEASING.md](docs/RELEASING.md) for release and brand-asset checks.
