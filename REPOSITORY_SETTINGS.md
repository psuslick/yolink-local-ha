# Repository settings required for HACS validation

These are GitHub repository settings, so they cannot be changed by an integration ZIP or HACS update.

Before expecting the HACS Action to pass, verify the following on `psuslick/yolink-local-ha`:

1. **Issues enabled** — Repository **Settings → General → Features → Issues**.
2. **Repository description set** — the GitHub **About** section should briefly describe the project.
3. **Repository topics set** — recommended topics: `home-assistant`, `hacs`, `yolink`, `local-control`, `iot`.
4. The repository remains **public**.
5. After CI passes, create a real GitHub **Release** for the version (not only a tag) so HACS presents normal version choices.

## Housekeeping failures fixed by v0.7.0 files

- Removed the invalid `icon` key from `custom_components/yolocal/manifest.json`.
- Added local Home Assistant brand assets at `custom_components/yolocal/brand/icon.png` and `icon@2x.png`.
- Updated the validation workflow to test the whole test suite and use current checkout/setup-python actions.
- Opted the workflow into Node.js 24 for JavaScript actions to avoid the Node.js 20 runner deprecation warning where supported.

If HACS validation still reports `issues` or `topics`, those settings must be corrected in GitHub's repository UI; they are not file-based settings.
