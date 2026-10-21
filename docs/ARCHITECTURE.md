# Architecture

## Purpose

`ha-halo-collar` is a telemetry-first Home Assistant custom integration for Halo Collar cloud data, with narrowly scoped physical controls that are disabled by default. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

## Runtime shape

```text
Home Assistant config entry (email, tokens, expiry, OAuth client settings; no password)
  -> HaloApiClient
     -> OAuth password grant on first login (auth.halocollar.com/connect/token)
     -> serialize access-token refresh within each client
     -> synchronously submit validated token rotations to the config entry before continuing
     -> required GET /pet/my, /collar/my, /subscription/my
     -> optional GET /system/server-date-time, then /walk/my?page=1&pageSize=30
     -> 30s request timeout; GET retries selected transient statuses and request failures
  -> DataUpdateCoordinator polls every 300 seconds (60-3600s via options flow)
     -> raises ConfigEntryAuthFailed to trigger reauth when credentials fail
  -> platforms expose telemetry entities plus disabled-by-default controls
     -> fence mode: one PUT /pet/{id}/instant-mode with {modePatch: {fencesOn: bool}}
     -> Find collar: one bodyless PUT /collar/{id}/find; requests sound and light
     -> all physical commands serialized per config entry across option-triggered reloads
     -> redirects disabled; no write replay after an ambiguous HTTP/network result, including 401
     -> fresh preflight and dispatch-boundary revalidation for exact identity, options, and telemetry
     -> fence writes reconcile reported state; Find collar has no durable confirmation state and retains a cooldown after every dispatch
```

## Platforms

- `sensor`: battery, battery status, remaining battery lifetime, connection type, Wi-Fi and cellular status and signal, GPS accuracy, location status, safety status, firmware, telemetry timestamps, activity metrics, current fence, fence configuration, average connectivity, active-walk metrics, recent-walk metrics, and an account-level subscription summary.
- `binary_sensor`: connectivity (staleness threshold configurable via options), fence breach, fence mode, fence synchronization, GPS calibration required, compass calibration required, active walk/paused state, diagnostic-reporting reliability, a fail-closed hardware issue summary with bounded allowlisted labels, and firmware update availability.
- `device_tracker`: provider GPS coordinates with an indoor Home state override. When `gpsAccuracyStatus` is `indoors` and `currentAdapter` is `wifi`, the tracker forces Home zone membership through `in_zones`, or `location_name` on older Home Assistant versions. These indicators do not verify the SSID or physical home location. Latitude and longitude remain the provider coordinates.
- `event`: fence breach event entity for automation triggers.
- `button`: fail-safe idempotent fence enable after the fence-control opt-in; separately opted-in Find collar physical Return Whistle after fresh identity/telemetry/entitlement checks and subject to a post-dispatch cooldown.
- `switch`: full fence mode including disable, available only after the separate high-risk opt-in.
- `diagnostics`: redacted config-entry diagnostics (tokens, serials, coordinates, names removed).

Opted-in actions also add enum status sensors in the diagnostic category. A pure classifier derives advisory reasons from cached state, current options, the shared freshness cap, and the existing cooldown map. Status sensors remain available through cloud and mapping failures. Their values never authorize dispatch or confirm physical execution.

The Find status sensor owns one local expiry timer, independently of the button. An entry-and-collar-scoped dispatcher notification reaches the current sensor when an older button transaction finishes after a reload. Timer expiry and status evaluation make no Halo requests. Report-age changes appear on the next state update rather than through a dedicated expiry timer.

### Recent-walk history

The integration requests `/walk/my?page=1&pageSize=30` and retains at most thirty returned records, the Halo app's own page size. One request per poll reduces crowding on multi-dog accounts without paging. An unordered response, or 30 walks from one dog, can still leave another dog's latest walk out. It selects the newest matching completed walk by valid `endedAt`, then exposes end time, duration, distance, and locally derived average speed. The request has no completed-only filter and the integration does not guarantee provider ordering.

History is optional enrichment, not live tracking. Unknown can mean no valid matching record in that window or an optional history timeout or error. It does not prove that no historical walk exists. Recent-walk entities never expose route, location, user, walk-ID, feedback, or correction fields.

### Last reported activity

The sensor reads only `pet.telemetry.activityType` through the existing exact pet relationship. Known integer codes map to stable translated enum states. Missing, malformed, zero, and unknown codes produce unknown. Booleans, floats, and strings are rejected. Cached known classifications remain visible when collar reports age. A failed required refresh makes telemetry unavailable. No extra requests or timers are added.

The code map comes from the Halo app. A live cloud response confirmed the field path, its integer type, and the resting code. The other codes are not yet observed live.

## Authentication

The config flow collects the user's Halo email and password and exchanges them for OAuth tokens via the password grant. The bundled `client_id`/`client_secret` are static, app-level credentials extracted from the official Halo app (shared by all installs, not user data); users can override them in the advanced fields during initial setup. Reauthentication uses the saved OAuth settings and exposes only email and password. Options cannot change OAuth settings. See [account recovery](../SUPPORT.md#recover-authentication) for existing-entry precautions.

### Token persistence

Refreshed tokens and expiry-only renewals are written back to the config entry so they survive restarts. OAuth and API reads disable redirects. Successful token payloads are validated completely before the access token, refresh token, or expiry is changed, so malformed responses cannot partially rotate credentials. When the API reports an authentication failure, the coordinator raises `ConfigEntryAuthFailed`, which starts Home Assistant's reauth flow.

The client submits each validated rotation to Home Assistant's config-entry storage mechanism before another await. Later polling failures, cancellation, and command vetoes therefore do not discard that update. This schedules storage through Home Assistant; it does not guarantee a disk flush. The historical `expires_at` field stores the refresh deadline, already 60 seconds before provider expiry. The refresh check compares that deadline directly. OAuth 429 responses are temporary failures, classified before reading their bodies, and are not retried.

Each setup client has a private token writer seeded from its starting access token, refresh token, and expiry. Before saving a rotation, the writer synchronously checks that the same entry is still registered and its saved credential triple matches the writer's last saved triple. A mismatch permanently retires that writer. Successful unload and incomplete or cancelled setup also retire it. A failed unload alone retains persistence authority. Already committed rotations survive setup failure and cancellation.

If reauth returns an identical credential triple, a rotation arriving before successful unload may still be saved. Replacement setup then starts from that saved triple. After retirement, the old callback cannot save. After reauth changes the credentials and unload fails, the retained old runtime cannot overwrite the accepted credentials. Home Assistant still owns recovery from that failed unload. These checks make no claim about provider token validity.

The repository must not contain real user access/refresh tokens, account credentials, serial-specific payloads, or real location data.

### Same-account reauthentication

1. Normalize the saved email by trimming whitespace and lowercasing. Any stored unique ID must agree. Legacy entries without a unique ID use the saved email without backfilling the ID.
2. Abort before login if saved identity is missing or conflicting. Reject a different submitted account with an email field error before constructing a client.
3. Resolve the live entry at submission, then recheck saved identity and effective OAuth settings after login.
4. Merge validated credential fields into current entry data, preserving unrelated concurrent data and options. Runtime token persistence separately checks saved credential lineage and client lifetime.

Successful reauth updates the entry, requests a reload, and returns its successful
abort before reload dispatch. It uses public APIs available in Home Assistant
2024.12, including when credentials are unchanged or no runtime exists. It does
not use the deprecated flow helper that combines an update listener with reload.

### Reload ownership

Reauth, control-option changes, and new-collar discovery share one transient
reload owner per entry. Queued requests coalesce. Setup absorbs outstanding
requests immediately before its credential snapshot. Requests after that snapshot
can require another pass. There is at most one pending intent, not a fixed limit
on passes when requests continue. The worker belongs to Home Assistant so unloading
the entry cannot cancel its own reload. The owner cancels scheduled setup retries and
checks that the same entry is registered before accepting work and before dispatch.

Token renewals, polling intervals, and staleness options continue to apply in
place. Each runtime requests an inventory reload only once, including after a
failed unload. Failed unload retains the runtime, token writer, control lock, and
cooldowns. The reload owner clears its transient bookkeeping on exit, but does
not make Home Assistant's nonrecoverable `FAILED_UNLOAD` state recoverable.

## Control transactions

The default installation is telemetry-only. Fence controls and Find collar require separate explicit option tiers. Entity availability alone never authorizes a command.

1. Acquire the domain-level config-entry transaction lock, which survives option-triggered reloads.
2. Force a non-debounced cloud refresh. Validate current options, required entitlement, snapshot-wide one-to-one pet/collar mapping, and telemetry. Controls use the shorter of **Collar report age limit** and 900 seconds.
3. After any token refresh and immediately before transport dispatch, revalidate options, telemetry freshness, command-specific safety state, and the exact pet/collar IDs captured by preflight. Concurrent token refreshes are serialized and double-checked.
4. Dispatch at most one request with redirects disabled. Never replay an ambiguous HTTP or network outcome, including 401.
5. Retain the lock through read-only post-dispatch reconciliation. Cancellation before the synchronous dispatch boundary cancels the inner task and sends nothing. Cancellation after dispatch waits for reconciliation before propagating.

Fence changes require fresh synchronized reported-state confirmation. Fence-off additionally requires both fence-control opt-ins and rejects active or unknown walks.

Find collar requests the physical Return Whistle sound and light command. It may confuse a pet wearing the collar. It requires its own opt-in, enabled `findcollar` entitlement, fresh exact mapping, and no active or unknown walk. A reload-stable per-collar **60-second cooldown** starts immediately before every attempted dispatch, including failed or ambiguous outcomes. The endpoint exposes no durable sound/light confirmation state. An unambiguous provider success means command acceptance, not verified physical execution. A 404 or ambiguous result remains an error and is never retried.

Each PUT owns a fresh session and connector that refuses a second connection lease. This blocks aiohttp's disconnected-PUT replay path on the dependency versions covered by the socket tests. Write sessions preserve Home Assistant's verified TLS context, default headers, and `trust_env` policy, but use the system threaded resolver instead of Home Assistant's shared DNS cache and connection pool. Resource preparation precedes the dispatch callback; all write resources close with the request. The shared telemetry session is unchanged.

Do not add corrections, fence geometry writes, other collar wake/control actions, bind/unbind, account mutation, or proprietary BLE walk-start behavior without a separate explicit review.

## Error handling

`HaloAuthError`, a subclass of `HaloApiError`, means credentials or tokens were rejected and triggers reauthentication. Transient failures, including token-endpoint 5xx, raise `HaloApiError` and become temporary `UpdateFailed` errors without prompting for credentials.

Each GET retry loop allows two retries after the first attempt, with 1- and 3-second backoffs. It retries HTTP 429, 500, 502, 503, and 504, timeouts, `aiohttp.ClientError`, and invalid JSON (`ValueError`). Optional-read budgets can end the loop earlier. Raised and logged errors retain status and operation context but omit provider response bodies and resource-bearing write paths so account data and pet/collar IDs are not echoed into Home Assistant logs.

Required pets, collars, and subscription responses are fetched and validated before
optional reads. Server time is diagnostic-only and has a five-second total budget,
including token refresh, GET retries, and response-body reads. A timeout or any
non-authentication `HaloApiError`, including permanent HTTP errors and malformed
JSON, leaves `server_time` as `None` and allows walk enrichment to continue. Successful
strings are retained; other successful payload types also become `None`.
Authentication failures, external cancellation, and unexpected programming errors
propagate. Cancellation during a response-body read releases the response.
Walk history retains its separate five-second optional budget. Downloading
diagnostics reads the cached coordinator snapshot and makes no network request.

Coordinator failures mark telemetry and controls unavailable until a successful
refresh. The provider boundary rejects pet/collar lists with missing, non-string,
or duplicate IDs before platforms construct entities. Selected optional mapping
envelopes are copied and normalized to empty mappings when malformed. These are
pet `collarInfo`, `metrics`, and `telemetry`; collar `diagnostics`, `firmware`,
`issues`, and `telemetry`; and collar `petInfo` with its `lastLocation`, `location`,
and `telemetry` mappings. This prevents mapping-access errors at those boundaries.
The resulting value depends on the extractor's existing fallback semantics;
an empty mapping does not universally mean unknown telemetry. Newly discovered collars trigger one
config-entry reload so every platform creates the new entity set. Removed collars
remain in the entity registry but become unavailable; restoring them reuses the
same entity IDs.

## Privacy boundary

Config-entry diagnostics recursively redact generic provider IDs, names,
serials, hardware UUIDs, coordinates, credentials, and account fields. Stable collar IDs and
the local display name remain in Home Assistant's private device/entity
registries because they provide durable identity and usable household labels;
those local registries and backups must be protected as household data and are
not release/support artifacts.

## Telemetry semantics

GPS and compass calibration flags report on or off only for actual provider booleans. Missing, null, and other values report unknown.

Connectivity evaluates the report timestamp when Home Assistant reads or publishes state. Old, missing, invalid, or excessively future-dated timestamps report off. There is no dedicated expiry timer, and a successful poll can leave old report data available. Missing optional values can be unknown without failing the refresh.

Activity values are provider daily/current-period readings. Location, current-fence, and active-walk state are cloud-polled supplemental telemetry, not real-time containment proof. The next-telemetry sensor is a timestamp calculated from the last report and provider interval; it is not a local countdown. Recent-walk average speed is calculated locally from the already-sanitized distance and duration and causes no additional Halo request. Hardware issue details are emitted only when diagnostics reporting is explicitly reliable and every allowlisted status is structurally valid; otherwise the entity is unknown.
