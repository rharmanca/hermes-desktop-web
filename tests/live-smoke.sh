#!/usr/bin/env bash
# Post-deploy smoke test for a running Desktop Web container.
# Usage: tests/live-smoke.sh [base-url]   (default http://127.0.0.1:18090)
# Read-only: makes GET/POST requests without credentials; changes nothing.
set -u
BASE="${1:-http://127.0.0.1:18090}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()  { echo "PASS  $1"; pass=$((pass+1)); }
bad() { echo "FAIL  $1"; fail=$((fail+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
index="$(curl -s "$BASE/")"

check "app page loads (200)"                    '[ "$(code "$BASE/")" = 200 ]'
check "page loads web-bridge.js with a ?v= hash" 'grep -qE "web-bridge\.js\?v=[a-f0-9]+" <<<"$index"'
check "web-bridge.js is served no-cache"         'curl -sI "$BASE/web-bridge.js" | grep -qi "cache-control:.*no-store"'
check "served bridge has sign-in-expired guard"  'curl -s "$BASE/web-bridge.js" | grep -q reauthAtEdge'
check "backend reachable through app (/api/status 200)" '[ "$(code "$BASE/api/status")" = 200 ]'
check "ws-ticket requires sign-in (401 without cookie)"  '[ "$(code -X POST "$BASE/api/auth/ws-ticket")" = 401 ]'
check "profiles require sign-in (401 without cookie)"    '[ "$(code "$BASE/api/profiles")" = 401 ]'
check "ws-ticket answers JSON, not an HTML page" \
  'curl -s -X POST -D - -o /dev/null "$BASE/api/auth/ws-ticket" | grep -qi "content-type:.*json"'

# Container checks are required: a missing container is a failure, not a skip.
check "container exists and reports healthy" '[ "$(docker inspect -f "{{.State.Health.Status}}" hermes-desktop-web 2>/dev/null)" = healthy ]'
rev="$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' hermes-desktop-web 2>/dev/null || true)"
check "running image built from a commit on GitHub (origin/robweb contains ${rev:-<none>})" \
  '[ -n "$rev" ] && git -C "$ROOT" merge-base --is-ancestor "$rev" origin/robweb 2>/dev/null'
# Exact content match: what is served must be byte-identical to that commit.
check "served web-bridge.js is exactly the file in commit ${rev:0:7}" \
  '[ "$(curl -s "$BASE/web-bridge.js" | sha256sum)" = "$(git -C "$ROOT" show "$rev:web-bridge.js" | sha256sum)" ]'
check "served web-preview.js is exactly the file in commit ${rev:0:7}" \
  '[ "$(curl -s "$BASE/web-preview.js" | sha256sum)" = "$(git -C "$ROOT" show "$rev:deploy/robweb/web-preview.js" | sha256sum)" ]'
check "running nginx config is exactly the file in commit ${rev:0:7}" \
  '[ "$(docker exec hermes-desktop-web cat /etc/nginx/conf.d/default.conf | sha256sum)" = "$(git -C "$ROOT" show "$rev:deploy/robweb/nginx.conf" | sha256sum)" ]'
RELAY_PID="$(systemctl --user show -p MainPID --value hermes-preview-relay 2>/dev/null || echo 0)"
check "browser-pane relay runs the file from commit ${rev:0:7}" \
  '[ "$RELAY_PID" != 0 ] && [ "$(sha256sum < "/proc/$RELAY_PID/cwd/relay.py")" = "$(git -C "$ROOT" show "$rev:deploy/robweb/preview-relay/relay.py" | sha256sum)" ]'
check "browser-pane relay healthy" 'curl -sf -m3 http://127.0.0.1:18091/healthz | grep -q "\"ok\": true"'

echo "---- $pass passed, $fail failed"
[ "$fail" = 0 ]
