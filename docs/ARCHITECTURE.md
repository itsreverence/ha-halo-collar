# Architecture

## Purpose

`ha-halo-collar` is a telemetry-first Home Assistant custom integration for Halo Collar cloud data, with narrowly scoped physical controls that are disabled by default. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

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
     -> fence mode: one PUT /pet/{id}/instant-mode with {modePatch: {fencesOn: bool}}
     -> Find collar: one bodyless PUT /collar/{id}/find; Return Whistle sounds/blinks for 10 seconds
     -> all physical commands serialized per config entry across option-triggered reloads
     -> redirects disabled; no write replay after an ambiguous HTTP/network result, including 401
     -> fresh preflight and dispatch-boundary revalidation for exact identity, options, and telemetry
     -> fence writes reconcile reported state; Find collar has no durable confirmation state and retains a cooldown after every dispatch
```

## Platforms

- `sensor`: battery, battery status, remaining battery lifetime, connection type (adapter), Wi-Fi/cellular status and signal, GPS accuracy, location status, safety status, firmware, last/next telemetry timestamps, daily/current-period activity goals and progress, current fence, fence configuration, average connectivity, cloud-polled active-walk duration/distance, latest matching completed walk among the ten most recent completed account walks (end time/duration/distance and locally derived average speed), and one account-level subscription summary. Walk history is optional enrichment; unknown means no valid matching record was present in that bounded window, not that no historical walk exists. It never exposes route, location, user, walk-ID, feedback, or correction fields and is not live tracking.
- `binary_sensor`: connectivity (staleness threshold configurable via options), fence breach, fence mode, fence synchronization, GPS calibration required, compass calibration required, active walk/paused state, diagnostic-reporting reliability, a fail-closed hardware issue summary with bounded allowlisted labels, and firmware update availability.
- `device_tracker`: pet/collar GPS tracker when Halo returns usable coordinates; pins the pet to `home` while the collar reports indoors on its configured Wi-Fi (GPS is unreliable indoors).
- `event`: fence breach event entity for automation triggers.
- `button`: fail-safe idempotent fence enable after the fence-control opt-in; separately opted-in Find collar physical Return Whistle after fresh identity/telemetry/entitlement checks and subject to a post-dispatch cooldown.
- `switch`: full fence mode including disable, available only after the separate high-risk opt-in.
- `diagnostics`: redacted config-entry diagnostics (tokens, serials, coordinates, names removed).

## Authentication

The config flow collects the user's Halo email and password and exchanges them for OAuth tokens via the password grant. The bundled `client_id`/`client_secret` are static, app-level credentials extracted from the official Halo app (shared by all installs, not user data); users can override them from the advanced fields if Halo rotates them.

Refreshed tokens and expiry-only renewals are written back to the config entry so they survive restarts. OAuth and API reads disable redirects. Successful token payloads are validated completely before the access token, refresh token, or expiry is changed, so malformed responses cannot partially rotate credentials. When the API reports an authentication failure, the coordinator raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow.

The repository must not contain real user access/refresh tokens, account credentials, serial-specific payloads, or real location data.

## Safety boundary

The default installation is telemetry-only. Fence controls and Find collar use separate explicit option tiers. Entity service actions share one domain-level config-entry transaction lock that survives option-triggered reloads and force non-debounced cloud refreshes before validating current options, subscription entitlement where required, snapshot-wide one-to-one relationship mapping, and telemetry rather than trusting UI availability or cached state. A command is issued at most once with automatic redirects disabled; after any token refresh and at every immediate transport-dispatch boundary, the client revalidates options, telemetry freshness, command-specific safety state, and the exact pet/collar IDs captured by preflight. Cancellation before that synchronous dispatch boundary cancels the inner task and sends nothing; cancellation after it waits for reconciliation before propagating. Concurrent token refreshes are serialized and double-checked so a rotated refresh token is used only once. The client never replays an ambiguous HTTP/network outcome, including 401.

Fence writes retain the transaction lock through read-only reconciliation and require fresh synchronized reported-state confirmation. Fence-off additionally requires no active walk. Find collar is Halo's physical Return Whistle: the collar blinks and plays a sound for 10 seconds, which Halo warns may confuse a pet wearing it. It requires its own opt-in, an enabled `findcollar` subscription feature, fresh uniquely mapped telemetry, no active or unknown walk, and a reload-stable per-collar cooldown started immediately before dispatch. The bodyless endpoint exposes no durable reported sound/light state, so read-only refresh cannot confirm physical execution; provider success is reported as command success, while 404 and ambiguous outcomes remain errors and are never retried.

Do not add corrections, fence geometry writes, other collar wake/control actions, bind/unbind, account mutation, or proprietary BLE walk-start behavior without a separate explicit review. Cloud pause/stop for an already active walk may be investigated later, but must account for the official app's local walk database and post-processing lifecycle.

## Error handling

`HaloAuthError` (subclass of `HaloApiError`) means credentials/tokens were
rejected and triggers reauth; everything transient (timeouts, connection
errors, 429s, 5xx — including 5xx from the token endpoint) is `HaloApiError`
and surfaces as a temporary `UpdateFailed` without prompting for credentials.
GET requests retry twice with a short backoff before giving up. Raised and logged
errors retain status/operation context but omit provider response bodies and
resource-bearing write paths so account data and pet/collar IDs are not echoed
into Home Assistant logs.

Coordinator failures mark telemetry and controls unavailable until a successful
refresh. Newly discovered collars trigger one config-entry reload so every
platform creates the new entity set. Removed collars remain in the entity
registry but become unavailable; restoring them reuses the same entity IDs.

## Privacy boundary

Config-entry diagnostics recursively redact generic provider IDs, names,
serials, coordinates, credentials, and account fields. Stable collar IDs and
the local display name remain in Home Assistant's private device/entity
registries because they provide durable identity and usable household labels;
those local registries and backups must be protected as household data and are
not release/support artifacts.

## Telemetry semantics

Activity values are provider daily/current-period readings. Location, current-fence, and active-walk state are cloud-polled supplemental telemetry, not real-time containment proof. The next-telemetry sensor is a timestamp calculated from the last report and provider interval; it is not a local countdown. Recent-walk average speed is calculated locally from the already-sanitized distance and duration and causes no additional Halo request. Hardware issue details are emitted only when diagnostics reporting is explicitly reliable and every allowlisted status is structurally valid; otherwise the entity is unknown.
