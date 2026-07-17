# Halo Collar for Home Assistant

<p align="center">
  <img src="custom_components/halo_collar/brand/icon.png" alt="Halo Collar integration icon" width="128" height="128">
</p>

[![Latest release](https://img.shields.io/github/v/release/itsreverence/ha-halo-collar)](https://github.com/itsreverence/ha-halo-collar/releases)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml/badge.svg)](https://github.com/itsreverence/ha-halo-collar/actions/workflows/validate.yml)

A telemetry-first Home Assistant custom integration for your [Halo Collar](https://www.halocollar.com/) pets and collars: location, battery, connectivity, GPS status, safety/fence status, firmware, and narrowly guarded opt-in controls.

> [!IMPORTANT]
> This is an **unofficial** integration. It is not affiliated with, endorsed by, or supported by Halo Collar / Protect Animals With Satellites, LLC. It talks to Halo's private mobile/cloud API, which may change or break at any time. Use at your own risk.

## Features

The integration is **telemetry-only by default**. For each collar on your account it exposes:

- **Device tracker** — pet location when Halo reports GPS coordinates. When the collar reports it is **indoors on its configured Wi-Fi** (where GPS is unreliable), the tracker pins the pet to `home` instead of drifting on a jittery fix.
- **Sensors** — battery %, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi status/signal, cellular status/signal, GPS accuracy, location status, safety status, firmware version, last/next telemetry timestamps, current fence, fence configuration, average connectivity, daily/current-period activity, outdoor time, distance traveled, walks, cloud-polled active-walk duration/distance, the latest matching completed walk among the ten most recent completed account walks (end time, duration, distance, and locally derived average speed), and subscription limits. Walk history is supplemental, not live tracking; unknown means no valid matching record was present in that bounded window, not that no historical walk exists.
- **Binary sensors** — connectivity (online/stale), fence breach, fence mode/synchronization, GPS calibration required, compass calibration required, active walk/paused state, collar diagnostic reporting reliability, bounded hardware issue status/details, and firmware update availability.
- **Events** — a fence breach event entity you can use directly as an automation trigger.
- **Optional fence controls** — an idempotent **Enable fences** button, plus a separately opted-in **Fence mode** switch that can disable containment. Controls are unavailable on stale telemetry, fence-off is blocked during active walks, writes are not blindly retried, and state is refreshed after every command.
- **Optional Find collar control** — a separately opted-in manual **Find collar (sound and light)** button. It makes the collar blink and play Halo's Return Whistle for 10 seconds, which Halo warns may confuse a pet wearing it. It requires fresh uniquely mapped telemetry, an enabled subscription feature, no active or unknown walk, and a post-dispatch cooldown. It is never retried after an ambiguous outcome.
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
- **Enable Find collar sound and light** (off by default) — adds a manual button that makes the collar blink and play a sound for 10 seconds. Halo warns this may confuse a pet wearing it. The integration blocks active/unknown walks and repeat dispatches during cooldown, but cannot prove whether the collar is physically on your pet.
- **Allow fences to be disabled** (off by default) — adds the full **Fence mode** switch. This is a separate high-risk opt-in because Home Assistant automations can then disable containment.

Changing a control option reloads the integration so the corresponding entities become active or unavailable. Home Assistant can retain a revoked control in the entity registry as unavailable; an unavailable control cannot dispatch a command. Commands for the integration entry use a domain-level lock that survives option-triggered config-entry reloads, and entity actions force a non-debounced cloud refresh before revalidating options, subscription entitlement when required, snapshot-wide one-to-one mapping, and telemetry; UI availability alone is never trusted. Automatic redirects are disabled, options and the exact target are checked again inside the API client after any token refresh and immediately before the transport PUT, and a command is never replayed after an ambiguous HTTP/network result, including 401. Fence writes retain the transaction lock through read-only reconciliation and require reported-state confirmation. Find collar has no durable reported sound/light state, so it retains the lock through a refresh but treats provider success only as command success; failures and ambiguous outcomes remain errors and keep the cooldown.

## Disclaimer & safety

- **Telemetry-only by default.** Every physical control requires its own explicit opt-in. Fence creation/editing/deletion, corrections, bind/unbind, and other control endpoints remain intentionally unsupported.
- **Find collar is a physical command, not a connectivity test.** It blinks and sounds the collar for 10 seconds. Do not invoke it casually while your pet is wearing the collar, and do not automate repeated presses.
- **Fence state is safety-critical.** Treat Home Assistant controls and automations as supplemental conveniences, not a containment authority. Confirm important changes in the official Halo app and physically verify your pet is safe.
- Do not rely on this integration for your pet's containment or safety — the official Halo app and collar are the source of truth. Location, fence, and active-walk state are cloud-polled supplemental telemetry, not real-time containment proof; activity metrics are provider daily/current-period values.
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
