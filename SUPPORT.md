# Support

## First checks

1. Restart Home Assistant after installing or updating the integration.
2. Confirm Halo Collar appears under **Settings → Devices & services**.
3. If Halo reports that the provided client version is unsupported, update the integration before retrying setup. The integration can adopt a strictly validated newer minimum for one safe read retry, but an update may still be needed if Halo changes authentication or its API contract.
4. Compare important telemetry with the official Halo app.
5. If authentication expired, complete the Home Assistant reauthentication flow.
6. If the problem continues, [collect logs and diagnostics](#collect-logs-and-diagnostics).

## Telemetry stale, unknown, or unavailable

| What you see | What to check and do |
| --- | --- |
| Values stop changing but remain available | Check **Last telemetry** and compare with the official Halo app. A successful cloud poll can return old report data. Reducing **Update interval** does not request a new collar report. |
| **Connectivity** is off | The report timestamp is old, missing, invalid, or too far in the future. Check **Last telemetry** and the official app. Connectivity reflects report age at the next state update, not a live connection test. |
| One sensor is **unknown** | Halo may omit that optional value or return an invalid value. Check related telemetry. Unknown does not mean safe, off, or zero. |
| Recent-walk sensors are **unknown** | No valid matching completed walk may exist in the 30 most recent records returned by Halo, or the optional history request failed or timed out. This does not prove that no historical walk exists. |
| Telemetry entities are **unavailable** | Check the integration's setup, authentication, or refresh error. Required refresh failures make telemetry unavailable until a successful refresh. A removed collar also becomes unavailable. Advisory control-status sensors remain readable. Reauthenticate only when prompted. |
| The tracker reports `home` but its map position differs | Halo's indoor and Wi-Fi indicators override the Home state. The integration does not verify the network or physical home location and keeps Halo's coordinates. Compare with the official app. |

## Recover authentication

Reauthentication renews credentials for the account already saved in this integration. Email comparison ignores surrounding spaces and capitalization. Use the saved account's email and current Halo password. A different email is rejected before sign-in. To use a different account, add a separate Halo Collar integration.

If Home Assistant reports that the account or OAuth settings changed during sign-in, restart reauthentication from the current integration. If the saved identity is missing or inconsistent, reauthentication stops without changing credentials. It does not repair, migrate, or delete the entry. Check that you selected the intended integration and consult the [support routes](#bugs-and-feature-requests) if you need help.

Reauthentication reuses the saved OAuth client ID and secret. Neither reauthentication nor **Configure** can replace them. Updating the integration does not replace settings already saved in an entry. If an existing entry needs different OAuth settings, use the [support routes](#bugs-and-feature-requests) before choosing remove-and-add recovery.

If you choose to remove and add the integration again, first back up Home Assistant and record the affected entities, automations, dashboards, and integration options. Removal can affect device and entity registry associations. After adding the intended account, check entity references and repair affected automations and dashboards. A new entry starts telemetry-only, so review each control opt-in again. Do not edit stored tokens or identity fields to bypass the account check.

## Control missing, unavailable, or rejected

Start with the integration status, the named telemetry entities, and any error shown for the action. Control availability can change after a fresh cloud read. An available button or switch does not guarantee that its action will pass the dispatch checks.

Open the collar's device page and scroll to **Diagnostic**. Each opted-in action has a status sensor that explains a cached blocker. **Ready to try** is advisory. Report age and other state can change before the next update, and the action always validates fresh state. The disable-fences status can show a walk blocker even while the switch remains available for turning fences on.

At the maximum one-hour update interval, outdated readiness can remain visible until the next hourly poll. The action still rejects stale reports immediately when you try it.

| What you see | What to check and do |
| --- | --- |
| A control is missing | Check **Configure** on the integration. **Enable fail-safe fence controls** adds **Enable fences**. **Allow fences to be disabled** also requires fence controls and adds **Fence mode**. **Enable Find collar sound and light** separately adds **Find collar**. Only opt in to controls you intend to use. |
| Controls or their status sensors remain in the registry but are unavailable after an option change | Disabling an opt-in reloads the integration. Home Assistant can retain the old entities as unavailable. Review any automation that still references them. |
| Telemetry and controls are unavailable | Check the integration for setup, authentication, or cloud errors. Complete [same-account reauthentication](#recover-authentication) when prompted. For connection failures, wait for a successful refresh and compare telemetry with the official Halo app. |
| Connectivity is on but a control is unavailable or rejects stale state | Check **Last telemetry**. Controls use the shorter of **Collar report age limit** and 900 seconds. Increasing the connectivity threshold cannot extend that cap. Wait for fresh collar telemetry. A cloud poll does not force a collar report. |
| An action reports missing or changed pet/collar mapping | Check the assigned collar in the official Halo app and wait for consistent cloud telemetry. Commands require one exact pet/collar mapping and recheck it immediately before dispatch. |
| **Fence mode** is unavailable, fence-off is rejected, or a fence change cannot be confirmed | Check **Fences enabled**, **Fence state synchronized**, and **Active walk**. The switch requires known reported fence mode and synchronized state. Fence-off also requires both control opt-ins and no active or unknown walk. Fence changes require fresh reported-state confirmation. An unconfirmed command is not proof that containment changed; check the official app and your pet's safety before deciding what to do next. |
| **Find collar** is unavailable or rejected | Check freshness, mapping, and **Active walk**. Active or unknown walks block Find collar. Halo must also report the enabled `findcollar` subscription feature. The **Subscription** summary does not expose this entitlement; check the official app or Halo support for account access. |
| **Find collar** is unavailable after an attempt | Wait for the 60-second cooldown after every attempted dispatch, including a failed or ambiguous result. Option-triggered reloads retain the cooldown. Provider success means command acceptance, not verified sound or light at the collar. Do not automate repeated attempts. |

Use telemetry and the official app to investigate connectivity. Physical commands are not connectivity tests. Do not change options to bypass safety checks. If the cause remains unclear, collect redacted diagnostics and the action's error for a support report. Downloading diagnostics uses cached state and makes no Halo request.

## Collect logs and diagnostics

1. Open **Settings > Devices & services > Halo Collar**.
2. In the integration menu, select **Enable debug logging**.
3. Reproduce the telemetry problem, or wait for the next scheduled refresh. Do not use a physical command as a connectivity test.
4. Select **Disable debug logging** to stop logging and download the log file.
5. Open the affected integration entry's menu and select **Download diagnostics**. This uses cached state and makes no Halo request.
6. Review both files before sharing. Diagnostics redact known sensitive fields, but automatic redaction is not a privacy audit. Remove any remaining credentials, names, serials, locations, fences, and account identifiers.

If the menus or diagnostics are unavailable during setup failure, include the error shown on the integration page and relevant logs from **Settings > System > Logs**.

## Bugs and feature requests

Use the repository's [issue forms](https://github.com/itsreverence/ha-halo-collar/issues/new/choose). Include Home Assistant and integration versions, reproduction steps, and only relevant redacted logs or diagnostics.

Never post Halo passwords, OAuth tokens, pet names, serial numbers, precise locations, fence data, account identifiers, or raw private-API captures.

## Security and privacy issues

Do not report vulnerabilities publicly. Follow [SECURITY.md](SECURITY.md) for private reporting and credential-response guidance.

## Vendor and safety support

This project is unofficial and cannot provide Halo account, billing, collar hardware, containment, or emergency support. Use the official Halo app and Halo support for those issues. Home Assistant telemetry and automations are supplemental and must not be treated as a pet-safety system.
