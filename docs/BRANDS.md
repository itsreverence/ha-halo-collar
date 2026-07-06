# Submitting to home-assistant/brands

HACS default inclusion requires the integration's domain to exist in the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository so
Home Assistant can show an icon/logo. This is a separate PR from the HACS default
submission and must be merged first.

## What you need

Create the following PNG assets (transparent background, trimmed to content):

| File     | Size (max)         | Notes                                  |
| -------- | ------------------ | -------------------------------------- |
| `icon.png`  | 256x256 (square) | Required. Also provide `icon@2x.png` at 512x512. |
| `logo.png`  | 128–256 tall     | Optional wordmark. Also `logo@2x.png` at 2x.     |

Guidelines: <https://github.com/home-assistant/brands#guidelines>

> Use only artwork you have the right to publish. Do not copy Halo Collar's
> trademarked logo unless permitted; a neutral, original icon is safest for an
> unofficial integration.

## Directory layout in the brands repo

Because this is a custom integration, place the assets under `custom_integrations`:

```text
custom_integrations/halo_collar/icon.png
custom_integrations/halo_collar/icon@2x.png
custom_integrations/halo_collar/logo.png        # optional
custom_integrations/halo_collar/logo@2x.png     # optional
```

## Steps

1. Fork `home-assistant/brands`.
2. Add the files above under `custom_integrations/halo_collar/`.
3. Open a PR. CI validates dimensions and transparency.
4. Once merged, proceed with the `hacs/default` PR (see `docs/WORKFLOW.md`).
