# Architecture

## Purpose

`ha-halo-collar` is a read-only Home Assistant custom integration for Halo Collar cloud telemetry. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

## Runtime shape

```text
Home Assistant config entry (email + password + stored tokens)
  -> HaloApiClient
     -> OAuth password grant on first login (auth.halocollar.com/connect/token)
     -> refresh the access token when needed
     -> GET /pet/my, /collar/my, /subscription/my, /system/server-date-time
  -> DataUpdateCoordinator polls every 300 seconds
     -> persists refreshed tokens back to the config entry
     -> raises ConfigEntryAuthFailed to trigger reauth when credentials fail
  -> platforms expose read-only entities
```

## Platforms

- `sensor`: battery, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi/cellular status and signal, GPS accuracy, location status, safety status, firmware.
- `binary_sensor`: connectivity, fence breach, GPS calibration required, compass calibration required.
- `device_tracker`: pet/collar GPS tracker when Halo returns usable coordinates.

## Authentication

The config flow collects the user's Halo email and password and exchanges them for OAuth tokens via the password grant. The bundled `client_id`/`client_secret` are static, app-level credentials extracted from the official Halo app (shared by all installs, not user data); users can override them from the advanced fields if Halo rotates them.

Refreshed tokens are written back to the config entry so they survive restarts. When the API reports an authentication failure, the coordinator raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow.

The repository must not contain real user access/refresh tokens, account credentials, serial-specific payloads, or real location data.

## Safety boundary

v1 is telemetry-only. Do not add correction, fence modification, collar wake/control, bind/unbind, or any other write/control endpoint without a separate explicit review.

## Possible future work

- Add telemetry freshness sensors (`last telemetry`, `next telemetry`, stale status).
- Improve tracker fallback when Halo reports an indoor/non-GPS location without lat/lon.
- Options flow for poll interval.
