# Releasing

This checklist is for maintainers.

## Prepare

1. Update the `X.Y.Z` versions in `custom_components/halo_collar/manifest.json` and `pyproject.toml`.
2. Confirm the planned Git tag and release use `vX.Y.Z`.
3. Review README, support, security, and compatibility guidance.
4. Run:

   ```bash
   uv run pytest -q
   uv run ruff check .
   uv run ruff format --check .
   python -m compileall custom_components tests scripts
   uv build --out-dir dist
   python scripts/verify_release_artifacts.py dist
   ```

5. Confirm the GitHub `Lint` and `Validate` workflows pass for the exact release commit.

## Brand assets

Home Assistant 2026.3 and newer can load custom-integration assets from `custom_components/halo_collar/brand/`; older versions may show a placeholder. HACS may also lag Home Assistant's local-brand display support. See Home Assistant's [brand image documentation](https://developers.home-assistant.io/docs/core/integration/brand_images/) and [custom-integration brand announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).

Keep the icon square, transparent, trimmed, and at most 256×256; optional `@2x`, logo, and dark-theme variants may also be provided.

Use only original or properly licensed artwork. Do not copy Halo's trademarked logo for this unofficial integration.

## Publish and verify

1. Create a `vX.Y.Z` tag and GitHub release with user-facing notes.
2. Mark beta or release-candidate builds as prereleases.
3. Confirm tagged `hacs.json`, `manifest.json`, local brand icon, and the source archive are publicly reachable.
4. Install or update through HACS and restart Home Assistant.
5. Confirm representative entities populate and compare important telemetry with the official Halo app.
6. Confirm release artifacts, screenshots, logs, and diagnostics contain no credentials, pet names, serial numbers, locations, fences, provider IDs, or private account data. Home Assistant's private device/entity registries intentionally retain stable device identity and local display labels; protect those registries and backups as household data.

## HACS default submission

The repository must remain public, pass Hassfest and HACS validation, include current release artifacts and local brand assets, and maintain an accurate description, topics, issue tracker, and documentation links.
