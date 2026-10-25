#!/usr/bin/env bash
# One command for every test layer.
#   tests/run-all.sh --partial offline layers only (safe anywhere, what GitHub runs)
#   Without --partial any skipped layer FAILS the run: a green result means every layer ran.
#   tests/run-all.sh --live    also the read-only checks against the running robweb site,
#                              plus the web-terminal attack test (own throwaway dashboard)
#   tests/run-all.sh --live --browser   also the real-browser suite (needs signed-in Chromium on :9222)
set -uo pipefail
cd "$(dirname "$0")/.."
LIVE=0; BROWSER=0; PARTIAL=0
for a in "$@"; do case "$a" in --live) LIVE=1;; --browser) BROWSER=1;; --partial) PARTIAL=1;; esac; done
declare -a NAMES RESULTS
run() { local name="$1"; shift; echo; echo "===== $name"; if "$@"; then RESULTS+=("PASS  $name"); else RESULTS+=("FAIL  $name"); fi; }
skip() { RESULTS+=("SKIP  $1 ($2)"); }

run "syntax: web-bridge.js" node --check web-bridge.js
run "syntax: web-preview.js" node --check deploy/robweb/web-preview.js
run "syntax: relay.py" python3 -m py_compile deploy/robweb/preview-relay/relay.py
run "unit: web bridge" bash -c 'node --test tests/*.test.mjs'
run "unit: browser-pane relay filters" python3 tests/relay/test_relay_filters.py
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  run "config: nginx routing + headers" tests/nginx/run_nginx_tests.sh
else skip "config: nginx routing + headers" "docker not available"; fi
if [ -x "${PREVIEW_CHROMIUM:-/usr/lib/chromium/chromium}" ]; then
  run "e2e: browser-pane relay with real Chromium" python3 tests/relay/run_relay_e2e.py
else skip "e2e: browser-pane relay with real Chromium" "chromium not installed"; fi
WT_PY="${HERMES_AGENT_SRC:-$HOME/.hermes/hermes-agent}/.venv/bin/python"
if [ -x "$WT_PY" ]; then
  run "unit: web-terminal plugin (on for owner only)" env HERMES_HOME="$(mktemp -d)" PYTHONPATH="${HERMES_AGENT_SRC:-$HOME/.hermes/hermes-agent}" \
    "$WT_PY" -m pytest gateway-plugin/web-terminal/tests -q -o addopts="" -p no:cacheprovider
else skip "unit: web-terminal plugin" "needs hermes-agent venv"; fi

if [ $LIVE = 1 ]; then
  run "live: smoke" tests/live-smoke.sh
  run "live: security" python3 tests/security/run_security_tests.py
  run "attack: web terminal (throwaway dashboard)" python3 tests/web-terminal/run_terminal_attack.py
else skip "live: smoke + security" "add --live"; fi
if [ $BROWSER = 1 ]; then run "live: real browser" python3 tests/browser/run_browser_tests.py
else skip "live: real browser" "add --browser"; fi

echo; echo "===== SUMMARY"; printf '%s\n' "${RESULTS[@]}"
for r in "${RESULTS[@]}"; do [[ $r == FAIL* ]] && exit 1; done
if [ $PARTIAL = 0 ]; then for r in "${RESULTS[@]}"; do
  [[ $r == SKIP* ]] && { echo "Skipped layers count as failure (use --partial to allow)"; exit 1; }; done; fi
exit 0
