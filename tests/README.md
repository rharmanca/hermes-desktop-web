# Tests

Two layers. Run both before and after every deploy.

| Command | What it checks | Needs |
|---|---|---|
| `node --test tests/*.test.mjs` | `web-bridge.js` behaviour in a fake browser: ticket minting, sign-in-expired re-auth (and no reload loop), REST `api()` profile/error handling, the renderer contract (void methods, subscriptions, unknown-method no-ops, prepared submissions) | Node 18+ only, no server |
| `tests/live-smoke.sh [url]` | The running container: page + cache-busted bridge, no-cache header, relay runs the committed file, backend reachable, auth gates return 401 (JSON, not HTML), container healthy, image revision is pushed to `origin/robweb` | Running deployment (default `http://127.0.0.1:18090`) |

Deploy rule: unit tests green → commit + push → build → deploy → `live-smoke.sh` green.

Covered by the real-browser suite: profile switching, themes, attachments, sign-in expiry.
Still manual: real Cloudflare Access expiry and the browser pane by eye.

Manual browser checks: see `tests/MANUAL-CHECKLIST.md`.

## One command

```bash
tests/run-all.sh --live --browser   # everything; the full check after a deploy
tests/run-all.sh --partial          # offline layers only (what GitHub runs on every push)
tests/run-all.sh --partial --live   # + read-only smoke, security, terminal attack test
```

Without `--partial`, any skipped layer fails the run, so a green result means every layer ran.
The optional chat round-trip (`run_browser_tests.py --send`) stays off: it creates a real chat.

| Layer | File | Needs |
|---|---|---|
| Web bridge unit tests | `tests/web-bridge.test.mjs` | Node 22 |
| Browser-pane address filter | `tests/relay/test_relay_filters.py` | Python 3 |
| Nginx routing, blocking, headers, upload limit | `tests/nginx/run_nginx_tests.sh` | Docker (throwaway containers, port 18099) |
| Web-terminal plugin (on for owner only) | `gateway-plugin/web-terminal/tests` | hermes-agent venv |
| Browser-pane relay end to end (real Chromium tries to reach private services) | `tests/relay/run_relay_e2e.py` | Chromium |
| Web-terminal attack suite (each refusal must come from the right gate) | `tests/web-terminal/run_terminal_attack.py` | hermes-agent venv; throwaway dashboard, never the live one |
| Live smoke | `tests/live-smoke.sh` | running site |
| Live security (auth, WS, traversal, headers, container hardening, firewall, relay reachable only from Desktop Web) | `tests/security/run_security_tests.py` | running site + docker |
| Real browser (profiles, themes, attach, expiry) | `tests/browser/run_browser_tests.py` | signed-in Chromium on :9222 |

GitHub Actions: `.github/workflows/tests.yml` runs the offline layers on pushes to `robweb` / `wip/**` and on PRs.
