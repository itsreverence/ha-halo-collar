# AGENTS.md

## Project intent

Build a public-quality Home Assistant custom integration for Halo Collar cloud telemetry.

## Guardrails

- Keep the default installation telemetry-only. Any write/control feature must be separately reviewed, explicitly opt-in, serialize conflicting actions, revalidate current options and fresh authoritative state in the action path, issue at most one request with transport redirects disabled and no replay after an ambiguous result, retain its lock through cancellation-safe post-dispatch reconciliation, and ship with transaction-level tests. State-bearing writes must require reported-state confirmation. A separately reviewed transient physical command may instead treat only an unambiguous provider success as command acceptance when no durable confirmation state exists, but must say so explicitly and retain a conservative post-dispatch cooldown after every attempted dispatch.
- The only currently approved write surfaces are fence mode, Find collar, and the Recall whistle. Fence mode requires reported-state confirmation: fail-safe enable is the first tier, while disabling containment requires a separate stronger opt-in and must be blocked during active walks. Find collar is a separate transient-command tier and must require entitlement, fresh exact mapping, no active or unknown walk, at-most-once dispatch, and a cooldown.
- The Recall whistle is its own transient-command tier with its own opt-in. It may send only `correctionType` `returnwhistle` to `POST /pet/{id}/run-instant-correction`, from one private constant that no option, argument, service field, or configuration can change. Every other correction type on that endpoint stays prohibited. It must require fresh exact pet, collar, and serial mapping, firmware support for numbered commands, no active or unknown walk, a command number saved as spent before dispatch, an expiry from Halo's clock, at-most-once dispatch, and a per-collar cooldown. It needs one Halo mobile registration (`POST /account/mobile-data`), which is approved only from the options flow after explicit confirmation, at most once per entry, with its intent saved first and no second request after an ambiguous result. A press or automation must never register.
- Do not implement any other correction type, fence creation/editing/deletion, bind/unbind, any other account write, or proprietary BLE walk-start behavior without another explicit review.
- Keep the integration generic and publishable; do not bake in specific accounts, entity IDs, dog names, token values, serials, or locations.
- Treat Halo's mobile/cloud API as an unofficial/private API. Keep docs honest and avoid implying vendor support.
- Do not commit real Halo refresh/access tokens or account credentials. The only credential in the repo is the static, app-level OAuth `client_id`/`client_secret` extracted from the Halo app, which is overridable in the config flow.

## Verification

- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and pull-request expectations.
- Run `uv run pytest -q`, `uv run ruff check .`, and `python -m compileall custom_components tests` before public-quality handoff.
- Use [docs/RELEASING.md](docs/RELEASING.md) for release and brand-asset checks.
- Before changing `controls.py`, the write path in `api.py`/`transport.py`, or how `switch.py`/`button.py` call into controls, follow [.agents/skills/verify-control-transactions/SKILL.md](.agents/skills/verify-control-transactions/SKILL.md). It is the exhaustive interleaving check and mutant proof for the guarded write transactions.
