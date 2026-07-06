# Halo Collar for Home Assistant

Read-only Home Assistant telemetry for Halo Collar pets and collars.

> Experimental/private-API prototype. This integration is not affiliated with or endorsed by Halo Collar.

## Current status

This repo is an early local prototype. We have proven the Halo cloud API can return pet, collar, subscription, and configuration data with a user-owned Halo account token. The first integration version intentionally exposes sensors/device trackers only.

## Planned entities

- Pet device tracker
- Collar battery percentage and battery status sensors
- Collar connection adapter/status/signal sensors
- Telemetry freshness/online binary sensor
- GPS accuracy sensor
- Safety/fence status sensor
- Fence breach binary sensor
- Firmware/configuration diagnostics

## Safety boundary

v1 is read-only. The Halo mobile API includes write/control endpoints for fence and collar behavior, but this integration does not call them.

## Development

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

## Manual token prototype

The first config flow is intentionally simple: paste a token bundle from the local auth probe plus the required OAuth client details. Before public release, we should replace this with a cleaner browser/device auth flow if practical.
