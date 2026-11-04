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

### Reviewed exceptions

- **waypoint**: reads page layout only (`document.querySelector`, `getBoundingClientRect`,
  `window.innerWidth/innerHeight`) to place its status card beside the sidebar, and writes to the
  clipboard only when you click Export. Gateway calls are limited to `session.list`,
  `session.history`, `session.archive` (only when you press Archive) and `llm.oneshot` (AI ideas are
  off by default; Power up runs only when pressed). `tests/desktop-plugins.test.mjs` pins this exact
  set, so an update that adds browser access fails the tests until it is re-reviewed.
  Its Python half (aux-task registration + observe-only call log) is installed per profile with
  `hermes plugins install waypoint`.
