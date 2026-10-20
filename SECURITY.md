# Security policy

## Supported versions

Security fixes are applied to the latest release only.

## Reporting a vulnerability

Do not open a public issue containing Halo credentials, OAuth tokens, pet names, serial numbers, precise locations, fence data, private API payloads, Home Assistant secrets, or unredacted diagnostics.

Use [GitHub private vulnerability reporting](https://github.com/itsreverence/ha-halo-collar/security/advisories/new) for this repository. Do not include sensitive details in a public issue.

Include:

- Home Assistant and Halo Collar integration versions;
- a minimal description and reproduction;
- only the redacted logs or diagnostics needed to understand the issue.

## Sensitive data and credential response

The integration exchanges Halo account credentials for OAuth tokens and does not persist the password. Home Assistant stores tokens, expiry, normalized email, and OAuth client settings in its config entry. Treat Home Assistant backups, diagnostics, API captures, and logs as sensitive until reviewed.

If a password or token is exposed:

1. change the Halo password or revoke the affected session when possible;
2. reauthenticate the integration;
3. remove the sensitive material from public issues, screenshots, logs, and commits;
4. do not repost the raw credential while discussing the incident.

## Safety boundary

Halo Collar is **telemetry-only by default**. The only supported writes are opt-in fence controls and Find collar. Fence-off requires a stronger opt-in and rejects active or unknown walks. Fence changes require reported-state confirmation. Find collar requires entitlement, fresh exact mapping, no active or unknown walk, and a cooldown. Provider acceptance does not verify physical execution. See [control transactions](docs/ARCHITECTURE.md#control-transactions) for the full guarantees.

Fence creation, editing, and deletion; corrections; bind/unbind; account changes; arbitrary collar behavior; and other private write endpoints remain intentionally unsupported. Treat every Home Assistant control as a supplemental convenience, not a containment authority. Confirm safety-critical state in the official Halo app and physically verify your pet is safe.

The integration depends on an undocumented private cloud API and may stop working when Halo changes that service.
