# Architecture

## Purpose

`ha-halo-collar` is a telemetry-first Home Assistant custom integration for Halo Collar cloud data, with narrowly scoped fence controls that are disabled by default. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

## Runtime shape

```text
Home Assistant config entry (email + stored tokens; password is never persisted)
  -> HaloApiClient
     -> OAuth password grant on first login (auth.halocollar.com/connect/token)
     -> refresh the access token when needed
     -> GET /pet/my, /collar/my, /subscription/my, /system/server-date-time
     -> 30s request timeout; retries 429/5xx/timeouts with short backoff
  -> DataUpdateCoordinator polls every 300 seconds (60-3600s via options flow)
     -> persists refreshed tokens back to the config entry
     -> raises ConfigEntryAuthFailed to trigger reauth when credentials fail
  -> platforms expose telemetry entities plus disabled-by-default controls
     -> PUT /pet/{id}/instant-mode with {modePatch: {fencesOn: bool}}
     -> no blind write retries; immediate coordinator refresh after success
```

## Platforms

- `sensor`: battery, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi/cellular status and signal, GPS accuracy, location status, safety status, firmware, last telemetry timestamp.
- `binary_sensor`: connectivity (staleness threshold configurable via options), fence breach, fence mode, fence synchronization, GPS calibration required, compass calibration required.
- `device_tracker`: pet/collar GPS tracker when Halo returns usable coordinates; pins the pet to `home` while the collar reports indoors on its configured Wi-Fi (GPS is unreliable indoors).
- `event`: fence breach event entity for automation triggers.
- `button`: fail-safe idempotent fence enable, available only after the first control opt-in.
- `switch`: full fence mode including disable, available only after the separate high-risk opt-in.
- `diagnostics`: redacted config-entry diagnostics (tokens, serials, coordinates, names removed).

## Authentication

The config flow collects the user's Halo email and password and exchanges them for OAuth tokens via the password grant. The bundled `client_id`/`client_secret` are static, app-level credentials extracted from the official Halo app (shared by all installs, not user data); users can override them from the advanced fields if Halo rotates them.

Refreshed tokens are written back to the config entry so they survive restarts. When the API reports an authentication failure, the coordinator raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow.

The repository must not contain real user access/refresh tokens, account credentials, serial-specific payloads, or real location data.

## Safety boundary

The default installation is telemetry-only. The reviewed fence-mode endpoint is available only through two explicit option tiers: enable-only, then full on/off. Controls fail closed on stale/unknown state, fence-off is blocked during an active walk, writes are never blindly retried, and every successful command is followed by an immediate cloud refresh.

Do not add corrections, fence geometry writes, collar wake/control, bind/unbind, account mutation, or proprietary BLE walk-start behavior without a separate explicit review. Cloud pause/stop for an already active walk may be investigated later, but must account for the official app's local walk database and post-processing lifecycle.

## Error handling

`HaloAuthError` (subclass of `HaloApiError`) means credentials/tokens were
rejected and triggers reauth; everything transient (timeouts, connection
errors, 429s, 5xx — including 5xx from the token endpoint) is `HaloApiError`
and surfaces as a temporary `UpdateFailed` without prompting for credentials.
GET requests retry twice with a short backoff before giving up.

## Possible future work

- Add a `next telemetry` / stale-countdown sensor if a suitable field is found
  in the Halo payload.
