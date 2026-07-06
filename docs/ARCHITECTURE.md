# Architecture

## Purpose

`ha-halo-collar` is a read-only Home Assistant custom integration for Halo Collar cloud telemetry. It is currently a private prototype around Halo's unofficial mobile/cloud API.

## Runtime shape

```text
Home Assistant config entry
  -> HaloApiClient
     -> refresh access token when needed
     -> GET /pet/my, /collar/my, /subscription/my, /system/server-date-time
  -> DataUpdateCoordinator polls every 300 seconds
  -> platforms expose read-only entities
```

## Platforms

- `sensor`: battery, battery status, runtime, adapter, Wi-Fi/cellular status and signal, GPS accuracy, location status, safety status, firmware.
- `binary_sensor`: connectivity, fence breach, GPS calibration required, compass calibration required.
- `device_tracker`: pet/collar GPS tracker when Halo returns usable coordinates.

## Safety boundary

v1 is telemetry-only. Do not add correction, fence modification, collar wake/control, bind/unbind, or any other write/control endpoint without a separate explicit review.

## Auth state

The private prototype can read a token bundle from `/config/.halo_collar_token.json` to avoid pasting large token JSON into Home Assistant. This is a local testing convenience, not the final public UX.

The repo must not contain user access tokens, refresh tokens, serial-specific payloads, or real location data.

## Known gaps before public/HACS quality

- Replace or clearly isolate the local token-file shortcut.
- Improve auth/reauth UX for normal users.
- Add telemetry freshness sensors (`last telemetry`, `next telemetry`, stale status).
- Improve tracker fallback when Halo reports `Indoors` without lat/lon.
- Add CI, HACS/Hassfest checks, security policy, and user-facing docs.
