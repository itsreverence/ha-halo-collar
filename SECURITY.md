# Security Policy

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

The integration exchanges Halo account credentials for OAuth tokens and does not persist the password. Home Assistant stores the resulting tokens in its config entry. Treat Home Assistant backups, diagnostics, API captures, and logs as sensitive until reviewed.

If a password or token is exposed:

1. change the Halo password or revoke the affected session when possible;
2. reauthenticate the integration;
3. remove the sensitive material from public issues, screenshots, logs, and commits;
4. do not repost the raw credential while discussing the incident.

## Safety boundary

Halo Collar is intentionally read-only and must not modify fences, corrections, modes, collar behavior, or account/device binding. Its cloud telemetry and Home Assistant automations are supplemental. Do not rely on this integration for pet containment, emergency response, or safety decisions; use the official Halo app and collar as the source of truth.

The integration depends on an undocumented private cloud API and may stop working when Halo changes that service.
