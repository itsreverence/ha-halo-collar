# Architecture

## Purpose

`ha-halo-collar` is a telemetry-first Home Assistant custom integration for Halo Collar cloud data, with narrowly scoped fence controls that are disabled by default. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

## Runtime shape

```text
Home Assistant config entry (email + stored tokens; password is never persisted)
  -> HaloApiClient
     -> OAuth password grant on first login (auth.halocollar.com/connect/token)
     -> serialize access-token refresh so concurrent reads/writes cannot reuse a rotated refresh token
     -> GET /pet/my, /collar/my, /subscription/my, /system/server-date-time; optional GET /walk/my?page=1&pageSize=10
     -> 30s request timeout; retries 429/5xx/timeouts with short backoff
  -> DataUpdateCoordinator polls every 300 seconds (60-3600s via options flow)
     -> persists refreshed tokens back to the config entry
     -> raises ConfigEntryAuthFailed to trigger reauth when credentials fail
  -> platforms expose telemetry entities plus disabled-by-default controls
     -> serialized per config entry across option-triggered reloads; one PUT /pet/{id}/instant-mode with {modePatch: {fencesOn: bool}}
     -> redirects disabled; no write replay after an ambiguous HTTP/network result, including 401
     -> fresh preflight plus read-only reconciliation and reported-state confirmation on every dispatched outcome
```

## Platforms

- `sensor`: battery, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi/cellular status and signal, GPS accuracy, location status, safety status, firmware, last/next telemetry timestamps, daily/current-period activity goals and progress, current fence, fence configuration, average connectivity, cloud-polled active-walk duration/distance, latest matching completed walk among the ten most recent completed account walks (end time/duration/distance), and one account-level subscription summary. Walk history is optional enrichment; unknown means no valid matching record was present in that bounded window, not that no historical walk exists. It never exposes route, location, user, walk-ID, feedback, or correction fields and is not live tracking.
- `binary_sensor`: connectivity (staleness threshold configurable via options), fence breach, fence mode, fence synchronization, GPS calibration required, compass calibration required, active walk/paused state, collar reporting issue, and firmware update availability.
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

The default installation is telemetry-only. The reviewed fence-mode endpoint is available only through two explicit option tiers: enable-only, then full on/off. Entity service actions share one domain-level config-entry transaction lock that survives option-triggered reloads and force non-debounced cloud refreshes before validating the current options, snapshot-wide one-to-one relationship mapping, and telemetry rather than trusting UI availability or cached state. A write is issued at most once with automatic redirects disabled; after any token refresh and at every immediate transport-dispatch boundary, the client revalidates options, telemetry freshness, disable-specific safety state, and the exact pet/collar IDs captured by preflight. Concurrent token refreshes are serialized and double-checked so a rotated refresh token is used only once. The client never replays an ambiguous HTTP/network outcome (including 401). Successful responses, post-dispatch failures, and caller cancellation all retain the transaction lock through read-only reconciliation; confirmation must still match the original pet/collar IDs, synchronized reported state, and fresh telemetry. Fence-off additionally requires synchronized reported mode and no active walk; rejected or unconfirmed transitions surface an error directing the user to the official app.

Do not add corrections, fence geometry writes, collar wake/control, bind/unbind, account mutation, or proprietary BLE walk-start behavior without a separate explicit review. Cloud pause/stop for an already active walk may be investigated later, but must account for the official app's local walk database and post-processing lifecycle.

## Error handling

`HaloAuthError` (subclass of `HaloApiError`) means credentials/tokens were
rejected and triggers reauth; everything transient (timeouts, connection
errors, 429s, 5xx — including 5xx from the token endpoint) is `HaloApiError`
and surfaces as a temporary `UpdateFailed` without prompting for credentials.
GET requests retry twice with a short backoff before giving up.

## Telemetry semantics

Activity values are provider daily/current-period readings. Location, current-fence, and active-walk state are cloud-polled supplemental telemetry, not real-time containment proof. The next-telemetry sensor is a timestamp calculated from the last report and provider interval; it is not a local countdown.
