# Brand icon & logo

Home Assistant no longer accepts brand images for **custom** integrations in the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository —
PRs there are auto-closed. Starting with Home Assistant 2026.3, custom
integrations ship their own brand images inside the integration directory
instead. See the
[announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)
and the [brand images docs](https://developers.home-assistant.io/docs/core/integration/brand_images/).

## What to do for this integration

Add PNG assets (transparent background, trimmed to content) to a `brand/`
folder inside the integration:

```text
custom_components/halo_collar/brand/icon.png        # required, square, max 256x256
custom_components/halo_collar/brand/icon@2x.png     # optional, 512x512
custom_components/halo_collar/brand/logo.png        # optional wordmark
custom_components/halo_collar/brand/logo@2x.png     # optional, 2x logo
```

Dark-theme variants (`dark_icon.png`, `dark_logo.png`, and their `@2x`
versions) are also supported. No manifest changes or extra configuration are
needed — Home Assistant picks the files up automatically and serves them via
`/api/brands/integration/halo_collar/icon.png`. Local images take priority over
anything on the brands CDN.

> Use only artwork you have the right to publish. Do not copy Halo Collar's
> trademarked logo unless permitted; a neutral, original icon is safest for an
> unofficial integration.

## Notes

- Users need Home Assistant 2026.3 or newer to see the icon; on older versions
  the `brand/` folder is simply ignored and a placeholder is shown.
- The HACS dashboard may still show an "icon not available" placeholder for
  integrations using local brand images — HACS is migrating to the new brands
  proxy (see [hacs/integration#5171](https://github.com/hacs/integration/issues/5171)).
  This does not affect the icon shown in Settings → Devices & Services, and it
  does not block a `hacs/default` submission.
