# Halo Collar for Home Assistant

[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml/badge.svg)](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml)

A read-only Home Assistant custom integration that surfaces telemetry from your [Halo Collar](https://www.halocollar.com/) pets and collars: location, battery, connectivity, GPS status, safety/fence status, and firmware.

> [!IMPORTANT]
> This is an **unofficial** integration. It is not affiliated with, endorsed by, or supported by Halo Collar / Protect Animals With Satellites, LLC. It talks to Halo's private mobile/cloud API, which may change or break at any time. Use at your own risk.

## Features

This integration is intentionally **read-only** — it never modifies fences, corrections, modes, or collar behavior. For each collar on your account it exposes:

- **Device tracker** — pet location when Halo reports GPS coordinates.
- **Sensors** — battery %, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi status/signal, cellular status/signal, GPS accuracy, location status, safety status, and firmware version.
- **Binary sensors** — connectivity (online/stale), fence breach, GPS calibration required, compass calibration required.

## Requirements

- Home Assistant 2024.1 or newer.
- A Halo account with an active Pack Membership Plan and at least one paired collar (the same email/password you use in the Halo mobile app).

## Installation

### HACS (recommended)

1. In HACS, open the menu (top-right) → **Custom repositories**.
2. Add `https://github.com/itsreverence/ha-halo-collar` with category **Integration**.
3. Search for **Halo Collar** in HACS and install it.
4. Restart Home Assistant.

> Once this repository is accepted into the HACS default list, the custom-repository step will no longer be needed.

### Manual

1. Copy `custom_components/halo_collar` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Halo Collar**.
3. Enter the **email** and **password** for your Halo account.
4. Submit. The integration signs in, discovers your collars, and creates the entities.

The advanced *OAuth client ID/secret* fields are pre-filled with the values used by the official Halo app and normally do not need to be changed. If Halo rotates these, you can override them here without waiting for a new release.

If your session expires or you change your Halo password, Home Assistant will prompt you to **re-authenticate** — just re-enter your credentials.

### How authentication works

Your credentials are exchanged with Halo's identity server (`auth.halocollar.com`) for OAuth access/refresh tokens using the password grant, the same flow the mobile app uses. Home Assistant stores the resulting tokens (and your credentials, for automatic re-authentication) in the config entry and refreshes the access token automatically. Data is polled about every 5 minutes.

## Disclaimer & safety

- **Read-only by design.** This integration deliberately does not implement any write/control endpoints (fence editing, corrections, mode changes, bind/unbind, etc.). Do not rely on it for your pet's containment or safety — the official Halo app and collar are the source of truth.
- Because it depends on an undocumented API, functionality may degrade without notice.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

If Home Assistant test dependencies are unavailable, you can still sanity-check imports:

```bash
python -m compileall custom_components tests
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/WORKFLOW.md`](docs/WORKFLOW.md) for internals and contributor workflow.

## License

Released under the [MIT License](LICENSE).
