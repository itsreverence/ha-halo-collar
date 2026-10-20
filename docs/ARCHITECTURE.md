# Architecture

## Purpose

`ha-halo-collar` is a telemetry-first Home Assistant custom integration for Halo Collar cloud data, with narrowly scoped physical controls that are disabled by default. It is built on Halo's unofficial mobile/cloud API and is not affiliated with or supported by the vendor.

## Runtime shape

```text
Home Assistant config entry (email + stored tokens; password is never persisted)
  -> HaloApiClient
     -> OAuth password grant on first login (auth.halocollar.com/connect/token)
     -> serialize access-token refresh within each client
     -> synchronously submit validated token rotations to the config entry before continuing
     -> required GET /pet/my, /collar/my, /subscription/my
     -> optional GET /system/server-date-time, then /walk/my?page=1&pageSize=10
     -> 30s request timeout; GET retries 429/5xx/timeouts with short backoff
  -> DataUpdateCoordinator polls every 300 seconds (60-3600s via options flow)
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

The client submits each validated rotation to Home Assistant's config-entry storage mechanism before another await. Later polling failures, cancellation, and command vetoes therefore do not discard that update. This schedules storage through Home Assistant; it does not guarantee a disk flush. The historical `expires_at` field stores the refresh deadline, already 60 seconds before provider expiry. The refresh check compares that deadline directly. OAuth 429 responses are temporary failures, classified before reading their bodies, and are not retried.

Each setup client has a private token writer seeded from its starting access token, refresh token, and expiry. Before saving a rotation, the writer synchronously checks that the same entry is still registered and its saved credential triple matches the writer's last saved triple. A mismatch permanently retires that writer. Successful unload and incomplete or cancelled setup also retire it. A failed unload alone retains persistence authority. Already committed rotations survive setup failure and cancellation.

If reauth returns an identical credential triple, a rotation arriving before successful unload may still be saved. Replacement setup then starts from that saved triple. After retirement, the old callback cannot save. After reauth changes the credentials and unload fails, the retained old runtime cannot overwrite the accepted credentials. Home Assistant still owns recovery from that failed unload. These checks make no claim about provider token validity.

The repository must not contain real user access/refresh tokens, account credentials, serial-specific payloads, or real location data.

Reauthentication binds to the saved email after trimming whitespace and lowercasing. A stored unique ID must agree with that email. Legacy entries without a unique ID use the saved email without backfilling the ID. Missing or conflicting saved identity aborts before login; a different submitted account returns an email field error before constructing a client. The flow resolves the live entry at submission and checks the saved identity and effective OAuth settings again after login. It merges only validated credential fields into current entry data, preserving unrelated concurrent data and options. This check protects the flow's update boundary. Runtime token persistence separately checks the saved credential lineage and client lifetime.

Successful reauth updates the entry, requests a reload, and returns its successful
abort before reload dispatch. It uses public APIs available in Home Assistant
2024.12, including when credentials are unchanged or no runtime exists. It does
not use the deprecated flow helper that combines an update listener with reload.

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

## Safety boundary

The default installation is telemetry-only. Fence controls and Find collar use separate explicit option tiers. Entity service actions share one domain-level config-entry transaction lock that survives option-triggered reloads and force non-debounced cloud refreshes before validating current options, subscription entitlement where required, snapshot-wide one-to-one relationship mapping, and telemetry rather than trusting UI availability or cached state. A command is issued at most once with automatic redirects disabled; after any token refresh and at every immediate transport-dispatch boundary, the client revalidates options, telemetry freshness, command-specific safety state, and the exact pet/collar IDs captured by preflight. Cancellation before that synchronous dispatch boundary cancels the inner task and sends nothing; cancellation after it waits for reconciliation before propagating. Concurrent token refreshes are serialized and double-checked so a rotated refresh token is used only once. The client never replays an ambiguous HTTP/network outcome, including 401.

Fence writes retain the transaction lock through read-only reconciliation and require fresh synchronized reported-state confirmation. Fence-off additionally requires no active walk. Find collar is Halo's physical Return Whistle: the collar blinks and plays a sound for 10 seconds, which Halo warns may confuse a pet wearing it. It requires its own opt-in, an enabled `findcollar` subscription feature, fresh uniquely mapped telemetry, no active or unknown walk, and a reload-stable per-collar cooldown started immediately before dispatch. The bodyless endpoint exposes no durable reported sound/light state, so read-only refresh cannot confirm physical execution; provider success is reported as command success, while 404 and ambiguous outcomes remain errors and are never retried.

Each PUT owns a fresh session and connector that refuses a second connection lease. This blocks aiohttp's disconnected-PUT replay path on the dependency versions covered by the socket tests. Write sessions preserve Home Assistant's verified TLS context, default headers, and `trust_env` policy, but use the system threaded resolver instead of Home Assistant's shared DNS cache and connection pool. Resource preparation precedes the dispatch callback; all write resources close with the request. The shared telemetry session is unchanged.

Do not add corrections, fence geometry writes, other collar wake/control actions, bind/unbind, account mutation, or proprietary BLE walk-start behavior without a separate explicit review.

## Error handling

`HaloAuthError` (subclass of `HaloApiError`) means credentials/tokens were
rejected and triggers reauth; everything transient (timeouts, connection
errors, 429s, 5xx — including 5xx from the token endpoint) is `HaloApiError`
and surfaces as a temporary `UpdateFailed` without prompting for credentials.
GET requests retry twice with a short backoff before giving up. Raised and logged
errors retain status/operation context but omit provider response bodies and
resource-bearing write paths so account data and pet/collar IDs are not echoed
into Home Assistant logs.

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
serials, coordinates, credentials, and account fields. Stable collar IDs and
the local display name remain in Home Assistant's private device/entity
registries because they provide durable identity and usable household labels;
those local registries and backups must be protected as household data and are
not release/support artifacts.

## Telemetry semantics

GPS and compass calibration flags report on or off only for actual provider booleans. Missing, null, and other values report unknown.

Activity values are provider daily/current-period readings. Location, current-fence, and active-walk state are cloud-polled supplemental telemetry, not real-time containment proof. The next-telemetry sensor is a timestamp calculated from the last report and provider interval; it is not a local countdown. Recent-walk average speed is calculated locally from the already-sanitized distance and duration and causes no additional Halo request. Hardware issue details are emitted only when diagnostics reporting is explicitly reliable and every allowlisted status is structurally valid; otherwise the entity is unknown.
