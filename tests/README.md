# Tests

Two layers. Run both before and after every deploy.

| Command | What it checks | Needs |
|---|---|---|
| `node --test tests/*.test.mjs` | `web-bridge.js` behaviour in a fake browser: ticket minting, sign-in-expired re-auth (and no reload loop), REST `api()` profile/error handling, the renderer contract (void methods, subscriptions, unknown-method no-ops, prepared submissions) | Node 18+ only, no server |
| `tests/live-smoke.sh [url]` | The running container: page + cache-busted bridge, no-cache header, served bridge matches this checkout, backend reachable, auth gates return 401 (JSON, not HTML), container healthy, image revision is pushed to `origin/robweb` | Running deployment (default `http://127.0.0.1:18090`) |

Deploy rule: unit tests green → commit + push → build → deploy → `live-smoke.sh` green.

Not covered (needs a real signed-in browser): actual Cloudflare Access expiry,
profile switching end-to-end, themes, attachments, browser pane.

Manual browser checks: see `tests/MANUAL-CHECKLIST.md`.
