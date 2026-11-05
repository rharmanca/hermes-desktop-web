# Server-approved Desktop plugins

Desktop Web cannot install plugins from the browser. Plugins here are baked into the
image and are the only ones the app will load.

A loaded plugin runs with the same authority as the app itself (it is not sandboxed),
so treat every plugin as code you are shipping.

## Add or update a plugin

1. Fetch `plugin.js` (and its licence) from the plugin's repository at an exact commit.
2. Review it: no network, storage, `document`/`window`, `eval`, or dynamic `import()`;
   run `hermes_cli.plugin_validate_desktop.desktop_surface_findings` from hermes-agent (must be `[]`).
3. Put it at `<name>/plugin.js` (name: lowercase letters, digits, dashes).
4. Add `{name, sha256, version, license, source, commit, path}` to `manifest.json`.
5. Regenerate `SHA256SUMS`: `sha256sum */plugin.js > SHA256SUMS` (run in this folder).
6. Add a `COPY` line for the new file to `deploy/robweb/Dockerfile`.
7. `tests/run-all.sh --partial`, commit, push, then the normal deploy.

To remove a plugin, delete its folder, manifest entry, `SHA256SUMS` line and `COPY` line.

## Installed

| Plugin | Version | Source | Commit |
|---|---|---|---|
| profile-picker | 1.1.0 (MIT) | https://github.com/weltmaister/hermes-profile-picker | `9a38fa52616c8b1be5397c4d922e80a2c754613d` |
| waypoint | 2.5.0 (MIT) | https://github.com/mochiNJ/hermes-waypoint | `971bab6762466bfdf6036aeaeb35352d6308f61d` |
| gmail | 1.0.20 (Private, not vendored) | https://github.com/guberm/hermes-gmail-desktop-plugin | `c8cd0c2d140561a1dffb7432c6d02b63b2374888` |

### Reviewed exceptions

- **waypoint**: reads page layout only (`document.querySelector`, `getBoundingClientRect`,
  `window.innerWidth/innerHeight`) to place its status card beside the sidebar, and writes to the
  clipboard only when you click Export. Gateway calls are limited to `session.list`,
  `session.history`, `session.archive` (only when you press Archive) and `llm.oneshot` (AI ideas are
  off by default; Power up runs only when pressed). `tests/desktop-plugins.test.mjs` pins this exact
  set, so an update that adds browser access fails the tests until it is re-reviewed.
  Its Python half (aux-task registration + observe-only call log) is installed per profile with
  `hermes plugins install waypoint`.
- **gmail**: the upstream licence is `Private`, so this public repo does not carry its code.
  `manifest.json` marks it `"vendored": false`; the Dockerfile fetches `plugins/gmail/desktop/plugin.js`
  from the pinned upstream commit with `ADD --checksum`, so the build fails on any byte change (see
  `gmail/NOTICE`). Reviewed: no network, browser storage, eval or dynamic import; gateway access is
  `ctx.rest` to its own `/api/plugins/gmail` backend only. The one raw-HTML use parses the backend's
  sanitised email body into a detached inert `<template>`; React re-renders it from a tag/style
  allowlist, remote images load only after the user clicks, links open only via an explicit click.
  Send/reply/archive/trash/label changes require confirmation in the backend. The backend (Python half)
  is installed per profile (`hermes -p <profile> plugins install gmail --ref <commit>`) and reads that
  profile's own `google_token.json`; only `work-primary` has it enabled. To re-check a local copy:
  `HDW_GMAIL_PLUGIN_JS=<path to plugin.js> node --test tests/desktop-plugins.test.mjs`.
  To update: change the commit, URL and checksum in `manifest.json`, `SHA256SUMS`, the Dockerfile `ADD`
  line and the test pins together, after re-reviewing the new file.
