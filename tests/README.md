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

## One command

```bash
tests/run-all.sh                    # offline layers (what GitHub runs on every push)
tests/run-all.sh --live             # + read-only smoke and security checks on the running site
tests/run-all.sh --live --browser   # + real-browser suite (signed-in Chromium on :9222)
```

| Layer | File | Needs |
|---|---|---|
| Web bridge unit tests | `tests/web-bridge.test.mjs` | Node 22 |
| Browser-pane address filter | `tests/relay/test_relay_filters.py` | Python 3 |
| Nginx routing, blocking, headers, upload limit | `tests/nginx/run_nginx_tests.sh` | Docker (throwaway containers, port 18099) |
| Web-terminal plugin (stays disabled in prod) | `gateway-plugin/web-terminal/tests` | hermes-agent venv |
| Live smoke | `tests/live-smoke.sh` | running site |
| Live security (auth, WS, traversal, container hardening) | `tests/security/run_security_tests.py` | running site + docker |
| Real browser (profiles, themes, attach, expiry) | `tests/browser/run_browser_tests.py` | signed-in Chromium on :9222 |

GitHub Actions: `.github/workflows/tests.yml` runs the offline layers on pushes to `robweb` / `wip/**` and on PRs.
