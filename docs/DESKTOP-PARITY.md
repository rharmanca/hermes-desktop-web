# Desktop vs Web parity audit

Goal: Desktop Web should behave like the native Hermes Desktop app, from any browser.

Audited 2026-10-24 against live image `e0ab439` (renderer Hermes v0.22.0) and the
native preload from Hermes v0.22.5.

## How the web version works

`web-bridge.js` reports itself as a **remote** connection (`mode: 'remote'`). The
renderer then uses server REST endpoints for files and Git (`/api/fs/*`,
`/api/git/*`, the same path the native app uses in Remote-gateway mode). So most of the
~200 native-only APIs are either desktop-window features or are served by the
backend. Anything the bridge doesn't define returns a silent no-op (the universal
Proxy at the end of `web-bridge.js`), so gaps show up as *nothing happens*, not crashes.

## Method (repeatable)

1. `node tests/parity/api-surface.cjs <compiled electron-preload.cjs> web-bridge.js`
   lists every native API key and marks it as implemented, stub or missing.
2. `python3 tests/parity/runtime_probe.py` loads the live app in the server's
   Chromium (CDP :9222), visits the main screens, and records every API the app
   actually asked for that the bridge doesn't provide, plus page errors.

Result: 272 native keys, 23 implemented, 48 stubs. At runtime the app touched 37
missing APIs and logged **0 page errors**.

## Gaps that matter, ranked

| # | Gap | What Rob notices | Effort |
|---|-----|------------------|--------|
| 1 | Agent can't drive the browser pane (`open_preview`/`read_preview`) | Hermes can't open or read pages for you | Large |
| 2 | Notifications never appear: `notify()` calls `new Notification` but permission is never requested; click-to-open (`onNotificationActivate`) is a stub | No alert when a long task finishes | Small |
| 3 | Default/last profile: `profile.get/set/remember/getDefault/setDefault` missing | Opens on the default profile instead of the last one | Small |
| 4 | "Open in new window" (`openSessionWindow`, `openSessionInTerminal`, `openWindow`) does nothing | Menu items silently fail | Small (open a new tab) |
| 5 | Multiple tabs don't sync (`windowRelay`) | Two tabs can drift | Medium (BroadcastChannel) |
| 6 | File tree doesn't refresh on change (`watchDirectory`, `onPreviewFileChanged`) | Need manual refresh | Medium (polling) |
| 7 | "Paste image" button (`saveClipboardImage`) returns nothing. Normal Ctrl/Cmd+V uses `saveImageBuffer`, which works | The button reports "no image on clipboard" | Small |
| 8 | `screenshot`, find-bar events | Minor; browser Cmd+F works | Low |

Desktop-only, intentionally skipped: HUD, pet overlay, quick entry, tray, battery,
keep-awake, app updates/uninstall, F12, local models, bootstrap, desktop plugins.

Web terminal stays disabled pending security review (PER-309).

## Recommended order

2, 3, 4, 7 together (small bridge-only changes, about 2–3 h with tests), then 1 (agent browser
control) as its own project.
