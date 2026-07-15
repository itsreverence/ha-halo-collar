# Halo Collar for Home Assistant

<p align="center">
  <img src="custom_components/halo_collar/brand/icon.png" alt="Halo Collar integration icon" width="128" height="128">
</p>

[![Latest release](https://img.shields.io/github/v/release/itsreverence/ha-halo-collar)](https://github.com/itsreverence/ha-halo-collar/releases)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml/badge.svg)](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml)

A telemetry-first Home Assistant custom integration for your [Halo Collar](https://www.halocollar.com/) pets and collars: location, battery, connectivity, GPS status, safety/fence status, firmware, and guarded opt-in fence controls.

> [!IMPORTANT]
> This is an **unofficial** integration. It is not affiliated with, endorsed by, or supported by Halo Collar / Protect Animals With Satellites, LLC. It talks to Halo's private mobile/cloud API, which may change or break at any time. Use at your own risk.

## Features

The integration is **telemetry-only by default**. For each collar on your account it exposes:

- **Device tracker** — pet location when Halo reports GPS coordinates. When the collar reports it is **indoors on its configured Wi-Fi** (where GPS is unreliable), the tracker pins the pet to `home` instead of drifting on a jittery fix.
- **Sensors** — battery %, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi status/signal, cellular status/signal, GPS accuracy, location status, safety status, firmware version, and last telemetry (when the collar last reported to the Halo cloud).
- **Binary sensors** — connectivity (online/stale), fence breach, fence mode/synchronization, GPS calibration required, compass calibration required.
- **Events** — a fence breach event entity you can use directly as an automation trigger.
- **Optional fence controls** — an idempotent **Enable fences** button, plus a separately opted-in **Fence mode** switch that can disable containment. Controls are unavailable on stale telemetry, fence-off is blocked during active walks, writes are not blindly retried, and state is refreshed after every command.
- **Diagnostics** — download redacted diagnostics (tokens, serials, locations, and names removed) from the integration page to attach to bug reports.

## Requirements

- Home Assistant 2024.11 or newer.
- A Halo account with an active Pack Membership Plan and at least one paired collar (the same email/password you use in the Halo mobile app).

## Installation

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=itsreverence&repository=ha-halo-collar&category=integration)
[![Add the Halo Collar integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=halo_collar)

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

### Confirm it is working

After setup, open the Halo Collar device in Home Assistant and confirm its device tracker, battery, connectivity, and last-telemetry entities are populated. Compare important location and status values with the official Halo app before using them in automations.

### How authentication works

Your credentials are exchanged with Halo's identity server (`auth.halocollar.com`) for OAuth access/refresh tokens using the password grant, the same flow the mobile app uses. Your password is **not stored** — Home Assistant keeps only the resulting tokens in the config entry and refreshes the access token automatically. If the refresh token ever becomes invalid, you'll be prompted to re-enter your credentials.

### Options

Open the integration in **Settings → Devices & Services** and click **Configure** to set:

- **Update interval** (60–3600 seconds, default 300) — how often the Halo cloud is polled. Shorter intervals put more load on Halo's cloud and drain nothing on the collar; the collar reports on its own schedule regardless.
- **Offline after** (120–86400 seconds, default 900) — how long after the collar's last report the connectivity sensor flips to offline.
- **Enable fail-safe fence controls** (off by default) — adds an **Enable fences** button. This can restore fence enforcement but cannot turn it off.
- **Allow fences to be disabled** (off by default) — adds the full **Fence mode** switch. This is a separate high-risk opt-in because Home Assistant automations can then disable containment.

Changing either control option reloads the integration so the corresponding entities are added or removed. Commands for the integration entry are serialized, and entity actions force a non-debounced cloud refresh before revalidating options, snapshot-wide one-to-one mapping, and telemetry; UI availability alone is never trusted. Automatic redirects are disabled and a write is never replayed after an ambiguous HTTP/network result, including 401. Successes and post-dispatch failures both trigger a read-only reconciliation; the action returns successfully only if Halo reports the requested synchronized state, otherwise it directs you to verify in the official app. Fence-off additionally requires synchronized reported fence state and no active walk.

## Disclaimer & safety

- **Telemetry-only by default.** Fence controls exist only after explicit opt-in. Fence creation/editing/deletion, corrections, bind/unbind, and other control endpoints remain intentionally unsupported.
- **Fence state is safety-critical.** Treat Home Assistant controls and automations as supplemental conveniences, not a containment authority. Confirm important changes in the official Halo app and physically verify your pet is safe.
- Do not rely on this integration for your pet's containment or safety — the official Halo app and collar are the source of truth.
- Because it depends on an undocumented API, functionality may degrade without notice.

## Troubleshooting

- Restart Home Assistant after installing or updating through HACS.
- Complete the reauthentication flow if Home Assistant prompts for it.
- Compare important location and status values with the official Halo app.
- For bugs, enable debug logging and download redacted diagnostics from the integration page.

See [SUPPORT.md](SUPPORT.md) for support routes, first checks, and sensitive-data guidance.

## Project docs

- [Support](SUPPORT.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Release process](docs/RELEASING.md)
- [MIT License](LICENSE)
