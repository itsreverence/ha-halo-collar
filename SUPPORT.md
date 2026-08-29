# Support

## First checks

1. Restart Home Assistant after installing or updating the integration.
2. Confirm Halo Collar appears under **Settings → Devices & services**.
3. If Halo reports that the provided client version is unsupported, update the integration before retrying setup. The integration can adopt a strictly validated newer minimum for one safe read retry, but an update may still be needed if Halo changes authentication or its API contract.
4. Compare important telemetry with the official Halo app.
5. If authentication expired, complete the Home Assistant reauthentication flow.
6. Enable debug logging and download diagnostics from the integration page when needed.

## Bugs and feature requests

Use the repository's [issue forms](https://github.com/itsreverence/ha-halo-collar/issues/new/choose). Include Home Assistant and integration versions, reproduction steps, and only relevant redacted logs or diagnostics.

Never post Halo passwords, OAuth tokens, pet names, serial numbers, precise locations, fence data, account identifiers, or raw private-API captures.

## Security and privacy issues

Do not report vulnerabilities publicly. Follow [SECURITY.md](SECURITY.md) for private reporting and credential-response guidance.

## Vendor and safety support

This project is unofficial and cannot provide Halo account, billing, collar hardware, containment, or emergency support. Use the official Halo app and Halo support for those issues. Home Assistant telemetry and automations are supplemental and must not be treated as a pet-safety system.
